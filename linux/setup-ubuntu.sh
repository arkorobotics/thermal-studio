#!/bin/bash
# One-time setup for Thermal Studio on Ubuntu / Debian (and derivatives).
#   linux/setup-ubuntu.sh
# Installs Python's venv module and gives your user access to the camera.
set -e
cd "$(dirname "$0")/.."

echo "Installing python3-venv (needs sudo)…"
sudo apt-get update -qq
sudo apt-get install -y python3 python3-venv

if id -nG "$USER" | grep -qw video; then
  echo "You are already in the video group."
else
  echo "Adding $USER to the video group (camera access without sudo)…"
  sudo usermod -aG video "$USER"
  NEED_RELOGIN=1
fi

# Optional, only for the libuvc fallback backend (THERMAL_BACKEND=libuvc):
if [[ "$1" == "--libuvc" ]]; then
  sudo apt-get install -y libuvc0 || sudo apt-get install -y libuvc-dev
  echo 'SUBSYSTEMS=="usb", ATTRS{idVendor}=="1e4e", ATTRS{idProduct}=="0100", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-purethermal.rules >/dev/null
  sudo udevadm control --reload-rules && sudo udevadm trigger
  echo "libuvc + udev rule installed. Replug the camera."
fi

echo
echo "Done. Start Thermal Studio with:  ./run.sh"
if [[ -n "$NEED_RELOGIN" ]]; then
  echo "Log out and back in first (or reboot) so the video group takes effect."
fi
