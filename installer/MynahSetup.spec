# PyInstaller spec for the MynahSetup bootstrap installer (issue #9).
# Build with: python installer/build_installer.py
#
# Onefile, stdlib-only payload + the mynah wheel as data. The result is
# ~15 MB — small enough for a GitHub release asset (2 GB cap) by three
# orders of magnitude; the ML stack downloads on the user's machine.

from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH).resolve().parent  # repo root (spec lives in installer/)

wheel_dir = ROOT / "dist" / "wheel"
wheels = sorted(wheel_dir.glob("mynah-*.whl"))
if not wheels:
    raise SystemExit(
        "No mynah wheel in dist/wheel/ — run installer/build_installer.py, "
        "which builds the wheel before invoking PyInstaller."
    )

datas = [
    (str(wheels[-1]), "wheel"),
    (str(ROOT / "src" / "mynah" / "assets" / "mynah.ico"), "."),
    (str(ROOT / "installer" / "assets" / "mynah-logo.png"), "."),
]

a = Analysis(
    [str(ROOT / "installer" / "bootstrap.py")],
    pathex=[str(ROOT / "installer")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MynahSetup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "src" / "mynah" / "assets" / "mynah.ico"),
)
