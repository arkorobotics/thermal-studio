#!/bin/bash
# Thermal Studio launcher (macOS / Linux). On Windows use run.bat.
#   ./run.sh           real PureThermal camera (no sudo needed)
#   ./run.sh --demo    simulated camera, no hardware needed
set -e
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating Python environment (one-time)…"
  if ! python3 -m venv .venv; then
    rm -rf .venv
    echo "Could not create a Python venv. On Ubuntu/Debian run:  sudo apt install python3-venv"
    echo "(or run linux/setup-ubuntu.sh once)"
    exit 1
  fi
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

if [[ "$(uname)" == "Darwin" ]]; then
  # macOS uses the IOKit backend (macos/lepton_iokit.c), built automatically on first run.
  # Do NOT use sudo: the old libuvc path detached Apple's camera driver as root and
  # caused kernel panics on macOS 26. It stays disabled unless THERMAL_BACKEND=libuvc
  # and THERMAL_ALLOW_LIBUVC_MACOS=1 are both set.
  if ! command -v clang >/dev/null 2>&1; then
    echo "clang not found. Install the Xcode Command Line Tools:  xcode-select --install"
    exit 1
  fi
  echo "Quit FaceTime, Photo Booth, OBS, Zoom and browser tabs using the camera first."
fi

if [[ "$(uname)" == "Linux" && "$*" != *--demo* ]]; then
  # Linux uses V4L2 (the kernel's uvcvideo driver): no libuvc, no udev rule, no sudo.
  # /dev/video* must be accessible: desktop sessions get that automatically, otherwise
  # join the video group (linux/setup-ubuntu.sh does it).
  dev_ok=0
  for d in /dev/video*; do [[ -r "$d" && -w "$d" ]] && dev_ok=1 && break; done
  if [[ $dev_ok == 0 ]] && ls /dev/video* >/dev/null 2>&1; then
    echo "Warning: no access to /dev/video*. Run linux/setup-ubuntu.sh (adds you to the video group)."
  fi
  echo "Close Cheese, OBS, Zoom and browser tabs using the camera first."
fi

exec .venv/bin/python thermal_app.py "$@"
