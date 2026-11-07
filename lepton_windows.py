"""
Windows backend: PureThermal / FLIR Lepton with the stock Windows UVC driver.

No Zadig / WinUSB driver swap: the camera keeps working in other apps too.

* Frames: OpenCV's DirectShow capture in raw 'Y16 ' mode (CONVERT_RGB off),
  160x120 uint16 = radiometric counts.
* Lepton SDK commands: IKsControl extension-unit properties on the camera's
  DirectShow filter (plain ctypes COM, no extra packages). Windows addresses
  an extension unit by its GUID; the PureThermal firmware uses the ASCII
  strings "pt1-lep-agc-0000" etc. Windows also caps an extension unit at
  31 controls, so the firmware mirrors the high RAD controls on a second
  unit (RAD_2, id 0x15) with selector - 31.

All Lepton command logic is inherited from LeptonUVC; only the transport differs.
"""
import ctypes
import sys
import threading
import time
from ctypes import POINTER, byref, c_long, c_ulong, c_void_p, c_wchar_p, sizeof

import numpy as np

from lepton_uvc import PT_PID, PT_VID, CameraError, LeptonUVC

HRESULT = c_long
S_OK, S_FALSE = 0, 1
ERROR_MORE_DATA_HR = -2147024662          # HRESULT_FROM_WIN32(ERROR_MORE_DATA) = 0x800700EA
ERROR_INSUFFICIENT_BUFFER_HR = -2147024774  # 0x8007007A
COINIT_MULTITHREADED = 0
CLSCTX_INPROC_SERVER = 1
KSPROPERTY_TYPE_GET = 0x00000001
KSPROPERTY_TYPE_SET = 0x00000002
KSPROPERTY_TYPE_TOPOLOGY = 0x10000000
VT_BSTR = 8
WINDOWS_MAX_CONTROLS = 31   # firmware: controls above this live on unit + 0x10


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_uint8 * 8)]


def _guid(s):
    g = GUID()
    _ole32().CLSIDFromString(c_wchar_p("{" + s + "}"), byref(g))
    return g


class KSP_NODE(ctypes.Structure):
    _fields_ = [("Set", GUID), ("Id", ctypes.c_uint32), ("Flags", ctypes.c_uint32),
                ("NodeId", ctypes.c_uint32), ("Reserved", ctypes.c_uint32)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("ptr", c_void_p), ("ptr2", c_void_p)]


# Extension unit GUIDs (Middlewares/ST/STM32_USB_Device_Library/Class/video/Inc/uvc_desc_vc.h
# in groupgets/purethermal1-firmware). The descriptor bytes are the GUID's memory layout.
XU_GUID_BYTES = {
    3: b"pt1-lep-agc-0000",
    4: b"pt1-lep-oem-0000",
    5: b"pt1-lep-rad-0000",
    6: b"pt1-lep-sys-0000",
    7: b"pt1-lep-vid-0000",
    0x15: b"pt1-lep-rad20000",
    0xFE: b"pt1-lep-cust0000",
}

_libs = {}


def _ole32():
    if "ole32" not in _libs:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [c_void_p, ctypes.c_uint32]
        ole32.CoInitializeEx.restype = HRESULT
        ole32.CoCreateInstance.argtypes = [c_void_p, c_void_p, ctypes.c_uint32, c_void_p, c_void_p]
        ole32.CoCreateInstance.restype = HRESULT
        ole32.CLSIDFromString.argtypes = [c_wchar_p, c_void_p]
        ole32.CLSIDFromString.restype = HRESULT
        _libs["ole32"] = ole32
        oleaut32 = ctypes.WinDLL("oleaut32")
        oleaut32.VariantInit.argtypes = [c_void_p]
        oleaut32.VariantClear.argtypes = [c_void_p]
        _libs["oleaut32"] = oleaut32
    return _libs["ole32"]


_tls = threading.local()


def _co_init():
    """COM must be initialised on every thread that touches a COM object."""
    if not getattr(_tls, "com", False):
        _ole32().CoInitializeEx(None, COINIT_MULTITHREADED)   # S_FALSE / mode-changed are fine
        _tls.com = True


def _call(obj, index, *args, argtypes=None):
    """Call vtable slot `index` of COM object `obj` (a c_void_p). Returns the HRESULT."""
    vtbl = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *(argtypes or [c_void_p] * len(args)))
    return proto(vtbl[index])(obj, *args)


def _release(obj):
    if obj:
        vtbl = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(obj)


def _qi(obj, iid):
    out = c_void_p()
    hr = _call(obj, 0, byref(iid), byref(out))
    return out if hr == S_OK and out else None


def _bag_string(bag, name):
    v = VARIANT()
    _libs["oleaut32"].VariantInit(byref(v))
    try:
        hr = _call(bag, 3, name, byref(v), None,              # IPropertyBag::Read
                   argtypes=[c_wchar_p, c_void_p, c_void_p])
        if hr == S_OK and v.vt == VT_BSTR and v.ptr:
            return ctypes.wstring_at(v.ptr)
        return ""
    finally:
        _libs["oleaut32"].VariantClear(byref(v))


def list_video_devices():
    """[(index, friendly name, device path, moniker)] in DirectShow order.

    This is the same order OpenCV's CAP_DSHOW uses for its device index.
    Caller owns (must _release) the returned monikers.
    """
    _co_init()
    CLSID_SystemDeviceEnum = _guid("62BE5D10-60EB-11d0-BD3B-00A0C911CE86")
    IID_ICreateDevEnum = _guid("29840822-5B84-11D0-BD3B-00A0C911CE86")
    CLSID_VideoInputDeviceCategory = _guid("860BB310-5D01-11d0-BD3B-00A0C911CE86")
    IID_IPropertyBag = _guid("55272A00-42CB-11CE-8135-00AA004BB851")

    devenum = c_void_p()
    hr = _ole32().CoCreateInstance(byref(CLSID_SystemDeviceEnum), None, CLSCTX_INPROC_SERVER,
                                   byref(IID_ICreateDevEnum), byref(devenum))
    if hr != S_OK:
        raise CameraError(f"DirectShow device enumerator unavailable (0x{hr & 0xffffffff:08x})")
    em = c_void_p()
    try:
        hr = _call(devenum, 3, byref(CLSID_VideoInputDeviceCategory), byref(em), 0,
                   argtypes=[c_void_p, c_void_p, ctypes.c_uint32])
    finally:
        _release(devenum)
    out = []
    if hr != S_OK or not em:
        return out          # S_FALSE: no video devices at all
    try:
        index = 0
        while True:
            mon, fetched = c_void_p(), c_ulong()
            if _call(em, 3, 1, byref(mon), byref(fetched),
                     argtypes=[c_ulong, c_void_p, c_void_p]) != S_OK or not mon:
                break
            bag = c_void_p()
            name = path = ""
            if _call(mon, 9, None, None, byref(IID_IPropertyBag), byref(bag)) == S_OK and bag:
                name = _bag_string(bag, "FriendlyName")
                path = _bag_string(bag, "DevicePath")
                _release(bag)
            out.append((index, name, path, mon))
            index += 1
    finally:
        _release(em)
    return out


class LeptonWindows(LeptonUVC):
    """Real camera on Windows. Same interface as LeptonUVC."""

    name = "PureThermal / FLIR Lepton (Windows)"
    stream_error = None

    def __init__(self):
        # Deliberately not calling LeptonUVC.__init__: it would load libuvc.
        try:
            import cv2
        except ImportError:
            raise CameraError("OpenCV is missing. Run run.bat again, or: "
                              ".venv\\Scripts\\pip install opencv-python-headless")
        self.cv2 = cv2
        self._lock = threading.RLock()
        self._user_cb = None
        self.streaming = False
        self.width, self.height = 160, 120
        self.scale = 0.01
        self.radiometric = True
        self.bad_frames = 0
        self.format_code = None
        self.product = ""
        self.firmware_notice = None
        self.ks = None
        self.cap = None
        self.index = None
        self._nodes = {}
        self._last_hr = 0
        self._stop = threading.Event()
        self._thread = None

    def _err(self, code=None):
        hr = self._last_hr if isinstance(self._last_hr, int) else 0
        if not hr:
            return "unknown error"
        msg = ctypes.FormatError(hr & 0xffffffff).strip() if hasattr(ctypes, "FormatError") else ""
        return f"0x{hr & 0xffffffff:08x} {msg}".strip()

    # ------------------------------------------------------------ open ---
    def open(self):
        _co_init()
        IID_IBaseFilter = _guid("56a86895-0ad4-11ce-b03a-0020af0ba770")
        IID_IKsControl = _guid("28F54685-06FD-11D2-B27A-00A0C9223196")
        tag = f"vid_{PT_VID:04x}&pid_{PT_PID:04x}"
        devices = list_video_devices()
        try:
            for index, name, path, mon in devices:
                if tag not in path.lower():
                    continue
                filt = c_void_p()
                if _call(mon, 8, None, None, byref(IID_IBaseFilter), byref(filt)) != S_OK:
                    continue
                self.ks = _qi(filt, IID_IKsControl)
                _release(filt)
                if self.ks:
                    self.index, self.product = index, name
                    break
        finally:
            for d in devices:
                _release(d[3])
        if self.ks is None:
            names = ", ".join(d[1] for d in devices) or "none"
            raise CameraError("PureThermal not found (USB 1e4e:0100). Check the cable / try "
                              f"another port. Video devices Windows sees: {names}.")
        try:
            self._open_capture()
        except Exception:
            self.close()
            raise
        self._init_radiometry()

    def _open_capture(self):
        cv2 = self.cv2
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise CameraError("could not open the camera. Another app (Camera, Teams, Zoom, "
                              "OBS, a browser tab...) may be using it.")
        y16 = cv2.VideoWriter_fourcc("Y", "1", "6", " ")
        cap.set(cv2.CAP_PROP_FOURCC, y16)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 160)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 120)
        cap.set(cv2.CAP_PROP_FOURCC, y16)
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        self.cap = cap
        # The first frame tells us the real size and whether Y16 took effect.
        for _ in range(20):
            ok, frame = cap.read()
            if ok and frame is not None:
                arr = self._to_counts(frame)
                if arr is None:
                    raise CameraError(
                        f"The camera did not switch to raw Y16 (got {frame.dtype} "
                        f"{tuple(frame.shape)}). Update opencv-python-headless "
                        f"(.venv\\Scripts\\pip install -U opencv-python-headless).")
                self.height, self.width = arr.shape
                return
            time.sleep(0.05)
        raise CameraError("The camera opened but sends no frames. Unplug it, plug it back in, "
                          "then press Reconnect.")

    def _to_counts(self, frame):
        """OpenCV returns Y16 as (h,w) uint16 or as a flat uint8 buffer, depending on version."""
        a = np.asarray(frame)
        if a.dtype == np.uint16 and a.ndim == 2:
            return a.copy()
        if a.dtype == np.uint8 and a.size in (160 * 120 * 2, 80 * 60 * 2):
            h, w = (120, 160) if a.size == 160 * 120 * 2 else (60, 80)
            return np.frombuffer(a.tobytes(), dtype="<u2").reshape(h, w).copy()
        return None

    # ------------------------------------------------------- streaming ---
    def start(self, callback):
        """callback(np.ndarray[uint16, (h,w)], t_seconds)"""
        self._user_cb = callback
        self.streaming = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        fails = 0
        while not self._stop.is_set():
            cap = self.cap
            if cap is None:
                break
            ok, frame = cap.read()
            if self._stop.is_set():
                break
            arr = self._to_counts(frame) if ok and frame is not None else None
            if arr is None:
                fails += 1
                self.bad_frames += 1
                if fails == 50:
                    self.stream_error = ("The camera is connected but sends no video. Unplug the "
                                         "PureThermal, plug it back in, then press Reconnect.")
                time.sleep(0.02)
                continue
            fails, self.stream_error = 0, None
            try:
                self._user_cb(arr, time.time())
            except Exception as e:
                print(f"[lepton] frame callback error: {e}", file=sys.stderr)

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        self._thread = None
        with self._lock:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
            if self.ks:
                try:
                    _co_init()
                    _release(self.ks)
                except Exception:
                    pass
                self.ks = None
        self.streaming = False

    # --------------------------------------------------------- control ---
    @staticmethod
    def _map(unit, ctrl):
        if ctrl > WINDOWS_MAX_CONTROLS:
            return unit + 0x10, ctrl - WINDOWS_MAX_CONTROLS
        return unit, ctrl

    def _ks(self, unit, ctrl, flags, buf, n):
        """One KSPROPERTY on the extension unit node. Returns (HRESULT, bytes returned)."""
        _co_init()
        node = self._node(unit)
        if node is None:
            return -1, 0
        prop = KSP_NODE(Set=GUID.from_buffer_copy(XU_GUID_BYTES[unit]), Id=ctrl,
                        Flags=flags | KSPROPERTY_TYPE_TOPOLOGY, NodeId=node)
        ret = c_ulong()
        hr = _call(self.ks, 3, byref(prop), sizeof(prop), buf, n, byref(ret),
                   argtypes=[c_void_p, c_ulong, c_void_p, c_ulong, c_void_p])
        self._last_hr = hr
        return hr, ret.value

    def _node(self, unit):
        """Find the topology node id of an extension unit (cached)."""
        if unit in self._nodes:
            return self._nodes[unit]
        if unit not in XU_GUID_BYTES:
            return None
        _co_init()
        found = None
        for node in range(32):
            prop = KSP_NODE(Set=GUID.from_buffer_copy(XU_GUID_BYTES[unit]), Id=1,
                            Flags=KSPROPERTY_TYPE_GET | KSPROPERTY_TYPE_TOPOLOGY, NodeId=node)
            ret = c_ulong()
            hr = _call(self.ks, 3, byref(prop), sizeof(prop), None, 0, byref(ret),
                       argtypes=[c_void_p, c_ulong, c_void_p, c_ulong, c_void_p])
            if hr in (S_OK, ERROR_MORE_DATA_HR, ERROR_INSUFFICIENT_BUFFER_HR) and ret.value:
                found = node
                break
        self._nodes[unit] = found
        return found

    def _ctrl_len(self, unit, ctrl):
        unit, ctrl = self._map(unit, ctrl)
        hr, n = self._ks(unit, ctrl, KSPROPERTY_TYPE_GET, None, 0)
        if hr in (S_OK, ERROR_MORE_DATA_HR, ERROR_INSUFFICIENT_BUFFER_HR) and n > 0:
            return n
        return -1

    def _get_ctrl(self, unit, ctrl, buf, n):
        unit, ctrl = self._map(unit, ctrl)
        hr, got = self._ks(unit, ctrl, KSPROPERTY_TYPE_GET, buf, n)
        return (got or n) if hr == S_OK else -1

    def _set_ctrl(self, unit, ctrl, buf, n):
        unit, ctrl = self._map(unit, ctrl)
        hr, _ = self._ks(unit, ctrl, KSPROPERTY_TYPE_SET, buf, n)
        return n if hr == S_OK else -1

    def read_info(self):
        info = super().read_info()
        info["backend"] = "windows"
        info.pop("y16_format_code", None)
        info["device_index"] = self.index
        return info
