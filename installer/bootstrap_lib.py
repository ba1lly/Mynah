"""Pure logic for the Mynah bootstrap installer (issue #9).

Everything here is GUI-free and unit-testable; bootstrap.py wraps it in
a tkinter window. The installer's job: place a self-contained CPython
runtime in <install>\\python, pip-install the ML stack (CUDA or CPU
torch picked by GPU detection) plus the bundled Mynah wheel, write a
launcher, and create shortcuts.

Design constraints:
- stdlib only (urllib, not requests) — keeps the PyInstaller onefile
  bootstrap small and dependency-free.
- Every step is idempotent and recorded in <install>\\install_state.json,
  so re-running the installer after a dropped connection RESUMES instead
  of starting over. Large downloads also resume mid-file via HTTP Range.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# ---- pinned versions (keep in sync with start.ps1 / pyproject.toml) -------

# Official CPython distribution published by the CPython release team to
# NuGet. Chosen over the python.org "embeddable" zip because it ships
# ensurepip + a standard Lib/ layout (the embeddable package needs
# get-pip.py and a python._pth edit before pip works). It does NOT
# include tkinter — the installed app is web-UI only, which app.py
# already degrades around.
PYTHON_VERSION = "3.12.10"
PYTHON_NUPKG_URL = f"https://www.nuget.org/api/v2/package/python/{PYTHON_VERSION}"
# NuGet package versions are immutable; hash pinned from a verified
# download (sha256 of the .nupkg as served).
PYTHON_NUPKG_SHA256 = (
    "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"
)

TORCH_VERSION = "2.8.0"
TORCHAUDIO_VERSION = "2.8.0"
TORCHVISION_VERSION = "0.23.0"
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu126"
WHISPERX_SPEC = "whisperx==3.8.6"

# Minimum NVIDIA driver for CUDA 12.6 wheels (see README system table).
MIN_NVIDIA_DRIVER_MAJOR = 552

DEFAULT_INSTALL_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home()))
) / "Mynah"

LAUNCHER_NAME = "Mynah.pyw"

# Written into the install dir so a re-run knows which steps completed.
STATE_FILENAME = "install_state.json"


# ---- GPU detection ---------------------------------------------------------

@dataclass
class GpuInfo:
    has_cuda: bool
    description: str  # human-readable, shown in the installer UI


def parse_nvidia_driver_version(smi_output: str) -> Optional[int]:
    """Extract the major driver version from nvidia-smi output.

    `nvidia-smi --query-gpu=driver_version --format=csv,noheader` prints
    one line per GPU like '581.42'. Multi-GPU systems print several
    lines; the first parseable one wins.
    """
    for line in smi_output.splitlines():
        m = re.match(r"\s*(\d+)\.", line)
        if m:
            return int(m.group(1))
    return None


def detect_gpu(run=subprocess.run) -> GpuInfo:
    """Decide CUDA vs CPU torch. `run` injectable for tests."""
    try:
        proc = run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return GpuInfo(False, "No NVIDIA GPU detected — installing CPU build "
                              "(transcription works, ~10x slower).")
    if proc.returncode != 0:
        return GpuInfo(False, "nvidia-smi present but not working — "
                              "installing CPU build.")
    major = parse_nvidia_driver_version(proc.stdout or "")
    if major is None:
        return GpuInfo(False, "Could not read NVIDIA driver version — "
                              "installing CPU build.")
    if major < MIN_NVIDIA_DRIVER_MAJOR:
        return GpuInfo(
            False,
            f"NVIDIA driver {major} is too old for CUDA 12.6 "
            f"(need {MIN_NVIDIA_DRIVER_MAJOR}+) — installing CPU build. "
            "Update the driver and re-run this installer to upgrade.",
        )
    return GpuInfo(
        True,
        f"NVIDIA driver {major} found — installing CUDA build (~2.5 GB).",
    )


def torch_pip_args(gpu: GpuInfo) -> list[str]:
    """pip arguments for the torch trio, CUDA or CPU flavour.

    --force-reinstall is load-bearing for flavour SWITCHES (mirrors
    start.ps1): PEP 440 ignores local version segments, so an installed
    torch 2.8.0+cpu already satisfies ``torch==2.8.0`` and a plain
    install would no-op the CPU→CUDA upgrade the GPU message promises.
    On a fresh install there is nothing to reinstall, so it costs
    nothing there.
    """
    args = [
        "install",
        "--force-reinstall",
        f"torch=={TORCH_VERSION}",
        f"torchaudio=={TORCHAUDIO_VERSION}",
        f"torchvision=={TORCHVISION_VERSION}",
    ]
    if gpu.has_cuda:
        # --extra-index-url (not --index-url) so transitive deps still
        # resolve from PyPI — mirrors start.ps1.
        args += ["--extra-index-url", TORCH_CUDA_INDEX]
    return args


# ---- resumable download ----------------------------------------------------

def download(
    url: str,
    dest: Path,
    sha256: Optional[str] = None,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    _opener=urllib.request.urlopen,
) -> Path:
    """Download `url` to `dest`, resuming a partial `.part` file via
    HTTP Range, then verify sha256 if given.

    progress(bytes_done, total_or_None) is called as chunks arrive.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if sha256 is None or _file_sha256(dest) == sha256:
            if progress:
                progress(dest.stat().st_size, dest.stat().st_size)
            return dest
        dest.unlink()  # corrupt previous download — start over

    part = dest.with_suffix(dest.suffix + ".part")
    offset = part.stat().st_size if part.exists() else 0

    req = urllib.request.Request(url, headers={"User-Agent": "MynahSetup"})
    if offset:
        req.add_header("Range", f"bytes={offset}-")

    try:
        resp = _opener(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            # Range not satisfiable. Most often the .part IS the complete
            # file (the previous run died between the last byte and the
            # rename) — verify and promote instead of re-downloading.
            if sha256 is not None and part.exists() and _file_sha256(part) == sha256:
                part.replace(dest)
                if progress:
                    progress(dest.stat().st_size, dest.stat().st_size)
                return dest
            part.unlink(missing_ok=True)
            raise InstallError(
                "Partial download was stale; deleted it — click Install "
                "again to retry."
            )
        raise

    with resp:
        status = getattr(resp, "status", 200)
        if offset and status != 206:
            # Server ignored the Range header — start over.
            offset = 0
            part.unlink(missing_ok=True)
        total = None
        length = resp.headers.get("Content-Length")
        if length is not None:
            total = int(length) + offset
        mode = "ab" if offset else "wb"
        done = offset
        with part.open(mode) as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    if sha256 is not None:
        actual = _file_sha256(part)
        if actual != sha256:
            part.unlink(missing_ok=True)
            raise InstallError(
                f"Checksum mismatch for {url}\n"
                f"  expected {sha256}\n  got      {actual}\n"
                "The download was corrupted or tampered with. "
                "Click Install to retry."
            )
    part.replace(dest)
    return dest


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- runtime extraction ----------------------------------------------------

def extract_runtime(nupkg: Path, python_dir: Path) -> None:
    """Extract the nupkg's tools/ payload as a FLAT runtime layout.

    The nupkg nests the distribution under tools/. Extracting it
    verbatim leaves python.exe at <dir>\\tools\\python.exe, which breaks
    CPython's prefix detection (pip then installs site-packages OUTSIDE
    the runtime — verified empirically). Stripping the tools/ prefix
    yields the standard layout python.exe + Lib\\ + DLLs\\ side by side.
    """
    python_dir.mkdir(parents=True, exist_ok=True)
    prefix = "tools/"
    with zipfile.ZipFile(nupkg) as z:
        for info in z.infolist():
            name = info.filename
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]
            target = python_dir / rel
            # Zip-slip guard: refuse entries that escape python_dir.
            if not target.resolve().is_relative_to(python_dir.resolve()):
                raise InstallError(f"Refusing unsafe archive path: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst:
                dst.write(src.read())


# ---- launcher + shortcuts ---------------------------------------------------

LAUNCHER_TEMPLATE = '''"""Mynah launcher (written by MynahSetup).

Sets MYNAH_APP_ROOT so config.json, the Recordings folder, and crash
logs live here in the install folder instead of inside site-packages,
then starts the app. Safe to double-click; runs windowless under
pythonw.exe.
"""
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
os.environ["MYNAH_APP_ROOT"] = root

from mynah.app import main

sys.exit(main())
'''


def write_launcher(install_dir: Path) -> Path:
    launcher = install_dir / LAUNCHER_NAME
    launcher.write_text(LAUNCHER_TEMPLATE, encoding="utf-8")
    return launcher


def icon_path(install_dir: Path) -> Path:
    return (
        install_dir / "python" / "Lib" / "site-packages"
        / "mynah" / "assets" / "mynah.ico"
    )


def shortcut_targets(install_dir: Path) -> dict:
    return {
        "target": str(install_dir / "python" / "pythonw.exe"),
        "arguments": f'"{install_dir / LAUNCHER_NAME}"',
        "workdir": str(install_dir),
        "icon": str(icon_path(install_dir)),
    }


def _ps_quote(value: str) -> str:
    """Single-quoted PowerShell string literal (the only escape inside
    single quotes is doubling the quote itself)."""
    return "'" + value.replace("'", "''") + "'"


def build_shortcut_script(lnk_path: Path, install_dir: Path) -> str:
    t = shortcut_targets(install_dir)
    return (
        "$s = (New-Object -ComObject WScript.Shell)"
        f".CreateShortcut({_ps_quote(str(lnk_path))});"
        f"$s.TargetPath = {_ps_quote(t['target'])};"
        f"$s.Arguments = {_ps_quote(t['arguments'])};"
        f"$s.WorkingDirectory = {_ps_quote(t['workdir'])};"
        f"if (Test-Path {_ps_quote(t['icon'])}) "
        f"{{ $s.IconLocation = {_ps_quote(t['icon'])} }};"
        "$s.Description = 'Mynah - local Discord call recorder';"
        "$s.Save()"
    )


def create_shortcut(lnk_path: Path, install_dir: Path, run=subprocess.run) -> None:
    """Create a .lnk via the WScript.Shell COM object through PowerShell.

    PowerShell is present on every supported Windows; using it avoids
    bundling pywin32 into the bootstrap. The script goes through
    -EncodedCommand (base64 UTF-16LE): unlike `-Command <script> args…`
    — where trailing argv items are CONCATENATED into the command string,
    not bound to $args — this has no parsing/quoting hazards at all.
    """
    import base64

    script = build_shortcut_script(lnk_path, install_dir)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    proc = run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encoded,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise InstallError(
            f"Could not create shortcut {lnk_path.name}:\n{proc.stderr.strip()}"
        )


def start_menu_dir() -> Path:
    return Path(
        os.environ.get("APPDATA", str(Path.home()))
    ) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# ---- uninstall: Apps & features registration --------------------------------

# Per-user (HKCU) uninstall key — no elevation needed, mirrors how
# Discord/VS Code user-installs register themselves.
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Mynah"


def version_from_wheel_name(wheel_name: str) -> str:
    """'mynah-1.1.0-py3-none-any.whl' -> '1.1.0' (best-effort)."""
    m = re.match(r"mynah-([0-9][^-]*)-", wheel_name)
    return m.group(1) if m else "0.0.0"


def installed_size_kb(install_dir: Path) -> int:
    """Approximate on-disk size for the Apps & features entry.

    Iterative os.scandir walk: DirEntry caches the stat from the
    directory read, so this is one pass over ~50k files instead of
    rglob's stat-per-path (which adds noticeable time on HDDs right at
    the end of the install).
    """
    total = 0
    stack = [str(install_dir)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total // 1024


def write_uninstall_entry(
    install_dir: Path, version: str, key_path: str = UNINSTALL_KEY,
) -> None:
    """Register the install in HKCU so Windows' 'Apps & features' lists
    Mynah with a working Uninstall button."""
    import winreg

    setup_exe = install_dir / "MynahSetup.exe"
    values = {
        "DisplayName": "Mynah",
        "DisplayVersion": version,
        "Publisher": "ba1lly",
        "DisplayIcon": str(icon_path(install_dir)),
        "InstallLocation": str(install_dir),
        "UninstallString": f'"{setup_exe}" --uninstall "{install_dir}"',
        "URLInfoAbout": "https://github.com/ba1lly/Mynah",
    }
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(
            key, "EstimatedSize", 0, winreg.REG_DWORD,
            min(installed_size_kb(install_dir), 0x7FFFFFFF),
        )
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def remove_uninstall_entry(key_path: str = UNINSTALL_KEY) -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except FileNotFoundError:
        pass


def copy_setup_into_install(install_dir: Path) -> None:
    """Drop a copy of the running MynahSetup.exe into the install dir so
    the registry UninstallString has something durable to point at.
    No-op in dev runs (not frozen)."""
    if not getattr(sys, "frozen", False):
        return
    import shutil

    src = Path(sys.executable)
    dest = install_dir / "MynahSetup.exe"
    if src.resolve() == dest.resolve():
        return
    shutil.copy2(src, dest)


def default_shortcut_paths() -> list[Path]:
    return [
        start_menu_dir() / "Mynah.lnk",
        Path.home() / "Desktop" / "Mynah.lnk",
    ]


def remove_shortcuts(paths: Optional[list[Path]] = None) -> None:
    for lnk in (paths if paths is not None else default_shortcut_paths()):
        try:
            lnk.unlink(missing_ok=True)
        except OSError:
            pass


def looks_like_install(install_dir: Path) -> bool:
    """Whether the directory carries Mynah install markers.

    The uninstall path refuses to touch a directory without them — a
    mangled UninstallString or a manual `--uninstall <wrong dir>` must
    never delete an arbitrary folder's contents.
    """
    return any(
        (install_dir / marker).exists()
        for marker in (STATE_FILENAME, "python", LAUNCHER_NAME)
    )


def keep_names(install_dir: Path) -> set[str]:
    """Top-level names preserved by a keep-user-data uninstall.

    Always Recordings + config.json; additionally, if the user pointed
    Settings -> Recordings folder at a custom directory INSIDE the
    install dir, that directory is preserved too (a renamed recordings
    folder must survive a 'keep my recordings' answer).
    """
    keep = {"Recordings", "config.json"}
    try:
        data = json.loads(
            (install_dir / "config.json").read_text(encoding="utf-8-sig")
        )
        rec = Path(str(data.get("recordings_dir") or ""))
        if rec.is_absolute():
            rel = rec.resolve().relative_to(install_dir.resolve())
            if rel.parts:
                keep.add(rel.parts[0])
    except (OSError, ValueError, json.JSONDecodeError):
        # No config / malformed JSON / recordings_dir outside the
        # install dir (the latter is untouched by the uninstall anyway).
        pass
    return keep


def uninstall(
    install_dir: Path,
    keep_user_data: bool,
    *,
    registry_key: str = UNINSTALL_KEY,
    shortcut_paths: Optional[list[Path]] = None,
) -> None:
    """Remove shortcuts, the registry entry, and the install contents.

    With keep_user_data, the recordings folder(s) and config.json
    survive; everything else (runtime, packages, launcher, state) is
    removed. The running MynahSetup.exe and the (possibly retained)
    folder itself are left for spawn_deferred_cleanup() — call that as
    the LAST thing before process exit, after any farewell dialog, so
    its retry window isn't burned while this process still holds the
    exe lock.
    """
    import shutil

    remove_shortcuts(shortcut_paths)
    remove_uninstall_entry(registry_key)

    keep = keep_names(install_dir) if keep_user_data else set()
    self_exe = install_dir / "MynahSetup.exe"
    for child in list(install_dir.iterdir()):
        if child.name in keep or child == self_exe:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            continue


def spawn_deferred_cleanup(install_dir: Path, keep_user_data: bool) -> None:
    """Detached cmd that removes what the running process cannot: the
    setup exe itself (and the folder, unless user data stays).

    Retries for ~60s: the target stays locked until this process fully
    exits, and antivirus scanners commonly hold freshly-flagged
    binaries open for several seconds beyond that.
    """
    import subprocess as sp

    self_exe = install_dir / "MynahSetup.exe"
    if keep_user_data:
        action, target = f'del /f /q "{self_exe}"', self_exe
    else:
        action, target = f'rmdir /s /q "{install_dir}"', install_dir
    script = (
        f'for /l %i in (1,1,30) do ({action} >nul 2>&1 & '
        f'if not exist "{target}" exit & ping -n 3 127.0.0.1 >nul)'
    )
    sp.Popen(
        ["cmd", "/c", script],
        creationflags=(
            getattr(sp, "CREATE_NO_WINDOW", 0)
            | getattr(sp, "DETACHED_PROCESS", 0)
        ),
        close_fds=True,
    )


# ---- bundled wheel -----------------------------------------------------------

def bundled_wheel() -> Path:
    """Locate the mynah wheel packed into the PyInstaller onefile bundle
    (or, in dev runs, under dist/wheel/)."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "wheel")
    candidates.append(Path(__file__).resolve().parents[1] / "dist" / "wheel")
    for d in candidates:
        if d.is_dir():
            wheels = sorted(d.glob("mynah-*.whl"))
            if wheels:
                return wheels[-1]
    raise InstallError(
        "Bundled mynah wheel not found — this MynahSetup.exe was not "
        "built correctly (see installer/build_installer.py)."
    )


# ---- install state (resume) ---------------------------------------------------

class InstallError(RuntimeError):
    """User-presentable installer failure."""


class InstallState:
    """Tiny persisted set of completed step names."""

    def __init__(self, install_dir: Path):
        self.path = install_dir / STATE_FILENAME
        self.done: set[str] = set()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.done = set(data.get("completed", []))
            except (json.JSONDecodeError, OSError):
                self.done = set()

    def mark(self, step: str) -> None:
        self.done.add(step)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"completed": sorted(self.done)}, indent=2),
            encoding="utf-8",
        )

    def is_done(self, step: str) -> bool:
        return step in self.done


def python_exe(install_dir: Path) -> Path:
    return install_dir / "python" / "python.exe"


# (step id, human label, weight for the progress bar). State keys embed
# the pinned versions so a NEWER MynahSetup run over an old install
# re-runs exactly the steps whose pins changed — nothing else.
STEPS = [
    ("runtime", "Python runtime", 1),
    ("torch", "PyTorch (the big one)", 6),
    ("whisperx", "WhisperX + transcription stack", 3),
    ("app", "Mynah", 1),
    ("launcher", "Launcher + shortcuts", 1),
]


def step_keys(gpu: GpuInfo, wheel_name: str) -> dict[str, str]:
    return {
        "runtime": f"runtime-{PYTHON_VERSION}",
        "torch": f"torch-{TORCH_VERSION}-{'cuda' if gpu.has_cuda else 'cpu'}",
        "whisperx": f"whisperx-{WHISPERX_SPEC}",
        "app": f"app-{wheel_name}",
        "launcher": f"launcher-{wheel_name}",
    }


def pending_steps(install_dir: Path, keys: dict[str, str]) -> list[str]:
    """Which steps still need to run for this install dir.

    Skipping is double-guarded: the state file says a step completed,
    AND the runtime artifact must actually exist — a wiped python/
    folder with a surviving state file would otherwise skip straight to
    pip runs that have no interpreter to run in. (Even with lost state,
    re-running a pip step is cheap: pip sees satisfied pins and does not
    re-download.)
    """
    if not python_exe(install_dir).exists():
        return [step_id for step_id, _, _ in STEPS]
    state = InstallState(install_dir)
    return [
        step_id for step_id, _, _ in STEPS
        if not state.is_done(keys[step_id])
    ]


def run_pip(
    install_dir: Path,
    args: list[str],
    on_line: Optional[Callable[[str], None]] = None,
) -> None:
    """Run pip in the installed runtime, streaming output lines.

    --no-warn-script-location: the runtime's Scripts dir is deliberately
    not on PATH. Retries are pip's own (5 by default); a failed run is
    resumable because pip's HTTP cache keeps completed wheel downloads.
    """
    cmd = [
        str(python_exe(install_dir)), "-m", "pip",
        *args,
        "--no-warn-script-location",
        "--disable-pip-version-check",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line:
            on_line(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise InstallError(
            f"pip {' '.join(args[:2])}… failed (exit {proc.returncode}). "
            "Check your connection and click Install to resume."
        )
