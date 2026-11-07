"""
Linux backend: PureThermal / FLIR Lepton through the kernel's uvcvideo driver (V4L2).

No libuvc, no compiling, no udev rule: the camera stays attached to the normal
Linux UVC driver.

* Frames: the /dev/videoN capture node, 160x120 'Y16 ', mmap streaming.
* Lepton SDK commands: the UVCIOC_CTRL_QUERY ioctl, which sends UVC extension
  unit requests by unit id + control selector. That's exactly what libuvc
  does, so all the command logic is inherited from LeptonUVC unchanged.

Access to /dev/video* needs a desktop session (Ubuntu grants it to the
logged-in user) or membership of the `video` group.
"""
import ctypes
import errno
import fcntl
import glob
import mmap
import os
import select
import sys
import threading
import time

import numpy as np

from lepton_uvc import PT_PID, PT_VID, CameraError, LeptonUVC


# --------------------------------------------------------------- ioctls ---
def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | (ord(t) << 8) | nr


def _IOR(t, nr, st):
    return _IOC(2, t, nr, ctypes.sizeof(st))


def _IOW(t, nr, st):
    return _IOC(1, t, nr, ctypes.sizeof(st))


def _IOWR(t, nr, st):
    return _IOC(3, t, nr, ctypes.sizeof(st))


def fourcc(s):
    return ord(s[0]) | (ord(s[1]) << 8) | (ord(s[2]) << 16) | (ord(s[3]) << 24)


V4L2_PIX_FMT_Y16 = fourcc("Y16 ")
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 1
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000


class v4l2_capability(ctypes.Structure):
    _fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
                ("bus_info", ctypes.c_char * 32), ("version", ctypes.c_uint32),
                ("capabilities", ctypes.c_uint32), ("device_caps", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 3)]


class v4l2_fmtdesc(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("description", ctypes.c_char * 32),
                ("pixelformat", ctypes.c_uint32), ("mbus_code", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 3)]


class v4l2_pix_format(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "width", "height", "pixelformat", "field", "bytesperline", "sizeimage",
        "colorspace", "priv", "flags", "ycbcr_enc", "quantization", "xfer_func")]


class _fmt_union(ctypes.Union):
    # The kernel union also holds struct v4l2_window (with pointers): 200 bytes, 8-aligned.
    _fields_ = [("pix", v4l2_pix_format), ("raw", ctypes.c_uint8 * 200), ("_align", ctypes.c_void_p)]


class v4l2_format(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("fmt", _fmt_union)]


class v4l2_requestbuffers(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("memory", ctypes.c_uint32), ("capabilities", ctypes.c_uint32),
                ("flags", ctypes.c_uint8), ("reserved", ctypes.c_uint8 * 3)]


class timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class v4l2_timecode(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("frames", ctypes.c_uint8), ("seconds", ctypes.c_uint8),
                ("minutes", ctypes.c_uint8), ("hours", ctypes.c_uint8),
                ("userbits", ctypes.c_uint8 * 4)]


class _buf_m(ctypes.Union):
    _fields_ = [("offset", ctypes.c_uint32), ("userptr", ctypes.c_ulong),
                ("planes", ctypes.c_void_p), ("fd", ctypes.c_int32)]


class v4l2_buffer(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("bytesused", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("field", ctypes.c_uint32), ("timestamp", timeval),
                ("timecode", v4l2_timecode), ("sequence", ctypes.c_uint32),
                ("memory", ctypes.c_uint32), ("m", _buf_m),
                ("length", ctypes.c_uint32), ("reserved2", ctypes.c_uint32),
                ("request_fd", ctypes.c_int32)]


class uvc_xu_control_query(ctypes.Structure):
    _fields_ = [("unit", ctypes.c_uint8), ("selector", ctypes.c_uint8),
                ("query", ctypes.c_uint8), ("size", ctypes.c_uint16),
                ("data", ctypes.c_void_p)]


VIDIOC_QUERYCAP = _IOR("V", 0, v4l2_capability)
VIDIOC_ENUM_FMT = _IOWR("V", 2, v4l2_fmtdesc)
VIDIOC_S_FMT = _IOWR("V", 5, v4l2_format)
VIDIOC_REQBUFS = _IOWR("V", 8, v4l2_requestbuffers)
VIDIOC_QUERYBUF = _IOWR("V", 9, v4l2_buffer)
VIDIOC_QBUF = _IOWR("V", 15, v4l2_buffer)
VIDIOC_DQBUF = _IOWR("V", 17, v4l2_buffer)
VIDIOC_STREAMON = _IOW("V", 18, ctypes.c_int)
VIDIOC_STREAMOFF = _IOW("V", 19, ctypes.c_int)
UVCIOC_CTRL_QUERY = _IOWR("u", 0x21, uvc_xu_control_query)

UVC_SET_CUR, UVC_GET_CUR, UVC_GET_LEN = 0x01, 0x81, 0x85
NUM_BUFFERS = 4


def _ioctl(fd, req, arg):
    while True:
        try:
            return fcntl.ioctl(fd, req, arg)
        except InterruptedError:
            continue


def _sysfs_usb_dir(video_name):
    """Walk up from /sys/class/video4linux/videoN/device to the USB device dir."""
    d = os.path.realpath(f"/sys/class/video4linux/{video_name}/device")
    while d and d != "/":
        if os.path.exists(os.path.join(d, "idVendor")):
            return d
        d = os.path.dirname(d)
    return None


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def find_purethermal_nodes():
    """[(/dev/videoN, usb sysfs dir)] for PureThermal boards, lowest N first."""
    out = []
    for p in sorted(glob.glob("/sys/class/video4linux/video*"),
                    key=lambda s: int(s.rsplit("video", 1)[1] or 0)):
        name = os.path.basename(p)
        usb = _sysfs_usb_dir(name)
        if not usb:
            continue
        if (_read(os.path.join(usb, "idVendor")).lower() == f"{PT_VID:04x}"
                and _read(os.path.join(usb, "idProduct")).lower() == f"{PT_PID:04x}"):
            out.append((f"/dev/{name}", usb))
    return out


class LeptonV4L2(LeptonUVC):
    """Real camera on Linux via V4L2. Same interface as LeptonUVC."""

    name = "PureThermal / FLIR Lepton (V4L2)"
    stream_error = None

    def __init__(self):
        # Deliberately not calling LeptonUVC.__init__: it would load libuvc.
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
        self.fd = None
        self.path = None
        self._bufs = []
        self._last_errno = 0
        self._stop = threading.Event()
        self._thread = None

    def _err(self, code=None):
        e = self._last_errno or (-code if isinstance(code, int) and code < 0 else 0)
        return os.strerror(e) if e else "unknown error"

    # ------------------------------------------------------------ open ---
    def open(self):
        nodes = find_purethermal_nodes()
        if not nodes:
            raise CameraError("PureThermal not found (USB 1e4e:0100). Check the cable / try "
                              "another port. `lsusb | grep 1e4e` should list it.")
        errors = []
        for path, usb in nodes:
            try:
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            except PermissionError:
                raise CameraError(
                    f"No permission to open {path}. Add yourself to the video group "
                    f"(sudo usermod -aG video $USER, then log out and back in), or run "
                    f"linux/setup-ubuntu.sh.")
            except OSError as e:
                errors.append(f"{path}: {e.strerror}")
                continue
            if self._is_y16_capture(fd):
                self.fd, self.path = fd, path
                self.product = _read(os.path.join(usb, "product"))
                break
            os.close(fd)
        if self.fd is None:
            raise CameraError("PureThermal found, but no video node offers Y16. "
                              + "; ".join(errors))
        try:
            self._set_format()
        except Exception:
            self.close()
            raise
        self._init_radiometry()

    @staticmethod
    def _is_y16_capture(fd):
        cap = v4l2_capability()
        try:
            _ioctl(fd, VIDIOC_QUERYCAP, cap)
        except OSError:
            return False
        caps = cap.device_caps if cap.capabilities & V4L2_CAP_DEVICE_CAPS else cap.capabilities
        if not caps & V4L2_CAP_VIDEO_CAPTURE:
            return False
        i = 0
        while True:
            d = v4l2_fmtdesc(index=i, type=V4L2_BUF_TYPE_VIDEO_CAPTURE)
            try:
                _ioctl(fd, VIDIOC_ENUM_FMT, d)
            except OSError:
                return False
            if d.pixelformat == V4L2_PIX_FMT_Y16:
                return True
            i += 1

    def _set_format(self):
        f = v4l2_format(type=V4L2_BUF_TYPE_VIDEO_CAPTURE)
        f.fmt.pix.width, f.fmt.pix.height = 160, 120
        f.fmt.pix.pixelformat = V4L2_PIX_FMT_Y16
        f.fmt.pix.field = V4L2_FIELD_NONE
        try:
            _ioctl(self.fd, VIDIOC_S_FMT, f)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise CameraError("The camera is busy: another app (Cheese, a browser tab, "
                                  "OBS, Zoom...) is using it.")
            raise CameraError(f"could not set Y16 format: {e.strerror}")
        if f.fmt.pix.pixelformat != V4L2_PIX_FMT_Y16:
            raise CameraError("The driver refused the Y16 format.")
        self.width, self.height = f.fmt.pix.width, f.fmt.pix.height

    # ------------------------------------------------------- streaming ---
    def _stream_on(self):
        req = v4l2_requestbuffers(count=NUM_BUFFERS, type=V4L2_BUF_TYPE_VIDEO_CAPTURE,
                                  memory=V4L2_MEMORY_MMAP)
        try:
            _ioctl(self.fd, VIDIOC_REQBUFS, req)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise CameraError("The camera is busy: another app is using it.")
            raise CameraError(f"VIDIOC_REQBUFS failed: {e.strerror}")
        self._bufs = []
        for i in range(req.count):
            b = v4l2_buffer(index=i, type=V4L2_BUF_TYPE_VIDEO_CAPTURE, memory=V4L2_MEMORY_MMAP)
            _ioctl(self.fd, VIDIOC_QUERYBUF, b)
            self._bufs.append(mmap.mmap(self.fd, b.length, mmap.MAP_SHARED,
                                        mmap.PROT_READ | mmap.PROT_WRITE, offset=b.m.offset))
            _ioctl(self.fd, VIDIOC_QBUF, b)
        _ioctl(self.fd, VIDIOC_STREAMON, ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE))

    def _stream_off(self):
        try:
            _ioctl(self.fd, VIDIOC_STREAMOFF, ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE))
        except OSError:
            pass
        for m in self._bufs:
            try:
                m.close()
            except Exception:
                pass
        self._bufs = []
        try:   # release the buffers so the format can change / another app can use it
            _ioctl(self.fd, VIDIOC_REQBUFS, v4l2_requestbuffers(
                count=0, type=V4L2_BUF_TYPE_VIDEO_CAPTURE, memory=V4L2_MEMORY_MMAP))
        except OSError:
            pass

    def start(self, callback):
        """callback(np.ndarray[uint16, (h,w)], t_seconds)"""
        self._user_cb = callback
        with self._lock:
            self._stream_on()
        self.streaming = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        expected = self.width * self.height * 2
        last_frame, restarts = time.time(), 0
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if self._stop.is_set():
                break
            if not r:
                # PureThermal firmware <= 1.3.0 can come up silent after an unclean stop;
                # a stream restart usually clears it.
                if time.time() - last_frame > 2.5:
                    if restarts < 2:
                        restarts += 1
                        print(f"[lepton] no frames; restarting stream (attempt {restarts})",
                              file=sys.stderr)
                        with self._lock:
                            self._stream_off()
                            try:
                                self._stream_on()
                            except Exception as e:
                                print(f"[lepton] restart failed: {e}", file=sys.stderr)
                    elif not self.stream_error:
                        self.stream_error = ("The camera is connected but sends no video. Unplug "
                                             "the PureThermal, plug it back in, then press Reconnect.")
                    last_frame = time.time()
                continue
            b = v4l2_buffer(type=V4L2_BUF_TYPE_VIDEO_CAPTURE, memory=V4L2_MEMORY_MMAP)
            try:
                _ioctl(self.fd, VIDIOC_DQBUF, b)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    continue
                if e.errno == errno.ENODEV:
                    self.stream_error = "The camera was unplugged. Plug it back in, then press Reconnect."
                    break
                print(f"[lepton] DQBUF failed: {e.strerror}", file=sys.stderr)
                time.sleep(0.05)
                continue
            try:
                if b.bytesused == expected and b.index < len(self._bufs):
                    data = self._bufs[b.index][:expected]
                    arr = np.frombuffer(data, dtype="<u2").reshape(self.height, self.width).copy()
                    last_frame, restarts, self.stream_error = time.time(), 0, None
                    try:
                        self._user_cb(arr, last_frame)
                    except Exception as e:
                        print(f"[lepton] frame callback error: {e}", file=sys.stderr)
                else:
                    self.bad_frames += 1
            finally:
                try:
                    _ioctl(self.fd, VIDIOC_QBUF, b)
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        self._thread = None
        with self._lock:
            if self.fd is not None:
                if self.streaming:
                    self._stream_off()
                try:
                    os.close(self.fd)
                except OSError:
                    pass
            self.fd = None
        self.streaming = False

    # --------------------------------------------------------- control ---
    def _xu(self, unit, ctrl, query, buf, n):
        q = uvc_xu_control_query(unit=unit, selector=ctrl, query=query, size=n,
                                 data=ctypes.cast(buf, ctypes.c_void_p))
        try:
            _ioctl(self.fd, UVCIOC_CTRL_QUERY, q)
            self._last_errno = 0
            return n
        except OSError as e:
            self._last_errno = e.errno or errno.EIO
            return -self._last_errno

    def _ctrl_len(self, unit, ctrl):
        buf = (ctypes.c_uint8 * 2)()
        r = self._xu(unit, ctrl, UVC_GET_LEN, buf, 2)
        return r if r < 0 else buf[0] | (buf[1] << 8)

    def _get_ctrl(self, unit, ctrl, buf, n):
        return self._xu(unit, ctrl, UVC_GET_CUR, buf, n)

    def _set_ctrl(self, unit, ctrl, buf, n):
        return self._xu(unit, ctrl, UVC_SET_CUR, buf, n)

    def read_info(self):
        info = super().read_info()
        info["backend"] = "v4l2"
        info["device"] = self.path
        info.pop("y16_format_code", None)
        return info
