#!/usr/bin/env python3
"""
Package Thermal Studio as a single executable for the OS you run this on.

    python -m pip install -r requirements-build.txt
    python build.py            # -> dist/thermal-studio[.exe] + dist/thermal-studio-<os>-<arch>.zip/.tar.gz
    python build.py --smoke    # also start the built binary in demo mode and check that the UI answers

PyInstaller can't cross-compile: build the Windows binary on Windows, the
Linux one on Linux and the macOS one on a Mac (the GitHub workflow in
.github/workflows/build.yml does all of them).
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "thermal-studio"
BACKENDS = ["demo_camera", "lepton_uvc", "lepton_iokit", "lepton_v4l2", "lepton_windows", "superres"]


def os_tag():
    osname = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    m = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)
    return f"{osname}-{arch}"


def build():
    sep = os.pathsep   # PyInstaller's src:dest separator (';' on Windows)
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--console", "--name", NAME,
            "--add-data", f"{os.path.join(HERE, 'web')}{sep}web"]
    for m in BACKENDS:
        args += ["--hidden-import", m]
    if sys.platform == "darwin":
        # The IOKit helper is normally compiled on first run; users of the binary
        # won't have clang, so build it now and bundle it.
        sys.path.insert(0, HERE)
        import lepton_iokit
        lib = lepton_iokit.build_helper(force=True)
        args += ["--add-binary", f"{lib}{sep}macos"]
    if sys.platform != "win32":
        args += ["--exclude-module", "cv2"]
    args.append(os.path.join(HERE, "thermal_app.py"))
    print(" ".join(args))
    subprocess.run(args, check=True, cwd=HERE)

    exe = os.path.join(HERE, "dist", NAME + (".exe" if sys.platform == "win32" else ""))
    return exe


def package(exe):
    tag = os_tag()
    dist = os.path.dirname(exe)
    readme = os.path.join(HERE, "README.md")
    if sys.platform.startswith("linux"):
        out = os.path.join(dist, f"{NAME}-{tag}.tar.gz")
        with tarfile.open(out, "w:gz") as t:
            t.add(exe, arcname=f"{NAME}/{os.path.basename(exe)}")
            t.add(readme, arcname=f"{NAME}/README.md")
            t.add(os.path.join(HERE, "linux", "setup-ubuntu.sh"), arcname=f"{NAME}/setup-ubuntu.sh")
    else:
        out = os.path.join(dist, f"{NAME}-{tag}.zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(exe, f"{NAME}/{os.path.basename(exe)}")   # keeps the exec bit
            z.write(readme, f"{NAME}/README.md")
    print(f"packaged {out} ({os.path.getsize(out) / 1e6:.1f} MB)")
    return out


def smoke(exe, port=8791):
    """Start the built binary with the simulated camera and fetch the UI and one API call."""
    p = subprocess.Popen([exe, "--demo", "--no-browser", "--port", str(port),
                          "--out", os.path.join(HERE, "build", "smoke-captures")])
    try:
        deadline = time.time() + 60   # one-file binaries unpack themselves on start
        while time.time() < deadline:
            if p.poll() is not None:
                raise SystemExit(f"smoke test: binary exited with code {p.returncode}")
            try:
                page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read()
                status = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2).read()
                assert b"<html" in page.lower() or b"<!doctype" in page.lower(), "UI page looks wrong"
                print(f"smoke test passed (UI {len(page)} bytes, status {len(status)} bytes)")
                return
            except OSError:
                time.sleep(1)
        raise SystemExit("smoke test: UI did not answer within 60 s")
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true", help="run the built binary in demo mode as a test")
    a = ap.parse_args()
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            raise SystemExit("PyInstaller is missing: python -m pip install -r requirements-build.txt")
    exe = build()
    if a.smoke:
        smoke(exe)
    package(exe)


if __name__ == "__main__":
    main()
