# PyInstaller spec for Mynah (FULL build with transcription).
# Build with: python build.py
#
# This bundles PyTorch + WhisperX + pyannote into the .exe. Expect a build
# time of ~10 min and a dist folder of ~4 GB. Model weights are NOT bundled
# (they're cached to %USERPROFILE%\.cache\huggingface\ on first use).

import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

block_cipher = None

# Packages that MUST be bundled successfully — if collect_all fails for
# any of these, the resulting .exe will crash at runtime, so abort the
# build rather than ship a silent dud. Previously every collect_all
# failure was a print-and-continue WARN, which let a build with broken
# torch / whisperx succeed at the PyInstaller level and only fail at
# first launch on the user's machine.
_critical_packages = [
    "torch",
    "torchaudio",
    "whisperx",
    "pyannote",
    "pyannote.audio",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "safetensors",
    "ctranslate2",
    "faster_whisper",
    "scipy",
    "soundfile",
]

# Packages that are nice-to-have for transitive imports but the app can
# survive without (e.g. torchvision is pulled in by upstream wheels but
# we don't import it ourselves; pyannote.{database,metrics,pipeline}
# are sometimes optional depending on the pipeline version).
_optional_packages = [
    "torchvision",
    "pyannote.core",
    "pyannote.database",
    "pyannote.metrics",
    "pyannote.pipeline",
    "speechbrain",
    "asteroid_filterbanks",
    "lightning_fabric",
    "pytorch_lightning",
    "torchmetrics",
    "sentencepiece",
    # collect_all() takes the IMPORTABLE module name, not the PyPI
    # distribution name. The importable module is `sklearn`; the
    # distribution name `scikit-learn` would be silently skipped here
    # (because the optional loop just warns on failure), and pyannote /
    # whisperx would then crash at runtime trying to import sklearn from
    # the frozen exe. Metadata for the distribution name is copied
    # separately via copy_metadata('scikit-learn') below.
    "sklearn",
    "librosa",
    "numba",
    "llvmlite",
]

datas: list = []
binaries: list = []
hiddenimports: list = [
    "win32file",
    "win32pipe",
    "pywintypes",
    *collect_submodules("pyaudiowpatch"),
]

_critical_failures: list[str] = []
for pkg in _critical_packages:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"ERROR: collect_all({pkg!r}) FAILED: {e}")
        _critical_failures.append(pkg)

if _critical_failures:
    raise SystemExit(
        "Build aborted: PyInstaller could not collect the following "
        f"critical packages: {_critical_failures}. Install them in the "
        "build venv before running build.py."
    )

for pkg in _optional_packages:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"WARN: collect_all({pkg!r}) skipped: {e}")

# transformers does importlib.metadata.version("<pkg>") at import time for
# several optional deps. If the .dist-info is missing, it raises
# PackageNotFoundError and import fails. Bundle just the metadata for these
# (we don't need their code to actually load — transformers handles failures
# gracefully *after* it can read the version).
_metadata_only = [
    "torchcodec",
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "safetensors",
    "accelerate",
    "regex",
    "tqdm",
    "filelock",
    "packaging",
    "pyyaml",
    "psutil",
    # scikit-learn is collected as `sklearn` above (importable name);
    # several upstream packages call importlib.metadata.version() with
    # the distribution name, so we ship that metadata explicitly.
    "scikit-learn",
]
for pkg in _metadata_only:
    try:
        datas += copy_metadata(pkg)
    except Exception as e:
        print(f"WARN: copy_metadata({pkg!r}) skipped: {e}")

# Drop the giant useless extras even when transitively pulled in.
# NOTE: do NOT exclude pandas or matplotlib — pyannote.database and
# pyannote.metrics import them unconditionally.
excludes = [
    "tensorboard",
    "tensorflow",
    "jax",
    "jaxlib",
    "PIL.ImageQt",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "notebook",
    "jupyter",
    "IPython",
]

a = Analysis(
    ["src/mynah/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mynah",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Mynah",
)
