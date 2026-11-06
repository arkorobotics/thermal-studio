"""
PureThermal (1/2/3/Mini) + FLIR Lepton 3.x access through libuvc.

* Streams the radiometric Y16 format (160x120 uint16 for Lepton 3.5).
* Talks to the Lepton SDK through the PureThermal's UVC extension units
  (the same mechanism GroupGets' GetThermal uses):

      unit id    = AGC:3  OEM:4  RAD:5  SYS:6  VID:7
      control id = ((command_id & 0xff) >> 2) + 1
      GET -> UVC GET_CUR, SET -> UVC SET_CUR with the attribute bytes,
      RUN -> UVC SET_CUR with a dummy payload.

Attribute payloads are the Lepton SDK structs in little-endian memory layout
(32-bit enums, 16-bit words).
"""
import ctypes
import ctypes.util
import os
import re
import struct
import sys
import threading
import time

import numpy as np

PT_VID = 0x1E4E
PT_PID = 0x0100

# ------------------------------------------------- board firmware check ---
# Newest PureThermal firmware we know of. Bump this when GroupGets releases a
# new one (https://github.com/groupgets/purethermal1-firmware/releases).
# Deliberately not fetched online: the app never phones home.
LATEST_BOARD_FW = (1, 3, 1)
BOARD_FW_RELEASES = "https://github.com/groupgets/purethermal1-firmware/releases"
BOARD_FW_GUIDE = ("https://github.com/groupgets/purethermal1-firmware"
                  "#installing-firmware-over-usb-via-dfu-mode-for-purethermal-2--purethermal-mini-boards")


def board_firmware_notice(product):
    """Parse the board's USB product name, e.g. "PureThermal (fw:v1.3.0)".

    Returns (version string or None, update notice dict or None).
    """
    m = re.search(r"fw:v?(\d+)\.(\d+)\.(\d+)", product or "")
    if not m:
        return None, None
    ver = tuple(int(x) for x in m.groups())
    current = "v%d.%d.%d" % ver
    if ver >= LATEST_BOARD_FW:
        return current, None
    notice = {
        "current": current,
        "latest": "v%d.%d.%d" % LATEST_BOARD_FW,
        "releases_url": BOARD_FW_RELEASES,
        "guide_url": BOARD_FW_GUIDE,
    }
    if ver < (1, 3, 1):
        notice["reason"] = ("Firmware before v1.3.1 can start streaming blank or scrambled "
                            "if the previous session stopped mid-frame. Thermal Studio works "
                            "around it, but other apps (and quick restarts) can still hit it.")
    return current, notice


# ---------------------------------------------------------------- libuvc ---

UVC_GET_CUR = 0x81


class uvc_context(ctypes.Structure):
    pass


class uvc_device(ctypes.Structure):
    pass


class uvc_device_handle(ctypes.Structure):
    pass


class timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class uvc_frame(ctypes.Structure):
    # Only the leading fields we read; libuvc owns the struct.
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("data_bytes", ctypes.c_size_t),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("frame_format", ctypes.c_int),
        ("step", ctypes.c_size_t),
        ("sequence", ctypes.c_uint32),
        ("capture_time", timeval),
    ]


class uvc_device_descriptor(ctypes.Structure):
    _fields_ = [("idVendor", ctypes.c_uint16), ("idProduct", ctypes.c_uint16),
                ("bcdUVC", ctypes.c_uint16), ("serialNumber", ctypes.c_char_p),
                ("manufacturer", ctypes.c_char_p), ("product", ctypes.c_char_p)]


# uvc_stream_ctrl_t is ~40 bytes; give libuvc plenty of room.
StreamCtrl = ctypes.c_uint8 * 256

FRAME_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(uvc_frame), ctypes.c_void_p)

UVC_ERRORS = {
    -1: "I/O error", -2: "invalid parameter", -3: "access denied",
    -4: "no such device", -5: "entity not found", -6: "resource busy",
    -7: "timeout", -8: "overflow", -9: "pipe error", -10: "interrupted",
    -11: "out of memory", -12: "not supported", -50: "invalid device",
    -51: "invalid mode", -52: "callback exists", -99: "unknown error",
}


def _load_libuvc():
    candidates = []
    env = os.environ.get("LIBUVC_PATH")
    if env:
        candidates.append(env)
    found = ctypes.util.find_library("uvc")
    if found:
        candidates.append(found)
    candidates += [
        "/opt/homebrew/lib/libuvc.dylib",      # Homebrew, Apple Silicon
        "/usr/local/lib/libuvc.dylib",         # Homebrew, Intel / source build
        "/opt/local/lib/libuvc.dylib",         # MacPorts
        "libuvc.so.0", "libuvc.so",
        "/usr/lib/x86_64-linux-gnu/libuvc.so.0",
        "/usr/lib/aarch64-linux-gnu/libuvc.so.0",
        "libuvc.dll", "uvc.dll",
    ]
    errors = []
    for c in candidates:
        try:
            return ctypes.cdll.LoadLibrary(c)
        except OSError as e:
            errors.append(f"{c}: {e}")
    raise RuntimeError(
        "Could not load libuvc. On macOS install it with `brew install libuvc` "
        "(or set LIBUVC_PATH=/path/to/libuvc.dylib).\n  " + "\n  ".join(errors[:4]))


class CameraError(RuntimeError):
    pass


# ------------------------------------------------------- Lepton commands ---
# Base command IDs (GET = base, SET = base+1, RUN = base+2)
AGC_ENABLE = 0x0100
AGC_POLICY = 0x0104
SYS_CAM_STATUS = 0x0204
SYS_SERIAL = 0x0208
SYS_UPTIME = 0x020C
SYS_AUX_TEMP = 0x0210
SYS_FPA_TEMP = 0x0214
SYS_FFC_SHUTTER_MODE = 0x023C
SYS_RUN_FFC = 0x0240
SYS_FFC_STATUS = 0x0244
SYS_GAIN_MODE = 0x0248
VID_FREEZE = 0x0324
OEM_PART_NUMBER = 0x481C
OEM_SW_VERSION = 0x4820
RAD_ENABLE = 0x4E10
RAD_FLUX_LINEAR_PARAMS = 0x4EBC
RAD_TLINEAR_ENABLE = 0x4EC0
RAD_TLINEAR_RESOLUTION = 0x4EC4

_UNIT_FOR_MODULE = {0x0100: 3, 0x0800: 4, 0x0E00: 5, 0x0200: 6, 0x0300: 7}

GAIN_MODES = ["high", "low", "auto"]
SHUTTER_MODES = ["manual", "auto", "external"]
FFC_STATUS = {-2: "write error", -1: "error", 0: "ready", 1: "busy",
              2: "averaging frames"}
CAM_STATUS = {0: "ready", 1: "initializing", 2: "low-power", 3: "going standby",
              4: "FFC in progress"}

K0 = 273.15
EMISSIVITY_SCALE = 8192.0


def _addr(cmd):
    unit = _UNIT_FOR_MODULE.get(cmd & 0x3F00)
    if unit is None:
        raise ValueError(f"unknown Lepton module for command 0x{cmd:04x}")
    return unit, ((cmd & 0x00FF) >> 2) + 1


class LeptonUVC:
    """Real camera. Call open(), then start(callback)."""

    is_demo = False
    name = "PureThermal / FLIR Lepton"

    def __init__(self, format_codes=None):
        self.lib = _load_libuvc()
        self._setup_prototypes()
        self.ctx = ctypes.POINTER(uvc_context)()
        self.dev = ctypes.POINTER(uvc_device)()
        self.devh = ctypes.POINTER(uvc_device_handle)()
        self.ctrl = StreamCtrl()
        self._cb_ref = None
        self._user_cb = None
        self._lock = threading.RLock()
        self.streaming = False
        self.width, self.height = 160, 120
        self.scale = 0.01          # Kelvin per count (TLinear 0.01 K)
        self.radiometric = True
        self.bad_frames = 0
        self.product = ""             # USB product name, carries the board firmware version
        self.firmware_notice = None   # set by read_info() if the board firmware is outdated
        env_codes = os.environ.get("UVC_Y16_FORMAT")
        # libuvc >= 0.0.7 (has H264): GRAY16 == 10 ; older / GroupGets fork: Y16 == 9
        self.format_codes = format_codes or ([int(env_codes)] if env_codes else [10, 9])

    def _setup_prototypes(self):
        L = self.lib
        P = ctypes.POINTER
        L.uvc_init.argtypes = [P(P(uvc_context)), ctypes.c_void_p]
        L.uvc_exit.argtypes = [P(uvc_context)]
        L.uvc_find_device.argtypes = [P(uvc_context), P(P(uvc_device)),
                                      ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
        L.uvc_open.argtypes = [P(uvc_device), P(P(uvc_device_handle))]
        L.uvc_close.argtypes = [P(uvc_device_handle)]
        L.uvc_unref_device.argtypes = [P(uvc_device)]
        L.uvc_get_stream_ctrl_format_size.argtypes = [
            P(uvc_device_handle), ctypes.c_void_p, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int]
        L.uvc_start_streaming.argtypes = [P(uvc_device_handle), ctypes.c_void_p,
                                          FRAME_CB, ctypes.c_void_p, ctypes.c_uint8]
        L.uvc_stop_streaming.argtypes = [P(uvc_device_handle)]
        L.uvc_get_ctrl.argtypes = [P(uvc_device_handle), ctypes.c_uint8, ctypes.c_uint8,
                                   ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        L.uvc_set_ctrl.argtypes = [P(uvc_device_handle), ctypes.c_uint8, ctypes.c_uint8,
                                   ctypes.c_void_p, ctypes.c_int]
        L.uvc_get_ctrl_len.argtypes = [P(uvc_device_handle), ctypes.c_uint8, ctypes.c_uint8]
        L.uvc_get_device_descriptor.argtypes = [P(uvc_device), P(P(uvc_device_descriptor))]
        L.uvc_free_device_descriptor.argtypes = [P(uvc_device_descriptor)]
        L.uvc_strerror.restype = ctypes.c_char_p
        L.uvc_strerror.argtypes = [ctypes.c_int]

    def _err(self, code):
        try:
            return self.lib.uvc_strerror(code).decode()
        except Exception:
            return UVC_ERRORS.get(code, str(code))

    # ------------------------------------------------------------ open ---
    def open(self):
        if sys.platform == "darwin" and os.environ.get("THERMAL_ALLOW_LIBUVC_MACOS") != "1":
            # On macOS 26 (Apple Silicon), libuvc detaching Apple's camera driver as root
            # caused repeated kernel panics in IOUSBHostFamily (2026-11-05). Refuse by default.
            raise CameraError(
                "Direct USB (libuvc) access is disabled on macOS: it crashed the system "
                "(kernel panic in Apple's USB driver). Use --demo for now. "
                "Override at your own risk with THERMAL_ALLOW_LIBUVC_MACOS=1.")
        r = self.lib.uvc_init(ctypes.byref(self.ctx), None)
        if r < 0:
            raise CameraError(f"uvc_init failed: {self._err(r)}")
        r = self.lib.uvc_find_device(self.ctx, ctypes.byref(self.dev), PT_VID, PT_PID, None)
        if r < 0:
            raise CameraError(
                "PureThermal not found (USB 1e4e:0100). Check the cable / try another port.")
        r = self.lib.uvc_open(self.dev, ctypes.byref(self.devh))
        if r < 0:
            hint = ""
            if r == -3 and sys.platform == "darwin":
                hint = (" On macOS libuvc needs root to detach the system camera driver:"
                        " run with sudo (./run.sh does this).")
            elif r == -6:
                hint = " Another app (Photo Booth, Zoom, a browser tab...) may be using the camera."
            raise CameraError(f"uvc_open failed: {self._err(r)}.{hint}")
        try:
            d = ctypes.POINTER(uvc_device_descriptor)()
            if self.lib.uvc_get_device_descriptor(self.dev, ctypes.byref(d)) == 0:
                self.product = (d.contents.product or b"").decode(errors="replace")
                self.lib.uvc_free_device_descriptor(d)
        except Exception:
            pass
        self._negotiate_format()
        self._init_radiometry()

    def _negotiate_format(self):
        last = None
        for code in self.format_codes:
            r = self.lib.uvc_get_stream_ctrl_format_size(
                self.devh, ctypes.byref(self.ctrl), code, 160, 120, 9)
            if r >= 0:
                self.format_code = code
                return
            last = r
        raise CameraError(f"Device does not offer 160x120 Y16: {self._err(last)}")

    def _init_radiometry(self):
        try:
            self.set_u32(RAD_ENABLE, 1)
            self.set_u32(RAD_TLINEAR_ENABLE, 1)
            self._refresh_scale()
            self.radiometric = bool(self.get_u32(RAD_TLINEAR_ENABLE))
        except CameraError as e:
            print(f"[lepton] radiometry init failed ({e}); showing raw counts", file=sys.stderr)
            self.radiometric = False

    def _refresh_scale(self):
        res = self.get_u32(RAD_TLINEAR_RESOLUTION)
        self.scale = 0.1 if res == 0 else 0.01

    # ------------------------------------------------------- streaming ---
    def start(self, callback):
        """callback(np.ndarray[uint16, (h,w)], t_seconds)"""
        self._user_cb = callback
        expected = self.width * self.height * 2

        def _cb(frame_p, _user):
            f = frame_p.contents
            if f.data_bytes != expected or f.width != self.width:
                self.bad_frames += 1
                if self.bad_frames == 10:
                    print(f"[lepton] frames are {f.data_bytes} bytes, expected {expected} (Y16). "
                          f"The libuvc format code may be wrong for your libuvc build: try "
                          f"UVC_Y16_FORMAT=9 or UVC_Y16_FORMAT=10.", file=sys.stderr)
                return
            buf = ctypes.string_at(f.data, f.data_bytes)
            arr = np.frombuffer(buf, dtype="<u2").reshape(self.height, self.width).copy()
            try:
                self._user_cb(arr, time.time())
            except Exception as e:  # never let exceptions into libuvc's thread
                print(f"[lepton] frame callback error: {e}", file=sys.stderr)

        self._cb_ref = FRAME_CB(_cb)
        r = self.lib.uvc_start_streaming(self.devh, ctypes.byref(self.ctrl), self._cb_ref, None, 0)
        if r < 0:
            raise CameraError(f"uvc_start_streaming failed: {self._err(r)}")
        self.streaming = True

    def close(self):
        try:
            if self.streaming:
                self.lib.uvc_stop_streaming(self.devh)
                self.streaming = False
            if self.devh:
                self.lib.uvc_close(self.devh)
            if self.dev:
                self.lib.uvc_unref_device(self.dev)
            if self.ctx:
                self.lib.uvc_exit(self.ctx)
        except Exception:
            pass

    # --------------------------------------------------------- control ---
    # Transport hooks (overridden by the macOS IOKit backend in lepton_iokit.py)
    def _ctrl_len(self, unit, ctrl):
        return self.lib.uvc_get_ctrl_len(self.devh, unit, ctrl)

    def _get_ctrl(self, unit, ctrl, buf, n):
        return self.lib.uvc_get_ctrl(self.devh, unit, ctrl, buf, n, UVC_GET_CUR)

    def _set_ctrl(self, unit, ctrl, buf, n):
        return self.lib.uvc_set_ctrl(self.devh, unit, ctrl, buf, n)

    def get_raw(self, cmd, length=None):
        unit, ctrl = _addr(cmd)
        with self._lock:
            n = length or self._ctrl_len(unit, ctrl)
            if n is None or n <= 0:
                raise CameraError(f"cmd 0x{cmd:04x}: GET_LEN failed ({self._err(n)})")
            buf = (ctypes.c_uint8 * n)()
            r = self._get_ctrl(unit, ctrl, buf, n)
            if r < 0:
                raise CameraError(f"cmd 0x{cmd:04x}: GET failed ({self._err(r)})")
            return bytes(buf)[:r if r > 0 else n]

    def set_raw(self, cmd, data):
        unit, ctrl = _addr(cmd)
        with self._lock:
            n = self._ctrl_len(unit, ctrl)
            if n > 0 and len(data) < n:
                data = data + b"\x00" * (n - len(data))
            buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            r = self._set_ctrl(unit, ctrl, buf, len(data))
            if r < 0:
                raise CameraError(f"cmd 0x{cmd:04x}: SET failed ({self._err(r)})")

    def run(self, cmd):
        unit, ctrl = _addr(cmd)
        with self._lock:
            n = self._ctrl_len(unit, ctrl)
            n = n if n and n > 0 else 1
            buf = (ctypes.c_uint8 * n)()
            r = self._set_ctrl(unit, ctrl, buf, n)
            if r < 0:
                raise CameraError(f"cmd 0x{cmd:04x}: RUN failed ({self._err(r)})")

    def get_u32(self, cmd):
        return struct.unpack_from("<i", self.get_raw(cmd))[0]

    def set_u32(self, cmd, v):
        self.set_raw(cmd, struct.pack("<i", int(v)))

    def get_u16(self, cmd):
        return struct.unpack_from("<H", self.get_raw(cmd))[0]

    # ------------------------------------------------- high level API ---
    def _get_shutter(self):
        b = self.get_raw(SYS_FFC_SHUTTER_MODE)
        b = b + b"\x00" * (32 - len(b))
        return list(struct.unpack("<iiiiIIiHH", b[:32]))

    def read_settings(self):
        s = {}

        def safe(key, fn):
            try:
                s[key] = fn()
            except Exception as e:
                s.setdefault("_errors", {})[key] = str(e)

        safe("gain_mode", lambda: GAIN_MODES[self.get_u32(SYS_GAIN_MODE)])
        try:
            sh = self._get_shutter()
            s["shutter_mode"] = SHUTTER_MODES[sh[0]] if 0 <= sh[0] < 3 else str(sh[0])
            s["video_freeze_during_ffc"] = bool(sh[2])
            s["ffc_period_s"] = sh[5] / 1000.0
            s["ffc_temp_delta_K"] = sh[7] / 100.0
            s["time_since_ffc_s"] = sh[4] / 1000.0
        except Exception as e:
            s.setdefault("_errors", {})["shutter"] = str(e)
        safe("radiometry_enabled", lambda: bool(self.get_u32(RAD_ENABLE)))
        safe("tlinear_enabled", lambda: bool(self.get_u32(RAD_TLINEAR_ENABLE)))
        safe("tlinear_resolution", lambda: 0.1 if self.get_u32(RAD_TLINEAR_RESOLUTION) == 0 else 0.01)
        try:
            f = struct.unpack("<8H", self.get_raw(RAD_FLUX_LINEAR_PARAMS)[:16])
            s.update({
                "emissivity": round(f[0] / EMISSIVITY_SCALE, 4),
                "t_bkg_C": round(f[1] / 100 - K0, 2),
                "tau_window": round(f[2] / EMISSIVITY_SCALE, 4),
                "t_window_C": round(f[3] / 100 - K0, 2),
                "tau_atm": round(f[4] / EMISSIVITY_SCALE, 4),
                "t_atm_C": round(f[5] / 100 - K0, 2),
                "refl_window": round(f[6] / EMISSIVITY_SCALE, 4),
                "t_refl_C": round(f[7] / 100 - K0, 2),
            })
        except Exception as e:
            s.setdefault("_errors", {})["flux"] = str(e)
        if s.get("tlinear_resolution"):
            self.scale = s["tlinear_resolution"]
        if "tlinear_enabled" in s:
            self.radiometric = s["tlinear_enabled"]
        return s

    def read_status(self):
        st = {}
        for key, cmd in (("fpa_C", SYS_FPA_TEMP), ("aux_C", SYS_AUX_TEMP)):
            try:
                st[key] = round(self.get_u16(cmd) / 100.0 - K0, 2)
            except Exception:
                pass
        try:
            st["ffc_status"] = FFC_STATUS.get(self.get_u32(SYS_FFC_STATUS), "?")
        except Exception:
            pass
        try:
            st["camera_status"] = CAM_STATUS.get(self.get_u32(SYS_CAM_STATUS), "?")
        except Exception:
            pass
        try:
            st["time_since_ffc_s"] = self._get_shutter()[4] / 1000.0
        except Exception:
            pass
        return st

    def read_info(self):
        info = {"backend": "libuvc", "y16_format_code": getattr(self, "format_code", None)}
        fw, self.firmware_notice = board_firmware_notice(getattr(self, "product", ""))
        if fw:
            info["board_firmware"] = fw
        try:
            info["serial"] = str(struct.unpack("<Q", self.get_raw(SYS_SERIAL)[:8])[0])
        except Exception:
            pass
        try:
            info["part_number"] = self.get_raw(OEM_PART_NUMBER).split(b"\x00")[0].decode(errors="replace").strip()
        except Exception:
            pass
        try:
            v = self.get_raw(OEM_SW_VERSION)
            info["firmware"] = f"GPP {v[0]}.{v[1]}.{v[2]} / DSP {v[3]}.{v[4]}.{v[5]}"
        except Exception:
            pass
        return info

    def apply_setting(self, key, value):
        if key == "gain_mode":
            self.set_u32(SYS_GAIN_MODE, GAIN_MODES.index(value))
            # 0.01 K TLinear tops out at 655 K (382 C); low/auto gain can exceed that.
            if value in ("low", "auto"):
                try:
                    self.set_u32(RAD_TLINEAR_RESOLUTION, 0)
                except CameraError:
                    pass
        elif key in ("shutter_mode", "ffc_period_s", "ffc_temp_delta_K", "video_freeze_during_ffc"):
            sh = self._get_shutter()
            if key == "shutter_mode":
                sh[0] = SHUTTER_MODES.index(value)
            elif key == "ffc_period_s":
                sh[5] = int(float(value) * 1000)
            elif key == "ffc_temp_delta_K":
                sh[7] = int(float(value) * 100)
            else:
                sh[2] = 1 if value else 0
            self.set_raw(SYS_FFC_SHUTTER_MODE, struct.pack("<iiiiIIiHH", *sh))
        elif key == "radiometry_enabled":
            self.set_u32(RAD_ENABLE, 1 if value else 0)
        elif key == "tlinear_enabled":
            self.set_u32(RAD_TLINEAR_ENABLE, 1 if value else 0)
        elif key == "tlinear_resolution":
            self.set_u32(RAD_TLINEAR_RESOLUTION, 0 if float(value) >= 0.05 else 1)
        elif key in ("emissivity", "t_bkg_C", "tau_window", "t_window_C",
                     "tau_atm", "t_atm_C", "refl_window", "t_refl_C"):
            f = list(struct.unpack("<8H", self.get_raw(RAD_FLUX_LINEAR_PARAMS)[:16]))
            idx = ["emissivity", "t_bkg_C", "tau_window", "t_window_C",
                   "tau_atm", "t_atm_C", "refl_window", "t_refl_C"].index(key)
            v = float(value)
            if key.startswith("t_"):
                f[idx] = int(round((v + K0) * 100))
            else:
                f[idx] = int(round(max(0.0, min(1.0, v)) * EMISSIVITY_SCALE))
            self.set_raw(RAD_FLUX_LINEAR_PARAMS, struct.pack("<8H", *f))
        else:
            raise ValueError(f"unknown setting {key}")
        time.sleep(0.05)
        try:
            self._refresh_scale()
        except CameraError:
            pass

    def run_ffc(self):
        self.run(SYS_RUN_FFC)
