"""
Simulated Lepton 3.5 so the app can be tried without hardware (--demo).

Renders a high-resolution synthetic scene, applies hand-held jitter
(sub-pixel random walk), integrates it onto a 160x120 pixel grid, adds
sensor noise and quantises to TLinear centikelvin - i.e. the same data a
radiometric Lepton delivers, with ground truth available for testing the
super-resolution engine.
"""
import threading
import time

import numpy as np

K0 = 273.15
UP = 4  # ground-truth oversampling


from superres import shift_bilinear as _shift_bilinear


def build_scene(w=160, h=120, up=UP, seed=3):
    """Return (base_K, heater_mask) at (h*up, w*up)."""
    H, W = h * up, w * up
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = 294.0 + 1.5 * (yy / H) + 0.4 * np.sin(xx / W * 3.0)

    # Mug (hot liquid) with handle
    cx, cy, r = W * 0.22, H * 0.55, H * 0.22
    body = (np.abs(xx - cx) < r) & (np.abs(yy - cy) < r * 1.2)
    img[body] = 333.0
    rim = body & (np.abs(yy - (cy - r * 1.2)) < up * 2)
    img[rim] = 338.0
    handle = (((xx - (cx + r)) ** 2 + (yy - cy) ** 2) < (r * 0.55) ** 2) & \
             (((xx - (cx + r)) ** 2 + (yy - cy) ** 2) > (r * 0.35) ** 2) & (xx > cx + r)
    img[handle] = 318.0

    # Resolution target: bar groups with periods 4, 3, 2.5, 2, 1.5 LR px
    x0 = W * 0.48
    for i, period_lr in enumerate([4.0, 3.0, 2.5, 2.0, 1.5]):
        p = period_lr * up
        y0 = H * 0.08 + i * H * 0.075
        band = (yy > y0) & (yy < y0 + H * 0.06) & (xx > x0) & (xx < x0 + p * 6)
        bars = band & (((xx - x0) % p) < p / 2)
        img[bars] = 312.0
    # Vertical bars too
    for i, period_lr in enumerate([4.0, 3.0, 2.0, 1.5]):
        p = period_lr * up
        xs = W * 0.80 + i * W * 0.045
        band = (xx > xs) & (xx < xs + W * 0.035) & (yy > H * 0.08) & (yy < H * 0.08 + p * 6)
        bars = band & (((yy - H * 0.08) % p) < p / 2)
        img[bars] = 310.0

    # "Circuit board": traces + a few hot chips
    pcb = (xx > W * 0.48) & (xx < W * 0.96) & (yy > H * 0.52) & (yy < H * 0.94)
    img[pcb] = 300.0
    for k in range(14):
        ty = H * 0.55 + k * H * 0.028
        tr = pcb & (np.abs(yy - ty) < up * 0.6)
        img[tr] = 303.0
    chips = [(0.60, 0.70, 0.05, 318.0), (0.78, 0.64, 0.035, 326.0), (0.88, 0.84, 0.025, 345.0)]
    for fx, fy, s, t in chips:
        m = (np.abs(xx - W * fx) < W * s) & (np.abs(yy - H * fy) < W * s * 0.8)
        img[m] = t
    # tiny hot via (sub-pixel feature)
    m = ((xx - W * 0.70) ** 2 + (yy - H * 0.86) ** 2) < (up * 0.9) ** 2
    img[m] = 360.0

    heater = (((xx - W * 0.22) ** 2 + (yy - H * 0.13) ** 2) < (H * 0.07) ** 2).astype(np.float32)

    # gentle optical blur (~0.25 LR px)
    from superres import blur_fast
    img = blur_fast(img.astype(np.float64), 1.0)
    img += rng.normal(0, 0.05, img.shape)
    return img.astype(np.float32), heater


def render(scene, dy, dx, up=UP):
    """Integrate the HR scene onto the LR grid, scene shifted by (dy,dx) LR px."""
    s = _shift_bilinear(scene, dy * up, dx * up)
    H, W = s.shape
    return s.reshape(H // up, up, W // up, up).mean(axis=(1, 3))


class DemoCamera:
    is_demo = True
    name = "Simulated Lepton 3.5 (demo)"

    def __init__(self, fps=8.7, jitter=True):
        self.width, self.height = 160, 120
        self.fps = fps
        self.scale = 0.01
        self.radiometric = True
        self.jitter = jitter
        self.scene, self.heater = build_scene()
        self._stop = threading.Event()
        self._thread = None
        self.pos = np.zeros(2)
        self.vel = np.zeros(2)
        self.rng = np.random.default_rng()
        self.settings = {
            "gain_mode": "high", "shutter_mode": "auto", "video_freeze_during_ffc": True,
            "ffc_period_s": 180.0, "ffc_temp_delta_K": 1.5,
            "radiometry_enabled": True, "tlinear_enabled": True, "tlinear_resolution": 0.01,
            "emissivity": 1.0, "t_bkg_C": 22.0, "tau_window": 1.0, "t_window_C": 22.0,
            "tau_atm": 1.0, "t_atm_C": 22.0, "refl_window": 0.0, "t_refl_C": 22.0,
        }
        self._ffc_until = 0.0
        self._last_ffc = time.time()
        self._frozen = None
        self.last_shift = (0.0, 0.0)

    def open(self):
        pass

    def start(self, callback):
        self._cb = callback
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()

    def _loop(self):
        period = 1.0 / self.fps
        t_next = time.time()
        t0 = time.time()
        while not self._stop.is_set():
            now = time.time()
            if self.jitter:
                # damped random walk ~ hand tremor, ~0.5-1.5 px
                self.vel = 0.75 * self.vel + self.rng.normal(0, 0.18, 2) - 0.05 * self.pos
                self.pos += self.vel
            else:
                self.pos[:] = 0
            dy, dx = self.pos
            self.last_shift = (float(dy), float(dx))
            t = now - t0
            heater_C = 45 + 25 * np.sin(t / 20.0) + 10 * np.sin(t / 3.3)
            scene = self.scene + self.heater * (heater_C + K0 - 294.0)
            lr = render(scene, dy, dx)
            # emissivity correction is a no-op here; noise ~ 50 mK NETD
            lr = lr + self.rng.normal(0, 0.05, lr.shape)
            if self.settings["gain_mode"] == "low":
                lr = lr + self.rng.normal(0, 0.15, lr.shape)
            sh = self.settings["shutter_mode"]
            if sh == "auto" and now - self._last_ffc > self.settings["ffc_period_s"]:
                self.run_ffc()
            if now < self._ffc_until:
                if self._frozen is None:
                    self._frozen = lr
                lr = self._frozen
            else:
                self._frozen = None
            counts = np.clip(np.round(lr / self.scale), 0, 65535).astype(np.uint16)
            if not self.settings["tlinear_enabled"]:
                counts = np.clip(8192 + (lr - 300) * 30, 0, 16383).astype(np.uint16)
            self._cb(counts, now)
            t_next += period
            time.sleep(max(0.0, t_next - time.time()))

    def read_settings(self):
        s = dict(self.settings)
        s["time_since_ffc_s"] = time.time() - self._last_ffc
        return s

    def read_status(self):
        return {"fpa_C": 31.2 + 0.3 * np.sin(time.time() / 60), "aux_C": 29.8,
                "ffc_status": "busy" if time.time() < self._ffc_until else "ready",
                "camera_status": "ready",
                "time_since_ffc_s": time.time() - self._last_ffc}

    def read_info(self):
        return {"backend": "demo", "serial": "DEMO-0000", "part_number": "500-0771-01 (simulated)",
                "firmware": "simulated"}

    def apply_setting(self, key, value):
        if key not in self.settings:
            raise ValueError(f"unknown setting {key}")
        cur = self.settings[key]
        if isinstance(cur, bool):
            value = bool(value)
        elif isinstance(cur, float):
            value = float(value)
        self.settings[key] = value
        if key == "gain_mode" and value in ("low", "auto"):
            self.settings["tlinear_resolution"] = 0.1
        self.scale = self.settings["tlinear_resolution"]
        self.radiometric = self.settings["tlinear_enabled"]

    def run_ffc(self):
        self._last_ffc = time.time()
        self._ffc_until = time.time() + 0.6
