"""
macOS backend: PureThermal / FLIR Lepton through IOKit (no sudo, no libuvc).

libuvc on macOS needs root to detach Apple's camera driver, and on macOS 26
(Apple Silicon) that detach caused kernel panics. This backend uses the small
C library in macos/lepton_iokit.c, which opens the camera the way
Lepton-GetThermal does (IOKit "seize", no re-enumeration) and streams raw Y16.

All Lepton command logic (settings, FFC, radiometry) is inherited from
LeptonUVC; only the USB transport is replaced.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time

import numpy as np

from lepton_uvc import CameraError, LeptonUVC, _addr

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "macos", "lepton_iokit.c")
LIB = os.path.join(HERE, "macos", "liblepton_iokit.dylib")

FRAME_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
                            ctypes.c_int, ctypes.c_int)


def build_helper(force=False):
    """Compile macos/liblepton_iokit.dylib if it's missing or older than the source."""
    if getattr(sys, "frozen", False):
        # Packaged app: the dylib was built at package time (build.py) and bundled.
        if not os.path.exists(LIB):
            raise CameraError("this build is missing macos/liblepton_iokit.dylib")
        return LIB
    if not force and os.path.exists(LIB) and os.path.getmtime(LIB) >= os.path.getmtime(SRC):
        return LIB
    cmd = ["clang", "-O2", "-Wall", "-Wno-deprecated-declarations", "-dynamiclib",
           "-framework", "IOKit", "-framework", "CoreFoundation", "-o", LIB, SRC]
    print("[lepton] building macos/liblepton_iokit.dylib ...", file=sys.stderr)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise CameraError("clang not found. Install the Xcode Command Line Tools: "
                          "xcode-select --install")
    except subprocess.CalledProcessError as e:
        raise CameraError(f"building the IOKit helper failed:\n{e.stderr}")
    return LIB


def load_helper():
    lib = ctypes.cdll.LoadLibrary(build_helper())
    u8p = ctypes.POINTER(ctypes.c_uint8)
    lib.lt_last_error.restype = ctypes.c_char_p
    lib.lt_product.restype = ctypes.c_char_p
    lib.lt_open.restype = ctypes.c_int
    lib.lt_size.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    lib.lt_start.argtypes = [FRAME_CB]
    lib.lt_get_ctrl.argtypes = [ctypes.c_uint8, ctypes.c_uint8, ctypes.c_void_p, ctypes.c_uint16]
    lib.lt_set_ctrl.argtypes = [ctypes.c_uint8, ctypes.c_uint8, ctypes.c_void_p, ctypes.c_uint16]
    lib.lt_get_ctrl_len.argtypes = [ctypes.c_uint8, ctypes.c_uint8]
    lib.lt_stats.argtypes = [ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                             ctypes.POINTER(ctypes.c_int)]
    lib.lt_feed.argtypes = [u8p, ctypes.c_size_t]
    lib.lt_test_setup.argtypes = [ctypes.c_int, ctypes.c_int, FRAME_CB]
    return lib


class LeptonIOKit(LeptonUVC):
    """Real camera on macOS via IOKit. Same interface as LeptonUVC."""

    name = "PureThermal / FLIR Lepton (IOKit)"
    MAX_RECOVERY = 5      # 2 stream restarts + 2 reopens, then ask the user to replug
    stream_error = None   # shown in the UI when the watchdog gives up

    def __init__(self):
        # Deliberately not calling LeptonUVC.__init__: it would load libuvc.
        self.lib = load_helper()
        self._lock = threading.RLock()
        self._cb_ref = None
        self._user_cb = None
        self.streaming = False
        self.is_open = False
        self.width, self.height = 160, 120
        self.scale = 0.01
        self.radiometric = True
        self.bad_frames = 0
        self.format_code = None
        self.product = ""
        self.firmware_notice = None

    def _err(self, code=None):
        return (self.lib.lt_last_error() or b"unknown error").decode(errors="replace")

    # ------------------------------------------------------------ open ---
    def open(self):
        if self.lib.lt_open() < 0:
            raise CameraError(self._err())
        self.is_open = True
        self.product = (self.lib.lt_product() or b"").decode(errors="replace")
        w, h = ctypes.c_int(), ctypes.c_int()
        self.lib.lt_size(ctypes.byref(w), ctypes.byref(h))
        self.width, self.height = w.value, h.value
        self._init_radiometry()

    # ------------------------------------------------------- streaming ---
    def start(self, callback):
        self._user_cb = callback
        expected = self.width * self.height * 2

        def _cb(data, nbytes, w, h):
            if nbytes != expected:
                self.bad_frames += 1
                return
            arr = np.frombuffer(ctypes.string_at(data, nbytes), dtype="<u2").reshape(h, w).copy()
            try:
                self._user_cb(arr, time.time())
            except Exception as e:  # never raise into the C streaming thread
                print(f"[lepton] frame callback error: {e}", file=sys.stderr)

        self._cb_ref = FRAME_CB(_cb)   # keep a reference so it isn't garbage-collected
        if self.lib.lt_start(self._cb_ref) < 0:
            raise CameraError(f"could not start streaming: {self._err()}")
        self.streaming = True
        self._wd_stop = threading.Event()
        self._wd = threading.Thread(target=self._watchdog, daemon=True)
        self._wd.start()

    def _watchdog(self):
        """Restart the stream if frames stop arriving.

        PureThermal firmware <= 1.3.0 can come up silent or scrambled if the
        previous session (another app, a killed process) stopped mid-frame.
        A stream restart usually clears it; a full reopen is the fallback.
        """
        last_ok, last_t, attempts = self.stats()["frames_ok"], time.time(), 0
        while not self._wd_stop.wait(0.5):
            ok = self.stats()["frames_ok"]
            if ok != last_ok:
                last_ok, last_t, attempts = ok, time.time(), 0
                self.stream_error = None
                continue
            if time.time() - last_t < 2.5 or attempts >= self.MAX_RECOVERY:
                continue   # after MAX_RECOVERY attempts: keep watching, stop poking the camera
            attempts += 1
            if attempts == self.MAX_RECOVERY:
                self.stream_error = ("The camera is connected but sends no video. Unplug the "
                                     "PureThermal, plug it back in, then press Reconnect.")
                print(f"[lepton] giving up after {attempts - 1} recovery attempts: "
                      f"{self.stream_error}", file=sys.stderr)
                continue
            full = attempts >= 3
            print(f"[lepton] no frames for {time.time() - last_t:.1f}s; "
                  f"{'reopening camera' if full else 'restarting stream'} (attempt {attempts})",
                  file=sys.stderr)
            with self._lock:
                if self._wd_stop.is_set():
                    return
                self.lib.lt_stop()
                if full:
                    self.lib.lt_close()
                    time.sleep(0.5)
                    if self.lib.lt_open() < 0:
                        print(f"[lepton] reopen failed: {self._err()}", file=sys.stderr)
                        last_t = time.time()
                        continue
                if self.lib.lt_start(self._cb_ref) < 0:
                    print(f"[lepton] restart failed: {self._err()}", file=sys.stderr)
            last_ok, last_t = self.stats()["frames_ok"], time.time()

    def stats(self):
        ok, bad, dead = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_int()
        self.lib.lt_stats(ctypes.byref(ok), ctypes.byref(bad), ctypes.byref(dead))
        return {"frames_ok": ok.value, "frames_bad": bad.value, "stream_dead": bool(dead.value)}

    def close(self):
        wd = getattr(self, "_wd_stop", None)
        if wd is not None:
            wd.set()
        try:
            with self._lock:
                self.lib.lt_close()
        except Exception:
            pass
        self.streaming = False
        self.is_open = False

    # --------------------------------------------------------- control ---
    def _ctrl_len(self, unit, ctrl):
        return self.lib.lt_get_ctrl_len(unit, ctrl)

    def _get_ctrl(self, unit, ctrl, buf, n):
        return self.lib.lt_get_ctrl(unit, ctrl, buf, n)

    def _set_ctrl(self, unit, ctrl, buf, n):
        return self.lib.lt_set_ctrl(unit, ctrl, buf, n)

    def run(self, cmd):
        # RUN = SET_CUR with the control id as a single byte (as Lepton-GetThermal does).
        unit, ctrl = _addr(cmd)
        with self._lock:
            buf = (ctypes.c_uint8 * 1)(ctrl)
            if self._set_ctrl(unit, ctrl, buf, 1) < 0:
                raise CameraError(f"cmd 0x{cmd:04x}: RUN failed ({self._err()})")

    def read_info(self):
        info = super().read_info()
        info["backend"] = "iokit"
        info.pop("y16_format_code", None)
        return info
