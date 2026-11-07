# Thermal Studio

Viewer, logger and super-resolution tool for a **FLIR Lepton 3.5 on a PureThermal 3** (also works with PureThermal 1/2/Mini).
It's a small Python server with a browser UI. The camera is read as radiometric **Y16** (each pixel is a temperature), so all measurements are real temperatures, not colours.

## Download

Prebuilt apps are on the [Releases page](https://github.com/arkorobotics/thermal-studio/releases/latest). Each one is a single file. You don't need Python, and you don't need to install anything else.

| OS | download |
|---|---|
| Windows 10/11 (x64) | [thermal-studio-windows-x64.zip](https://github.com/arkorobotics/thermal-studio/releases/latest/download/thermal-studio-windows-x64.zip) |
| macOS, Apple Silicon | [thermal-studio-macos-arm64.zip](https://github.com/arkorobotics/thermal-studio/releases/latest/download/thermal-studio-macos-arm64.zip) |
| macOS, Intel | [thermal-studio-macos-x64.zip](https://github.com/arkorobotics/thermal-studio/releases/latest/download/thermal-studio-macos-x64.zip) |
| Ubuntu / Linux (x64) | [thermal-studio-linux-x64.tar.gz](https://github.com/arkorobotics/thermal-studio/releases/latest/download/thermal-studio-linux-x64.tar.gz) |
| Linux ARM64 (Raspberry Pi 4/5 with a 64-bit OS, Jetson) | [thermal-studio-linux-arm64.tar.gz](https://github.com/arkorobotics/thermal-studio/releases/latest/download/thermal-studio-linux-arm64.tar.gz) |

Unzip it and run `thermal-studio`. A terminal window opens and shows the log, and the UI opens in your browser. Close the terminal window (or press Ctrl+C) to quit. Captures go to `Documents/Thermal Studio` (change this with `--out`). All the command-line options below work with the binary too, e.g. `thermal-studio --demo`.

- **Windows:** double-click `thermal-studio.exe`. The app isn't code-signed, so SmartScreen may warn you. Click *More info → Run anyway*.
- **macOS:** the app isn't signed or notarized, so macOS blocks the first launch. Either run `xattr -d com.apple.quarantine thermal-studio` in the unzipped folder, or double-click it once, then go to *System Settings → Privacy & Security* and click *Open Anyway*.
- **Linux:** `tar xzf thermal-studio-linux-*.tar.gz && cd thermal-studio && ./thermal-studio`. If you can't open the camera, run `./setup-ubuntu.sh` once, then log out and back in.

The sections below run Thermal Studio from source instead. Use them if you want to change the code, or if there's no build for your platform.

## Quick start (macOS)

```bash
cd thermal-studio
./run.sh                     # opens http://127.0.0.1:8765 (no sudo)
./run.sh --demo              # no camera: simulated Lepton
```

**How the camera is accessed on macOS:** through IOKit, using the small C library in `macos/lepton_iokit.c` (built automatically with clang on first run; needs the Xcode Command Line Tools). It opens the PureThermal the way [Lepton-GetThermal](https://github.com/hmenzagh/Lepton-GetThermal) does and streams raw Y16. **Don't run it with sudo, and don't use libuvc on macOS:** libuvc detaches Apple's camera driver as root, and on macOS 26 (Apple Silicon) that caused kernel panics. The libuvc backend is still used on Linux.

Quit Photo Booth, FaceTime, Zoom and any browser tab that is using the camera first. Otherwise you'll get "resource busy".

## Quick start (Ubuntu / Linux)

```bash
cd thermal-studio
linux/setup-ubuntu.sh        # one-time: python3-venv + adds you to the video group (log out/in after)
./run.sh                     # opens http://127.0.0.1:8765 (no sudo)
./run.sh --demo
```

**How the camera is accessed on Linux:** through the kernel's own UVC driver (V4L2, `lepton_v4l2.py`, pure Python). Frames come from the `/dev/videoN` node that offers 160×120 `Y16`, and Lepton commands go over the `UVCIOC_CTRL_QUERY` extension-unit ioctl. Nothing to compile, no libuvc and no udev rule. The old libuvc backend is still there: `THERMAL_BACKEND=libuvc ./run.sh` (set it up with `linux/setup-ubuntu.sh --libuvc`).

Close Cheese, OBS, Zoom and browser tabs that are using the camera first.

## Quick start (Windows 10/11)

Install Python 3 from [python.org](https://www.python.org/downloads/) (tick *Add python.exe to PATH*), then double-click **`run.bat`**, or from a terminal:

```bat
cd thermal-studio
run.bat                      :: opens http://127.0.0.1:8765
run.bat --demo
```

The first run creates `.venv` and installs numpy and OpenCV.

**How the camera is accessed on Windows:** with the stock Windows UVC driver (`lepton_windows.py`). **Don't install a WinUSB/libusbK driver with Zadig**: it isn't needed, and it would stop the camera from working here and in other apps. Frames are read in raw `Y16` through OpenCV's DirectShow backend. Lepton commands are sent as `IKsControl` extension-unit properties on the camera's DirectShow filter. Windows addresses each unit by its GUID (`pt1-lep-agc-0000` and so on, from the PureThermal firmware). The RAD controls above 31 go through the firmware's second RAD unit, because Windows limits an extension unit to 31 controls.

Close the Camera app, Teams, Zoom, OBS and browser tabs that are using the camera first.

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
| `lepton_uvc.py` | Lepton command layer + libuvc (ctypes) Y16 streaming (Linux fallback) |
| `lepton_v4l2.py` | Linux backend: V4L2 Y16 streaming + UVC extension-unit ioctls |
| `lepton_windows.py` | Windows backend: DirectShow Y16 (OpenCV) + IKsControl extension units |
| `lepton_iokit.py`, `macos/lepton_iokit.c` | macOS backend: IOKit Y16 streaming + control transfers (no sudo) |
| `superres.py` | registration, integration, super-resolution (numpy only) |
| `demo_camera.py` | simulated Lepton with hand-held jitter (for `--demo`) |
| `web/` | browser UI |
| `run.sh`, `run.bat`, `linux/setup-ubuntu.sh` | launchers (macOS/Linux, Windows) and Ubuntu one-time setup |
| `build.py`, `requirements-build.txt`, `.github/workflows/build.yml` | packaging into single-file binaries, and the release pipeline |

## Building the binaries yourself

The app is packaged with [PyInstaller](https://pyinstaller.org). PyInstaller can't cross-compile, so build on the OS you're targeting: Windows on Windows, macOS on a Mac (each Mac architecture builds its own), Linux on Linux. You need Python 3.9+. On macOS you also need the Xcode Command Line Tools (`xcode-select --install`), because the IOKit helper is compiled and bundled at build time.

```bash
git clone https://github.com/arkorobotics/thermal-studio.git
cd thermal-studio
python3 -m venv .venv-build                       # Windows: py -3 -m venv .venv-build
.venv-build/bin/pip install -r requirements-build.txt     # Windows: .venv-build\Scripts\pip ...
.venv-build/bin/python build.py --smoke           # Windows: .venv-build\Scripts\python build.py --smoke
```

This writes `dist/thermal-studio` (`thermal-studio.exe` on Windows) and a ready-to-share `dist/thermal-studio-<os>-<arch>.zip` (Linux: `.tar.gz`). `--smoke` starts the built binary in demo mode and checks that the UI answers. Build on the oldest Linux you want to support, because the binary needs a glibc at least as new as the build machine's.

**Tests:** `python -m unittest discover -s tests -v`. These tests run offline, with no camera. They cover the Lepton command layer, the Windows and Linux backend internals, the demo camera and sub-pixel registration.

**Publishing a release (maintainers):** [.github/workflows/build.yml](.github/workflows/build.yml) runs the offline tests on Windows, macOS and Linux, then builds and smoke-tests all five binaries on GitHub's runners. It runs on every pull request and every push to `main`. Push a version tag to publish them as a GitHub Release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

You can also run the workflow by hand (*Actions → Build → Run workflow*). That builds everything without publishing, and the files can be downloaded from the run page.

## Troubleshooting

- **"PureThermal not found"**: check `system_profiler SPUSBDataType | grep -A3 PureThermal` (macOS), `lsusb | grep 1e4e` (Linux) or Device Manager → Cameras (Windows). Try another cable or port; some USB-C hubs have trouble with isochronous video.
- **"could not open the camera"** (macOS): another app has it open. Quit FaceTime, Photo Booth, OBS, Zoom and browser tabs, then press Reconnect.
- **"resource busy"**: another app has the camera open.
- **Image is garbage or no frames arrive, and the terminal mentions the format code**: your libuvc build numbers its formats differently. Run `UVC_Y16_FORMAT=9 ./run.sh` (or `=10`).
- **Temperatures look wrong**: check that TLinear is enabled and emissivity is set (Camera tab), and run an FFC. After a big change in ambient temperature, give the camera a few minutes to settle.
- **Linux, "No permission to open /dev/videoN"**: run `linux/setup-ubuntu.sh`, then log out and back in.
- **Linux, libuvc fallback**: `linux/setup-ubuntu.sh --libuvc` installs libuvc and the udev rule
  `SUBSYSTEMS=="usb", ATTRS{idVendor}=="1e4e", ATTRS{idProduct}=="0100", MODE="0666"`. Then run `THERMAL_BACKEND=libuvc ./run.sh`.
- **Windows, "did not switch to raw Y16"**: update OpenCV with `.venv\Scripts\pip install -U opencv-python-headless`. If you ever installed a Zadig driver for the PureThermal, go to Device Manager → the device → *Uninstall device* and tick *delete the driver*, then replug it.
- **Windows, camera controls fail but video works**: another program may have the camera's controls locked. Close other camera apps and press Reconnect.
