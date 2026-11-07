"""
Offline tests: no camera needed. Run with  python -m unittest discover -s tests -v
"""
import ctypes
import os
import struct
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import lepton_uvc  # noqa: E402
from lepton_uvc import LeptonUVC, _addr, board_firmware_notice  # noqa: E402


class FakeLepton(LeptonUVC):
    """LeptonUVC with an in-memory control store instead of USB: exercises the
    shared Lepton command layer every real backend inherits."""

    def __init__(self):
        import threading
        self._lock = threading.RLock()
        self.scale, self.radiometric = 0.01, True
        self.product = "PureThermal (fw:v1.3.0)"
        self.firmware_notice = None
        self.store = {}
        self.lens = {}

    def _err(self, code=None):
        return "fake error"

    def _ctrl_len(self, unit, ctrl):
        return self.lens.get((unit, ctrl), 4)

    def _get_ctrl(self, unit, ctrl, buf, n):
        data = self.store.get((unit, ctrl), b"\x00" * n)[:n]
        ctypes.memmove(buf, data, len(data))
        return n

    def _set_ctrl(self, unit, ctrl, buf, n):
        self.store[(unit, ctrl)] = bytes(buf)[:n]
        return n


class TestLeptonCommands(unittest.TestCase):
    def test_addressing(self):
        self.assertEqual(_addr(lepton_uvc.SYS_GAIN_MODE), (6, 19))
        self.assertEqual(_addr(lepton_uvc.RAD_FLUX_LINEAR_PARAMS), (5, 48))
        self.assertEqual(_addr(lepton_uvc.OEM_PART_NUMBER), (4, 8))
        self.assertEqual(_addr(lepton_uvc.AGC_ENABLE), (3, 1))

    def test_gain_mode_round_trip(self):
        cam = FakeLepton()
        cam.apply_setting("gain_mode", "low")
        self.assertEqual(cam.read_settings()["gain_mode"], "low")
        # low gain forces 0.1 K TLinear resolution
        self.assertEqual(cam.scale, 0.1)

    def test_flux_params(self):
        cam = FakeLepton()
        cam.lens[_addr(lepton_uvc.RAD_FLUX_LINEAR_PARAMS)] = 16
        cam.set_raw(lepton_uvc.RAD_FLUX_LINEAR_PARAMS,
                    struct.pack("<8H", 8192, 29515, 8192, 29515, 8192, 29515, 0, 29515))
        cam.apply_setting("emissivity", 0.95)
        cam.apply_setting("t_bkg_C", 30.0)
        s = cam.read_settings()
        self.assertAlmostEqual(s["emissivity"], 0.95, places=3)
        self.assertAlmostEqual(s["t_bkg_C"], 30.0, places=1)

    def test_firmware_notice(self):
        self.assertEqual(board_firmware_notice("PureThermal (fw:v1.3.1)"), ("v1.3.1", None))
        ver, notice = board_firmware_notice("PureThermal (fw:v1.2.2)")
        self.assertEqual(ver, "v1.2.2")
        self.assertEqual(notice["latest"], "v1.3.1")
        self.assertEqual(board_firmware_notice("PureThermal"), (None, None))


class TestWindowsBackend(unittest.TestCase):
    def setUp(self):
        import lepton_windows
        self.w = lepton_windows

    def test_rad2_mapping(self):
        m = self.w.LeptonWindows._map
        self.assertEqual(m(5, 5), (5, 5))
        self.assertEqual(m(*_addr(lepton_uvc.RAD_FLUX_LINEAR_PARAMS)), (0x15, 17))
        self.assertEqual(m(*_addr(lepton_uvc.RAD_TLINEAR_RESOLUTION)), (0x15, 19))
        for unit, ctrl in [m(5, c) for c in range(32, 51)]:
            self.assertIn(unit, self.w.XU_GUID_BYTES)

    def test_struct_layout(self):
        self.assertEqual(ctypes.sizeof(self.w.GUID), 16)
        self.assertEqual(ctypes.sizeof(self.w.KSP_NODE), 32)
        for b in self.w.XU_GUID_BYTES.values():
            self.assertEqual(len(b), 16)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_com_enumeration(self):
        """Runs the real DirectShow/COM device enumeration (no camera on CI)."""
        devices = self.w.list_video_devices()
        for _, name, path, mon in devices:
            self.assertIsInstance(name, str)
            self.w._release(mon)
        with self.assertRaises(lepton_uvc.CameraError) as cm:
            self.w.LeptonWindows().open()
        self.assertIn("PureThermal not found", str(cm.exception))

    def test_frame_conversion(self):
        cam = self.w.LeptonWindows.__new__(self.w.LeptonWindows)
        raw = np.arange(19200, dtype="<u2").reshape(120, 160)
        np.testing.assert_array_equal(cam._to_counts(raw), raw)
        flat = np.frombuffer(raw.tobytes(), np.uint8).reshape(1, -1)
        np.testing.assert_array_equal(cam._to_counts(flat), raw)
        self.assertIsNone(cam._to_counts(np.zeros((120, 160, 3), np.uint8)))


@unittest.skipIf(sys.platform == "win32" or ctypes.sizeof(ctypes.c_long) != 8,
                 "V4L2 ioctl numbers are checked for 64-bit Linux ABI")
class TestV4L2Backend(unittest.TestCase):
    def test_ioctl_numbers(self):
        import lepton_v4l2 as v
        expected = {
            "VIDIOC_QUERYCAP": 0x80685600, "VIDIOC_ENUM_FMT": 0xC0405602,
            "VIDIOC_S_FMT": 0xC0D05605, "VIDIOC_REQBUFS": 0xC0145608,
            "VIDIOC_QUERYBUF": 0xC0585609, "VIDIOC_QBUF": 0xC058560F,
            "VIDIOC_DQBUF": 0xC0585611, "VIDIOC_STREAMON": 0x40045612,
            "VIDIOC_STREAMOFF": 0x40045613, "UVCIOC_CTRL_QUERY": 0xC0107521,
        }
        for name, val in expected.items():
            self.assertEqual(getattr(v, name), val, name)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_no_camera_message(self):
        import lepton_v4l2 as v
        if v.find_purethermal_nodes():
            self.skipTest("a PureThermal is attached")
        with self.assertRaises(lepton_uvc.CameraError) as cm:
            v.LeptonV4L2().open()
        self.assertIn("PureThermal not found", str(cm.exception))


class TestDemoAndSuperres(unittest.TestCase):
    def test_demo_camera_frames(self):
        from demo_camera import DemoCamera
        frames = []
        cam = DemoCamera(fps=30)
        cam.open()
        cam.start(lambda a, t: frames.append(a))
        deadline = time.time() + 5
        while len(frames) < 3 and time.time() < deadline:
            time.sleep(0.05)
        cam.close()
        self.assertGreaterEqual(len(frames), 3)
        self.assertEqual(frames[0].shape, (120, 160))
        self.assertEqual(frames[0].dtype, np.uint16)
        kelvin = frames[0] * 0.01
        self.assertTrue(250 < float(np.median(kelvin)) < 330)

    def test_register_known_shift(self):
        import superres
        from demo_camera import build_scene, render
        scene, _ = build_scene()
        ref = render(scene, 0.0, 0.0).astype(float)
        mov = render(scene, 0.3, -0.45).astype(float)
        win = np.outer(np.hanning(ref.shape[0]), np.hanning(ref.shape[1]))
        d, q = superres.register(np.fft.fft2(superres._prep(ref, win)), mov, win)
        self.assertLess(abs(abs(d[0]) - 0.3), 0.1)
        self.assertLess(abs(abs(d[1]) - 0.45), 0.1)


if __name__ == "__main__":
    unittest.main()
