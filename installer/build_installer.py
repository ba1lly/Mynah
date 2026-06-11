"""Build MynahSetup.exe: wheel first, then the PyInstaller onefile.

Usage (from the repo root, any Python >= 3.10 with pip):
    python installer/build_installer.py

Output:
    dist/wheel/mynah-<version>-py3-none-any.whl
    dist/MynahSetup.exe

Used both locally and by .github/workflows/release.yml.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> int:
    wheel_dir = ROOT / "dist" / "wheel"
    if wheel_dir.exists():
        shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True)

    # `pip wheel --no-deps .` builds just the project wheel — no build
    # of the multi-GB dependency closure.
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", ".",
         "-w", str(wheel_dir)])
    wheels = sorted(wheel_dir.glob("mynah-*.whl"))
    if not wheels:
        print("ERROR: wheel build produced nothing", file=sys.stderr)
        return 1
    print(f"wheel: {wheels[-1].name}")

    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "installer"),
        str(ROOT / "installer" / "MynahSetup.spec"),
    ])

    exe = ROOT / "dist" / "MynahSetup.exe"
    if not exe.exists():
        print("ERROR: PyInstaller did not produce MynahSetup.exe",
              file=sys.stderr)
        return 1
    size_mb = exe.stat().st_size / 1e6
    print(f"OK: {exe} ({size_mb:.1f} MB)")
    if size_mb > 50:
        print("WARNING: installer exceeds the 50 MB acceptance target",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
