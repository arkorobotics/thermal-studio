# Thermal Studio

Viewer, logger and super-resolution tool for a **FLIR Lepton 3.5 on a PureThermal 3** (also works with PureThermal 1/2/Mini).
It's a small Python server with a browser UI. The camera is read as radiometric **Y16** (each pixel is a temperature), so all measurements are real temperatures, not colours.

## Quick start (macOS)

```bash
cd thermal-studio
./run.sh                     # opens http://127.0.0.1:8765 (no sudo)
./run.sh --demo              # no camera: simulated Lepton
```

**How the camera is accessed on macOS:** through IOKit, using the small C library in `macos/lepton_iokit.c` (built automatically with clang on first run; needs the Xcode Command Line Tools). It opens the PureThermal the way [Lepton-GetThermal](https://github.com/hmenzagh/Lepton-GetThermal) does and streams raw Y16. **Don't run it with sudo, and don't use libuvc on macOS:** libuvc detaches Apple's camera driver as root, and on macOS 26 (Apple Silicon) that caused kernel panics. The libuvc backend is still used on Linux.

Quit Photo Booth, FaceTime, Zoom and any browser tab that is using the camera first. Otherwise you'll get "resource busy".

Options: `--port 8765`, `--out /path/for/captures`, `--no-browser`, `--host 0.0.0.0` (view from another device on your LAN).

## What's in the UI

**Live view**
- 8 palettes, auto range, auto 1–99 % range or manual range ("Lock current range" freezes it)
- °C / °F / K, smooth or pixelated scaling, rotate, mirror and flip
- Temporal averaging (1–16 frames), hot/cold markers, centre crosshair, isotherm highlight above or below a threshold
- Hover anywhere to read the temperature at that pixel

**Measurement points and logging**
- Click the image to add a point. Drag a point to move it, right-click it to delete it.
- Each point averages a 1×1, 3×3, 5×5 or 7×7 pixel area. Rename points in the Points tab.
- The live chart shows 30 s, 2 min, 10 min, 30 min or all history (about 4.8 h kept in memory). Hover the chart to read values.
- You can also plot the frame max, min and mean.
- **Export CSV** (next to the chart) saves what the chart shows, after the fact: every plotted point and frame max/min/mean series over the selected time window, in the current unit. Your browser downloads it and a copy goes to the captures folder (`chart_<time>.csv`). A point added later is blank before it existed.
- CSV logging records every frame, or the average over 0.5 s, 1 s, 5 s, 10 s or 1 min. The file gets one column per point, and point coordinates go in the header. Adding or removing a point during logging starts a `_partN.csv` file.
- Raw recording saves the full radiometric video (`.y16` + `_meta.json`). Read it back in Python with `np.fromfile(path,'<u2').reshape(-1,120,160) * kelvin_per_count`.
- **Snapshot** saves `_kelvin.npy`, a CSV in your chosen unit, a 16-bit PNG in centikelvin and `_meta.json` (which includes the camera settings). Your browser also downloads the coloured PNG.

**Camera controls** (Lepton SDK through the PureThermal's UVC extension units)
- Gain mode: High (−10…140 °C), Low (up to ~400 °C) or Auto. Choosing Low or Auto switches TLinear to 0.1 K resolution, because 0.01 K overflows above 382 °C.
- FFC: run now, shutter mode (auto/manual/external), auto-FFC period, auto-FFC temperature delta, freeze video during FFC
- Emissivity (with material presets) and reflected/background temperature
- Atmosphere and window: transmission and temperatures, for shooting through a protective window
- Radiometry on/off, TLinear on/off and resolution
- Info tab: serial number, part number, firmware, FPA and housing temperatures, FFC status

**Integration tab**
- **Super-resolution (2×, 3× or 4×).** Hand-hold the camera and aim it at a still scene, then press Capture. Natural hand tremor shifts each frame by a fraction of a pixel. The frames are registered with sub-pixel phase correlation (about 0.04 px accuracy), fused onto a finer grid and sharpened with iterative back-projection that models the Lepton's square pixels. Frames that register badly (scene motion, FFC) are dropped. The result shows how much the camera moved and how well the sub-pixel phases were covered. Toggle between *Result* and *Single frame* to compare.
- **Integrate (noise).** Use a tripod and a still scene. Frames are averaged, so noise drops by about √N (48 frames ≈ 7× lower). There's optional drift compensation.
- Results are saved as radiometric files (`.npy`, CSV, 16-bit PNG, metadata) and can be downloaded as a coloured PNG.

On a synthetic test with a known ground truth, 48 hand-held frames at 4× cut the reconstruction error to about a third of an upscaled single frame's. Bar patterns at 1.5 px spacing were resolved; a single frame blurs them together. How much you gain depends on the motion and on the camera's optics. A tripod gives no new detail, only lower noise.

## Files

| file | purpose |
|---|---|
| `thermal_app.py` | HTTP server, point measurement, logging, recording, capture jobs |
| `lepton_uvc.py` | Lepton command layer + libuvc (ctypes) Y16 streaming (Linux) |
| `lepton_iokit.py`, `macos/lepton_iokit.c` | macOS backend: IOKit Y16 streaming + control transfers (no sudo) |
| `superres.py` | registration, integration, super-resolution (numpy only) |
| `demo_camera.py` | simulated Lepton with hand-held jitter (for `--demo`) |
| `web/` | browser UI |

## Troubleshooting

- **"PureThermal not found"**: check `system_profiler SPUSBDataType | grep -A3 PureThermal`. Try another cable or port; some USB-C hubs have trouble with isochronous video.
- **"could not open the camera"** (macOS): another app has it open. Quit FaceTime, Photo Booth, OBS, Zoom and browser tabs, then press Reconnect.
- **"resource busy"**: another app has the camera open.
- **Image is garbage or no frames arrive, and the terminal mentions the format code**: your libuvc build numbers its formats differently. Run `UVC_Y16_FORMAT=9 ./run.sh` (or `=10`).
- **Temperatures look wrong**: check that TLinear is enabled and emissivity is set (Camera tab), and run an FFC. After a big change in ambient temperature, give the camera a few minutes to settle.
- **Linux**: `sudo apt install libuvc0` (or build libuvc), then add the udev rule
  `SUBSYSTEMS=="usb", ATTRS{idVendor}=="1e4e", ATTRS{idProduct}=="0100", MODE="0666"`.
