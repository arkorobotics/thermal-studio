#!/bin/bash
# Thermal Studio launcher (macOS / Linux)
#   ./run.sh           real PureThermal camera (no sudo needed)
#   ./run.sh --demo    simulated camera, no hardware needed
set -e
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating Python environment (one-time)…"
  python3 -m venv .venv
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

# Linux: libuvc works without sudo if you add a udev rule for 1e4e:0100 (see README)
exec .venv/bin/python thermal_app.py "$@"
