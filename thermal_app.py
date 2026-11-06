#!/usr/bin/env python3
"""
Thermal Studio - viewer / logger / super-resolution for FLIR Lepton 3.5 on a
PureThermal board.

    python3 thermal_app.py               # real camera (macOS: IOKit backend, no sudo)
    python3 thermal_app.py --demo        # simulated camera

Then open http://127.0.0.1:8765 (opened automatically).
"""
import argparse
import collections
import csv
import datetime as dt
import io
import itertools
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import superres  # noqa: E402

K0 = 273.15
HISTORY_LEN = 150_000            # samples per series (~4.8 h at 8.7 Hz)


# ------------------------------------------------------------------ utils ---
def fix_owner(path):
    """When run under sudo, hand created files back to the real user."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        try:
            os.chown(path, int(uid), int(gid))
        except OSError:
            pass


def write_png16(path, arr_u16):
    """Minimal 16-bit greyscale PNG writer (no Pillow needed)."""
    h, w = arr_u16.shape
    raw = b"".join(b"\x00" + arr_u16[y].astype(">u2").tobytes() for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def convert(K, unit):
    if unit == "F":
        return (K - K0) * 9 / 5 + 32
    if unit == "K":
        return K
    return K - K0


def stamp():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# ------------------------------------------------------------------- core ---
class Studio:
    def __init__(self, out_dir, demo=False, demo_jitter=True):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        fix_owner(out_dir)
        self.cam = None
        self.cam_error = None
        self.cam_info = {}
        self.demo_jitter = demo_jitter
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.q = queue.Queue(maxsize=8)
        self.seq = 0
        self.latest = None               # dict(seq,t,counts,scale,radiometric)
        self.fps = 0.0
        self._fps_t = collections.deque(maxlen=30)

        self.points = []                 # dict(id,x,y,r,label,color)
        self.next_pid = 1
        self.builtin = {"max": False, "min": False, "mean": False}
        self.history = {}                # sid -> (deque t, deque K)
        self.logger = None
        self.recorder = None
        self.sr = {"state": "idle", "progress": 0.0, "message": "", "id": 0}
        self._sr_frames = None
        self._sr_result = None

        threading.Thread(target=self._process_loop, daemon=True).start()
        self.connect(demo)

    # ---------------------------------------------------------- camera ---
    def connect(self, demo=False):
        with self.lock:
            if self.cam is not None:
                try:
                    self.cam.close()
                except Exception:
                    pass
                self.cam = None
            self.cam_error = None
            try:
                if demo:
                    from demo_camera import DemoCamera
                    cam = DemoCamera(jitter=self.demo_jitter)
                elif sys.platform == "darwin" and os.environ.get("THERMAL_BACKEND", "iokit") == "iokit":
                    # macOS: IOKit backend, no sudo (libuvc's driver detach caused kernel panics)
                    from lepton_iokit import LeptonIOKit
                    cam = LeptonIOKit()
                else:
                    from lepton_uvc import LeptonUVC
                    cam = LeptonUVC()
                cam.open()
                cam.start(self._on_frame)
                self.cam = cam
                try:
                    self.cam_info = cam.read_info()
                except Exception:
                    self.cam_info = {}
                print(f"[studio] connected: {cam.name}")
            except Exception as e:
                self.cam_error = str(e)
                print(f"[studio] camera error: {e}", file=sys.stderr)

    def _on_frame(self, counts, t):
        try:
            self.q.put_nowait((counts, t))
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.q.put_nowait((counts, t))

    # ------------------------------------------------------- per-frame ---
    def _process_loop(self):
        while True:
            counts, t = self.q.get()
            try:
                self._process(counts, t)
            except Exception:
                traceback.print_exc()

    def _process(self, counts, t):
        cam = self.cam
        if cam is None:
            return
        scale = cam.scale if cam.radiometric else 1.0
        radiometric = bool(cam.radiometric)
        K = counts.astype(np.float32) * scale if radiometric else counts.astype(np.float32)
        self._fps_t.append(t)
        if len(self._fps_t) > 2:
            self.fps = (len(self._fps_t) - 1) / max(1e-6, self._fps_t[-1] - self._fps_t[0])

        with self.cond:
            self.seq += 1
            self.latest = {"seq": self.seq, "t": t, "counts": counts, "scale": scale,
                           "radiometric": radiometric, "K": K}
            self.cond.notify_all()

        with self.lock:
            values = self._measure(K)
            for sid, v in values.items():
                h = self.history.get(sid)
                if h is None:
                    h = self.history[sid] = (collections.deque(maxlen=HISTORY_LEN),
                                             collections.deque(maxlen=HISTORY_LEN))
                h[0].append(t)
                h[1].append(v)
            if self.logger:
                self.logger.add(t, values)
            if self.recorder:
                self.recorder.add(counts, t)
            if self._sr_frames is not None and self.sr["state"] == "capturing":
                self._sr_frames.append(K.astype(np.float64))
                n = self.sr["params"]["frames"]
                self.sr["progress"] = len(self._sr_frames) / n * 0.999
                self.sr["message"] = f"capturing {len(self._sr_frames)}/{n}"
                if len(self._sr_frames) >= n:
                    frames, self._sr_frames = self._sr_frames, None
                    self.sr["state"] = "processing"
                    threading.Thread(target=self._sr_run, args=(frames,), daemon=True).start()

    def _measure(self, K):
        out = {}
        h, w = K.shape
        for p in self.points:
            r = int(p["r"])
            y0, y1 = max(0, p["y"] - r), min(h, p["y"] + r + 1)
            x0, x1 = max(0, p["x"] - r), min(w, p["x"] + r + 1)
            out[f"p{p['id']}"] = float(K[y0:y1, x0:x1].mean())
        if self.builtin["max"]:
            out["max"] = float(K.max())
        if self.builtin["min"]:
            out["min"] = float(K.min())
        if self.builtin["mean"]:
            out["mean"] = float(K.mean())
        return out

    def series_meta(self):
        meta = [{"id": f"p{p['id']}", "label": p["label"], "color": p["color"],
                 "x": p["x"], "y": p["y"], "r": p["r"]} for p in self.points]
        colors = {"max": "#ff5a4e", "min": "#4ea8ff", "mean": "#c8c8c8"}
        for k, on in self.builtin.items():
            if on:
                meta.append({"id": k, "label": f"Frame {k}", "color": colors[k]})
        return meta

    def points_changed(self):
        if self.logger:
            self.logger.rotate(self.series_meta())

    def export_history(self, window_s=0.0, unit="C"):
        """Write the plotted history (all current series) to a CSV in out_dir.

        window_s > 0 keeps only the last window_s seconds, like the chart.
        Returns (path, csv_text, rows). All series are sampled on the same
        frames, so rows are aligned by timestamp; a series that started later
        (point added mid-session) is blank before it existed.
        """
        with self.lock:
            series = self.series_meta()
            hist = {s["id"]: (list(self.history[s["id"]][0]), list(self.history[s["id"]][1]))
                    for s in series if s["id"] in self.history}
        since = (time.time() - float(window_s)) if window_s and float(window_s) > 0 else -1.0
        rows = {}
        for col, s in enumerate(series):
            ts, vs = hist.get(s["id"], ([], []))
            for t, v in zip(ts, vs):
                if t >= since:
                    rows.setdefault(t, [None] * len(series))[col] = v
        buf = io.StringIO()
        w = csv.writer(buf)
        write_series_header(w, series, unit)
        for t in sorted(rows):
            w.writerow([dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds"), f"{t:.3f}"] +
                       ["" if v is None else f"{convert(v, unit):.3f}" for v in rows[t]])
        text = buf.getvalue()
        base = os.path.join(self.out_dir, f"chart_{stamp()}")
        path, n = base + ".csv", 2
        while os.path.exists(path):            # several exports within one second
            path, n = f"{base}_{n}.csv", n + 1
        with open(path, "w", newline="") as f:
            f.write(text)
        fix_owner(path)
        return path, text, len(rows)

    # ------------------------------------------------------- snapshots ---
    def snapshot(self, unit="C"):
        lt = self.latest
        if lt is None:
            raise RuntimeError("no frame yet")
        base = os.path.join(self.out_dir, f"snapshot_{stamp()}")
        return self._save_radiometric(base, lt["K"].astype(np.float64), unit, lt["radiometric"],
                                      extra={"scale_K_per_count": lt["scale"]})

    def _save_radiometric(self, base, K, unit, radiometric=True, extra=None):
        paths = []
        np.save(base + "_kelvin.npy", K.astype(np.float32))
        paths.append(base + "_kelvin.npy")
        with open(base + f"_{unit}.csv", "w", newline="") as f:
            wr = csv.writer(f)
            for row in convert(K, unit) if radiometric else K:
                wr.writerow([f"{v:.2f}" for v in row])
        paths.append(base + f"_{unit}.csv")
        if radiometric:
            write_png16(base + "_centikelvin.png", np.clip(np.round(K * 100), 0, 65535).astype(np.uint16))
            paths.append(base + "_centikelvin.png")
        meta = {"created": dt.datetime.now().isoformat(), "shape": list(K.shape),
                "radiometric": radiometric, "camera": self.cam.name if self.cam else None,
                "camera_info": self.cam_info, "min_K": float(K.min()), "max_K": float(K.max())}
        if self.cam:
            try:
                meta["settings"] = self.cam.read_settings()
            except Exception:
                pass
        meta.update(extra or {})
        with open(base + "_meta.json", "w") as f:
            json.dump(meta, f, indent=2, default=str)
        paths.append(base + "_meta.json")
        for p in paths:
            fix_owner(p)
        return paths

    # ------------------------------------------------- super-resolution ---
    def sr_start(self, params):
        with self.lock:
            if self.sr["state"] in ("capturing", "processing"):
                raise RuntimeError("a capture is already running")
            if self.cam is None:
                raise RuntimeError("no camera")
            p = {"mode": params.get("mode", "superres"),
                 "frames": int(max(4, min(400, int(params.get("frames", 48))))),
                 "scale": int(max(2, min(4, int(params.get("scale", 3))))),
                 "iterations": int(max(0, min(200, int(params.get("iterations", 30))))),
                 "align": bool(params.get("align", False)),
                 "ffc_first": bool(params.get("ffc_first", False)),
                 "unit": params.get("unit", "C")}
            self.sr = {"state": "capturing", "progress": 0.0, "message": "starting",
                       "id": self.sr["id"] + 1, "params": p}
            self._sr_result = None
        if p["ffc_first"]:
            try:
                self.sr["message"] = "running FFC"
                self.cam.run_ffc()
                time.sleep(1.5)
            except Exception as e:
                print(f"[studio] FFC before capture failed: {e}")
        with self.lock:
            self._sr_frames = []

    def sr_cancel(self):
        with self.lock:
            self._sr_frames = None
            self.sr["state"] = "idle"
            self.sr["message"] = "cancelled"

    def _sr_run(self, frames):
        p = self.sr["params"]
        sr_id = self.sr["id"]

        def prog(x, msg):
            if self.sr["id"] == sr_id:
                self.sr["progress"] = float(x)
                self.sr["message"] = msg
        try:
            t0 = time.time()
            if p["mode"] == "integrate":
                res = superres.integrate(frames, align=p["align"], progress=prog)
            else:
                res = superres.superres(frames, scale=p["scale"], iterations=p["iterations"],
                                        progress=prog)
            res["seconds"] = time.time() - t0
            base = os.path.join(self.out_dir, f"{'superres' if p['mode'] != 'integrate' else 'integrated'}_{stamp()}")
            meta_extra = {k: v for k, v in res.items() if k not in ("image", "baseline")}
            meta_extra["params"] = p
            radiometric = bool(self.cam.radiometric) if self.cam else True
            res["files"] = self._save_radiometric(base, res["image"], p["unit"], radiometric, meta_extra)
            if self.sr["id"] == sr_id:
                self._sr_result = res
                self.sr["state"] = "done"
                self.sr["progress"] = 1.0
                self.sr["message"] = f"done in {res['seconds']:.1f}s"
        except Exception as e:
            traceback.print_exc()
            if self.sr["id"] == sr_id:
                self.sr["state"] = "error"
                self.sr["message"] = str(e)

    def sr_status(self):
        s = {k: v for k, v in self.sr.items()}
        r = self._sr_result
        if r is not None and s["state"] == "done":
            s["result"] = {k: v for k, v in r.items() if k not in ("image", "baseline", "shifts")}
            s["result"]["shape"] = list(r["image"].shape)
            s["result"]["shifts"] = r["shifts"][:400]
        return s


def write_series_header(w, series, unit):
    """Header shared by live logs and chart exports: unit, point positions, column names."""
    w.writerow(["# unit", unit])
    for s in series:
        if "x" in s:
            w.writerow(["# " + s["id"], s["label"], f"x={s['x']}", f"y={s['y']}",
                        f"window={2 * s['r'] + 1}x{2 * s['r'] + 1}"])
    w.writerow(["timestamp", "unix_time"] + [f"{s['label']} [{unit}]" for s in series])


class CsvLogger:
    def __init__(self, out_dir, series, interval_s, unit):
        self.out_dir = out_dir
        self.interval = max(0.0, float(interval_s))
        self.unit = unit
        self.base = os.path.join(out_dir, f"log_{stamp()}")
        self.part = 0
        self.rows = 0
        self.started = time.time()
        self.f = None
        self.rotate(series)

    def rotate(self, series):
        if self.f:
            self._flush_acc(force=True)
            self.f.close()
        self.part += 1
        self.path = self.base + (".csv" if self.part == 1 else f"_part{self.part}.csv")
        self.f = open(self.path, "w", newline="")
        fix_owner(self.path)
        self.series = series
        self.w = csv.writer(self.f)
        write_series_header(self.w, series, self.unit)
        self.acc = {}
        self.acc_n = 0
        self.next_due = None

    def add(self, t, values):
        if self.next_due is None:
            self.next_due = t + self.interval
        for s in self.series:
            v = values.get(s["id"])
            if v is not None:
                self.acc[s["id"]] = self.acc.get(s["id"], 0.0) + v
        self.acc_n += 1
        self.last_t = t
        if self.interval == 0 or t >= self.next_due:
            self._flush_acc()
            self.next_due += self.interval
            if self.next_due <= t:          # fell behind (e.g. paused stream)
                self.next_due = t + self.interval

    def _flush_acc(self, force=False):
        if not self.acc_n:
            return
        t = self.last_t
        row = [dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds"), f"{t:.3f}"]
        for s in self.series:
            v = self.acc.get(s["id"])
            row.append("" if v is None else f"{convert(v / self.acc_n, self.unit):.3f}")
        self.w.writerow(row)
        self.rows += 1
        if self.rows % 20 == 0:
            self.f.flush()
        self.acc, self.acc_n = {}, 0

    def close(self):
        self._flush_acc(force=True)
        self.f.close()

    def status(self):
        return {"active": True, "path": self.path, "rows": self.rows,
                "elapsed_s": time.time() - self.started, "interval_s": self.interval, "unit": self.unit}


class RawRecorder:
    def __init__(self, out_dir, scale, radiometric, shape):
        self.base = os.path.join(out_dir, f"recording_{stamp()}")
        self.path = self.base + ".y16"
        self.f = open(self.path, "wb")
        fix_owner(self.path)
        self.times = []
        self.meta = {"format": "raw little-endian uint16 frames, row-major",
                     "width": shape[1], "height": shape[0],
                     "kelvin_per_count": scale if radiometric else None,
                     "started": dt.datetime.now().isoformat()}
        self.started = time.time()

    def add(self, counts, t):
        self.f.write(counts.astype("<u2").tobytes())
        self.times.append(t)

    def close(self):
        self.f.close()
        self.meta["frames"] = len(self.times)
        self.meta["frame_unix_times"] = self.times
        self.meta["read_with"] = ("np.fromfile(path, '<u2').reshape(-1, height, width) "
                                  "* kelvin_per_count  -> Kelvin")
        with open(self.base + "_meta.json", "w") as f:
            json.dump(self.meta, f)
        fix_owner(self.base + "_meta.json")

    def status(self):
        return {"active": True, "path": self.path, "frames": len(self.times),
                "elapsed_s": time.time() - self.started}


# ------------------------------------------------------------------- HTTP ---
PALETTE = ["#ffd23f", "#3bceac", "#ee4266", "#4ea8ff", "#b388ff", "#ff9f1c",
           "#7bd389", "#f15bb5", "#00bbf9", "#e0e0e0"]


def make_handler(studio):
    web = os.path.join(HERE, "web")
    ctypes_map = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                  ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png"}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, default=lambda o: float(o) if isinstance(o, np.floating) else str(o)).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            self.wfile.write(body)

        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")

        # ---------------------------------------------------------- GET ---
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/api/frame":
                    return self._frame(int(qs.get("after", ["0"])[0]))
                if u.path == "/api/camera":
                    return self._send(200, self._camera_state())
                if u.path == "/api/status":
                    return self._send(200, self._live_status())
                if u.path == "/api/points":
                    with studio.lock:
                        return self._send(200, {"series": studio.series_meta(),
                                                "builtin": studio.builtin})
                if u.path == "/api/history":
                    return self._history(float(qs.get("since", ["0"])[0]))
                if u.path == "/api/history/export.csv":
                    unit = qs.get("unit", ["C"])[0]
                    unit = unit if unit in ("C", "F", "K") else "C"
                    path, text, n = studio.export_history(float(qs.get("window", ["0"])[0]), unit)
                    print(f"[studio] exported {n} rows -> {path}")
                    return self._send(200, text, "text/csv; charset=utf-8", {
                        "Content-Disposition": f'attachment; filename="{os.path.basename(path)}"',
                        "X-Saved-Path": path, "X-Rows": n})
                if u.path == "/api/superres":
                    return self._send(200, studio.sr_status())
                if u.path == "/api/superres/image":
                    return self._sr_image(qs.get("which", ["image"])[0])
                return self._static(u.path)
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})

        def _static(self, path):
            if path == "/":
                path = "/index.html"
            full = os.path.normpath(os.path.join(web, path.lstrip("/")))
            if not full.startswith(web) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            return self._send(200, data, ctypes_map.get(os.path.splitext(full)[1], "application/octet-stream"))

        def _frame(self, after):
            deadline = time.time() + 2.0
            with studio.cond:
                while (studio.latest is None or studio.latest["seq"] <= after) and time.time() < deadline:
                    studio.cond.wait(timeout=max(0.01, deadline - time.time()))
                lt = studio.latest
            if lt is None or lt["seq"] <= after:
                return self._send(204, b"", "application/octet-stream")
            c = lt["counts"]
            return self._send(200, c.astype("<u2").tobytes(), "application/octet-stream", {
                "X-Seq": lt["seq"], "X-Time": f"{lt['t']:.3f}", "X-Width": c.shape[1],
                "X-Height": c.shape[0], "X-Scale": lt["scale"],
                "X-Radiometric": int(lt["radiometric"]), "X-Fps": f"{studio.fps:.2f}"})

        def _camera_state(self):
            cam = studio.cam
            st = {"connected": cam is not None, "error": studio.cam_error,
                  "demo": bool(cam and cam.is_demo), "name": cam.name if cam else None,
                  "info": studio.cam_info, "out_dir": studio.out_dir,
                  "firmware_notice": getattr(cam, "firmware_notice", None) if cam else None}
            if cam:
                try:
                    st["settings"] = cam.read_settings()
                except Exception as e:
                    st["settings_error"] = str(e)
                st["radiometric"] = bool(cam.radiometric)
                st["scale"] = cam.scale
            return st

        def _live_status(self):
            cam = studio.cam
            st = {"fps": studio.fps, "seq": studio.seq,
                  "logging": studio.logger.status() if studio.logger else {"active": False},
                  "recording": studio.recorder.status() if studio.recorder else {"active": False},
                  "connected": cam is not None, "error": studio.cam_error}
            if cam:
                try:
                    st.update(cam.read_status())
                except Exception as e:
                    st["status_error"] = str(e)
                st["bad_frames"] = getattr(cam, "bad_frames", 0)
                st["stream_error"] = getattr(cam, "stream_error", None)
            return st

        def _history(self, since):
            out = []
            with studio.lock:
                for s in studio.series_meta():
                    h = studio.history.get(s["id"])
                    if not h:
                        out.append({"id": s["id"], "t": [], "v": []})
                        continue
                    ts, vs = h
                    # walk back from the end - incremental fetches are short
                    n = len(ts)
                    i = 0 if (n and since < ts[0]) else n
                    while i > 0 and ts[i - 1] > since:
                        i -= 1
                    tl = list(itertools.islice(ts, i, n))
                    vl = list(itertools.islice(vs, i, n))
                    out.append({"id": s["id"], "t": [round(x, 3) for x in tl],
                                "v": [round(x, 3) for x in vl]})
            return self._send(200, {"series": out, "now": time.time()})

        def _sr_image(self, which):
            r = studio._sr_result
            if r is None:
                return self._send(404, {"error": "no result"})
            img = r["image"] if which != "baseline" else r["baseline"]
            img = np.asarray(img, dtype="<f4")
            return self._send(200, img.tobytes(), "application/octet-stream",
                              {"X-Width": img.shape[1], "X-Height": img.shape[0]})

        # --------------------------------------------------------- POST ---
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            try:
                body = self._json()
                fn = {
                    "/api/camera/setting": self._p_setting,
                    "/api/camera/ffc": self._p_ffc,
                    "/api/camera/connect": self._p_connect,
                    "/api/points/add": self._p_point_add,
                    "/api/points/update": self._p_point_update,
                    "/api/points/remove": self._p_point_remove,
                    "/api/points/clear": self._p_point_clear,
                    "/api/points/builtin": self._p_builtin,
                    "/api/history/clear": self._p_hist_clear,
                    "/api/log/start": self._p_log_start,
                    "/api/log/stop": self._p_log_stop,
                    "/api/record/start": self._p_rec_start,
                    "/api/record/stop": self._p_rec_stop,
                    "/api/snapshot": self._p_snapshot,
                    "/api/superres/start": self._p_sr_start,
                    "/api/superres/cancel": self._p_sr_cancel,
                    "/api/open_folder": self._p_open_folder,
                }.get(u.path)
                if fn is None:
                    return self._send(404, {"error": "unknown endpoint"})
                return self._send(200, fn(body) or {"ok": True})
            except Exception as e:
                traceback.print_exc()
                return self._send(400, {"error": str(e)})

        def _need_cam(self):
            if studio.cam is None:
                raise RuntimeError("camera not connected")
            return studio.cam

        def _p_setting(self, b):
            cam = self._need_cam()
            cam.apply_setting(b["key"], b["value"])
            return {"ok": True, "settings": cam.read_settings(), "scale": cam.scale,
                    "radiometric": bool(cam.radiometric)}

        def _p_ffc(self, b):
            self._need_cam().run_ffc()

        def _p_connect(self, b):
            studio.connect(bool(b.get("demo")))
            return self._camera_state()

        def _p_point_add(self, b):
            with studio.lock:
                pid = studio.next_pid
                studio.next_pid += 1
                p = {"id": pid, "x": int(b["x"]), "y": int(b["y"]), "r": int(b.get("r", 0)),
                     "label": b.get("label") or f"P{pid}",
                     "color": b.get("color") or PALETTE[(pid - 1) % len(PALETTE)]}
                studio.points.append(p)
                studio.points_changed()
                return {"ok": True, "point": p}

        def _p_point_update(self, b):
            with studio.lock:
                for p in studio.points:
                    if p["id"] == int(b["id"]):
                        for k in ("x", "y", "r"):
                            if k in b:
                                p[k] = int(b[k])
                        for k in ("label", "color"):
                            if k in b:
                                p[k] = str(b[k])
                studio.points_changed()

        def _p_point_remove(self, b):
            with studio.lock:
                studio.points = [p for p in studio.points if p["id"] != int(b["id"])]
                studio.history.pop(f"p{int(b['id'])}", None)
                studio.points_changed()

        def _p_point_clear(self, b):
            with studio.lock:
                for p in studio.points:
                    studio.history.pop(f"p{p['id']}", None)
                studio.points = []
                studio.points_changed()

        def _p_builtin(self, b):
            with studio.lock:
                for k in studio.builtin:
                    if k in b:
                        studio.builtin[k] = bool(b[k])
                        if not b[k]:
                            studio.history.pop(k, None)
                studio.points_changed()

        def _p_hist_clear(self, b):
            with studio.lock:
                studio.history.clear()

        def _p_log_start(self, b):
            with studio.lock:
                if studio.logger:
                    studio.logger.close()
                studio.logger = CsvLogger(studio.out_dir, studio.series_meta(),
                                          b.get("interval_s", 1.0), b.get("unit", "C"))
                return studio.logger.status()

        def _p_log_stop(self, b):
            with studio.lock:
                if studio.logger:
                    st = studio.logger.status()
                    studio.logger.close()
                    studio.logger = None
                    st["active"] = False
                    return st

        def _p_rec_start(self, b):
            lt = studio.latest
            if lt is None:
                raise RuntimeError("no frames yet")
            with studio.lock:
                if studio.recorder:
                    studio.recorder.close()
                studio.recorder = RawRecorder(studio.out_dir, lt["scale"], lt["radiometric"],
                                              lt["counts"].shape)
                return studio.recorder.status()

        def _p_rec_stop(self, b):
            with studio.lock:
                if studio.recorder:
                    st = studio.recorder.status()
                    studio.recorder.close()
                    studio.recorder = None
                    st["active"] = False
                    return st

        def _p_snapshot(self, b):
            return {"ok": True, "files": studio.snapshot(b.get("unit", "C"))}

        def _p_sr_start(self, b):
            studio.sr_start(b)
            return studio.sr_status()

        def _p_sr_cancel(self, b):
            studio.sr_cancel()

        def _p_open_folder(self, b):
            path = studio.out_dir
            user = os.environ.get("SUDO_USER")
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            cmd = ["sudo", "-u", user, opener, path] if user else [opener, path]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return H


def open_browser(url):
    user = os.environ.get("SUDO_USER")
    try:
        if user and sys.platform == "darwin":
            subprocess.Popen(["sudo", "-u", user, "open", url])
        else:
            webbrowser.open(url)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use the simulated camera")
    ap.add_argument("--no-jitter", action="store_true", help="demo: hold the simulated camera still")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--out", default=os.path.join(HERE, "captures"), help="folder for logs and captures")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    studio = Studio(os.path.abspath(a.out), demo=a.demo, demo_jitter=not a.no_jitter)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(studio))
    srv.daemon_threads = True
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/"
    print(f"[studio] UI at {url}   (Ctrl+C to quit)")
    print(f"[studio] files go to {studio.out_dir}")
    if not a.no_browser:
        threading.Timer(0.8, open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if studio.logger:
            studio.logger.close()
        if studio.recorder:
            studio.recorder.close()
        if studio.cam:
            studio.cam.close()
        print("\n[studio] bye")


if __name__ == "__main__":
    main()
