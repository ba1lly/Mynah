# Mynah bootstrap.
# Idempotent: creates the venv if missing, installs any missing dependency,
# then launches the app. Safe to run any number of times.
#
# Usage (from this folder):
#   .\start.ps1
#
# Optional flags:
#   .\start.ps1 -Build     # build the standalone .exe instead of launching
#   .\start.ps1 -Force     # reinstall everything from scratch

[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$VenvDir = Join-Path $PSScriptRoot ".venv"
$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Has-Module($name) {
    # Wrapped in try/except + explicit import so a bad probe never writes to
    # stderr (which PS 5.1 turns into a NativeCommandError with Stop policy).
    $probe = "import sys`ntry:`n    from importlib.util import find_spec`n    sys.exit(0 if find_spec('$name') else 1)`nexcept Exception:`n    sys.exit(1)"
    & $VenvPy -c $probe 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-Prefetch {
    Write-Step "Prefetching model weights"
    & $VenvPy -m mynah.prefetch
    if ($LASTEXITCODE -ne 0) {
        # Don't throw — the README explicitly mentions prefetch failures
        # can be retried by re-running the bootstrap, and the app will
        # transparently download missing weights on first transcription.
        # But surface it clearly so the user knows the launch may stall
        # later instead of starting transcription instantly.
        Write-Warning "Prefetch step had failures. First transcription may need to download missing weights."
    }
}

function Invoke-CudaSanityCheck {
    Write-Step "CUDA sanity check"
    # Guard with device_count() > 0 so this doesn't throw when CUDA is
    # available but no usable GPU device exists (e.g. MIG partitioning,
    # driver hiccup, GPU eject mid-session). With ErrorActionPreference = Stop
    # an IndexError here would otherwise abort the script before launch.
    $sanityScript = @"
import torch
print('CUDA available:', torch.cuda.is_available())
if torch.cuda.is_available() and torch.cuda.device_count() > 0:
    print('Device:', torch.cuda.get_device_name(0))
else:
    print('Device: CPU only')
"@
    & $VenvPy -c $sanityScript
}

# --- 1. venv ----------------------------------------------------------------

if ($Force -and (Test-Path $VenvDir)) {
    Write-Step "Removing existing venv (-Force)"
    Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $VenvPy)) {
    Write-Step "Creating virtual environment at .venv"
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "python -m venv failed. Is Python 3.10, 3.11, 3.12, or 3.13 installed and on PATH?"
    }
} else {
    Write-Host "Venv already exists." -ForegroundColor DarkGray
}

Write-Step "Upgrading pip"
& $VenvPy -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

# --- 2. base deps -----------------------------------------------------------

Write-Step "Checking base dependencies"
$baseNeeded = @()
if (-not (Has-Module "numpy"))         { $baseNeeded += "numpy>=1.24" }
if (-not (Has-Module "pyaudiowpatch")) { $baseNeeded += "PyAudioWPatch>=0.2.12.7" }
if (-not (Has-Module "win32file"))     { $baseNeeded += "pywin32>=306" }
if (-not (Has-Module "requests"))      { $baseNeeded += "requests>=2.31" }
if (-not (Has-Module "scipy"))         { $baseNeeded += "scipy>=1.10" }
if (-not (Has-Module "soundfile"))     { $baseNeeded += "soundfile>=0.12" }
if (-not (Has-Module "imageio_ffmpeg")){ $baseNeeded += "imageio-ffmpeg>=0.5" }
# Issue #14: OS credential store (Windows Credential Manager via the
# wincred backend). Without this, secrets fall back to plaintext
# config.json — the exact regression #14 was meant to close.
if (-not (Has-Module "keyring"))       { $baseNeeded += "keyring>=24" }
# Web UI (default since v1.0.1): pywebview renders src/mynah/web/ in
# WebView2. Without it the app degrades to the legacy Tk UI.
if (-not (Has-Module "webview"))       { $baseNeeded += "pywebview>=5.1" }

if ($baseNeeded.Count -gt 0) {
    Write-Host ("Installing: " + ($baseNeeded -join ", "))
    & $VenvPy -m pip install @baseNeeded
    if ($LASTEXITCODE -ne 0) { throw "Base dependency install failed." }
} else {
    Write-Host "All base deps already installed." -ForegroundColor DarkGray
}

# --- 3. WhisperX ------------------------------------------------------------
# Installed BEFORE PyTorch so pip's resolver picks a torch version that
# matches whisperx's pin. Step 4 then force-reinstalls the matching CUDA
# build (whisperx pulls the CPU-only wheel from PyPI by default).
#
# Pinned to a specific PyPI release rather than `git+https://...` HEAD.
# A pinned release means: (1) reproducible builds, (2) supply-chain
# risk is bounded to PyPI's content-addressed registry, (3) the torch
# pin in step 4 lines up reliably with what whisperx declares.

$WhisperXSpec    = "whisperx==3.8.6"
$WhisperXVersion = "3.8.6"

function Get-WhisperXVersion {
    $script = @"
import sys
try:
    from importlib.metadata import version, PackageNotFoundError
    print(version('whisperx'))
except PackageNotFoundError:
    print('missing')
except Exception as e:
    print(f'error:{e}')
"@
    return ((& $VenvPy -c $script 2>&1) -join "`n").Trim()
}

# Enforce the EXACT pinned version, not just any importable whisperx. A
# dev venv with an older git-installed whisperx (or a newer release that
# breaks the torch 2.8 alignment) was previously accepted as-is and
# undermined the reproducibility claim the pin is supposed to provide.
Write-Step "Checking WhisperX"
$installedWhisperX = Get-WhisperXVersion
if ($installedWhisperX -eq $WhisperXVersion) {
    Write-Host "WhisperX $WhisperXVersion already installed." -ForegroundColor DarkGray
} else {
    if ($installedWhisperX -eq "missing") {
        Write-Host "Installing $WhisperXSpec from PyPI..."
    } else {
        Write-Host "Installed WhisperX is '$installedWhisperX'; replacing with $WhisperXSpec to match pinned torch."
    }
    & $VenvPy -m pip install --upgrade --force-reinstall $WhisperXSpec
    if ($LASTEXITCODE -ne 0) { throw "WhisperX install failed." }
}

# --- 4. PyTorch with CUDA --------------------------------------------------
# Detects missing / CPU-only / wrong-version torch and reinstalls.
# WhisperX 3.8.5 requires torch~=2.8.0 and torchvision~=0.23.0; update these
# constants together when bumping whisperx.

$TorchVersion       = "2.8.0"
$TorchAudioVersion  = "2.8.0"
$TorchVisionVersion = "0.23.0"
$TorchCudaIndex     = "https://download.pytorch.org/whl/cu126"

function Get-TorchState {
    $script = @"
import sys
try:
    import torch
except ImportError:
    print('missing'); sys.exit(0)
if not torch.cuda.is_available():
    print('cpu-only'); sys.exit(0)
v = torch.__version__.split('+')[0].split('.')
major, minor = int(v[0]), int(v[1])
if (major, minor) == (2, 8):
    print('ok')
else:
    print('wrong-version')
"@
    return ((& $VenvPy -c $script 2>&1) -join "`n").Trim()
}

Write-Step "Checking PyTorch / CUDA"
$torchState = Get-TorchState
switch ($torchState) {
    "ok" {
        Write-Host "PyTorch is CUDA-enabled and version-compatible." -ForegroundColor DarkGray
    }
    default {
        Write-Host "PyTorch state: $torchState. Installing pinned CUDA build (~2.5 GB)..." -ForegroundColor Yellow
        # Use --extra-index-url so transitive deps of torch still resolve
        # from PyPI, not just the pytorch wheel index. --index-url would
        # replace PyPI entirely, which leaves transitive resolution to a
        # registry that may not mirror every package needed.
        & $VenvPy -m pip install --force-reinstall `
            "torch==$TorchVersion" `
            "torchaudio==$TorchAudioVersion" `
            "torchvision==$TorchVisionVersion" `
            --extra-index-url $TorchCudaIndex
        if ($LASTEXITCODE -ne 0) { throw "PyTorch CUDA install failed." }
    }
}

# --- 5. Project itself (editable) ------------------------------------------

Write-Step "Ensuring project is installed (editable)"
& $VenvPy -m pip install -e . --quiet
if ($LASTEXITCODE -ne 0) { throw "Project editable install failed." }

# --- 6. CUDA sanity + prefetch (run in BOTH launch and Build paths) -------
# Previously the Build path skipped these, meaning a developer could build
# a .exe on a machine with broken CUDA / unpopulated HF cache and ship it
# without realising. Now both paths run them.

Invoke-CudaSanityCheck
Invoke-Prefetch

# --- 7. Build path or launch path ------------------------------------------

if ($Build) {
    Write-Step "Checking PyInstaller"
    if (-not (Has-Module "PyInstaller")) {
        & $VenvPy -m pip install "pyinstaller>=6.3"
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller install failed." }
    }
    # Fail-fast: warn if the existing .exe is still running, since PyInstaller
    # cannot delete locked files.
    $running = Get-Process Mynah -ErrorAction SilentlyContinue
    if ($running) {
        throw "Mynah.exe is currently running (PID $($running.Id)). Close it before rebuilding."
    }

    Write-Step "Building Mynah.exe (this can take 10+ minutes)"
    & $VenvPy build.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed (exit $LASTEXITCODE). See output above."
    }

    $BuiltExe = Join-Path $PSScriptRoot "dist\Mynah\Mynah.exe"
    if (-not (Test-Path $BuiltExe)) {
        throw "Build reported success but $BuiltExe is missing. Something went wrong in PyInstaller."
    }

    # Seed the built app with NON-SECRET settings from the dev config so
    # things like recordings_dir / whisper_model / audio_source carry
    # over. The previous behaviour copied config.json verbatim, which
    # leaked the developer's Discord Client Secret, HF token, and live
    # OAuth refresh token into the distributable folder — anyone who
    # received the dist/ tree could then impersonate the developer's
    # Discord application or read their HF account.
    $DevConfig  = Join-Path $PSScriptRoot "config.json"
    $DistConfig = Join-Path $PSScriptRoot "dist\Mynah\config.json"
    if (Test-Path $DevConfig) {
        Write-Step "Seeding the built app with sanitised settings (no secrets)"
        # The keep-list is hardcoded here, but Config's schema lives in
        # config.py. Drift between the two is the failure mode that
        # leaks the next-added secret into the dist tree. The script
        # below asserts that:
        #   1. Every field in keep IS a real Config field (no typos);
        #   2. Every field marked secret in Config._SECRET_FIELDS is
        #      NOT in keep (the secret hasn't been accidentally added);
        #   3. Every non-secret Config field IS in keep (no new field
        #      has been silently dropped from the seeded config).
        # Build fails loudly on any mismatch so the developer must
        # consciously update both sides.
        $scrubScript = @"
import json, sys, pathlib
from dataclasses import fields
src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])

# Import Config from the editable-installed package so we can validate
# the keep-list against the canonical schema rather than a hardcoded
# duplicate.
try:
    from mynah.config import Config, _SECRET_FIELDS, _SHADOW_FIELDS
except Exception as e:
    print(f'  ERROR: could not import mynah.config: {e}')
    sys.exit(1)

keep = {'discord_client_id', 'recordings_dir', 'whisper_model', 'audio_source', 'loopback_device_name', 'check_updates'}
all_fields = {f.name for f in fields(Config)}

unknown = keep - all_fields
if unknown:
    print(f'  ERROR: keep-list references non-existent Config fields: {sorted(unknown)}')
    sys.exit(1)

leaks = keep & set(_SECRET_FIELDS)
if leaks:
    print(f'  ERROR: keep-list contains secret fields: {sorted(leaks)}')
    sys.exit(1)

# _SHADOW_FIELDS are runtime-only in-memory mirrors of secret values for
# installs where the keyring backend is unavailable; by design they are
# never persisted to config.json. Exclude them from the keep-list
# coverage check (they are neither secret-on-disk nor seedable).
missing = (all_fields - set(_SECRET_FIELDS) - set(_SHADOW_FIELDS)) - keep
if missing:
    print(f'  ERROR: keep-list is missing non-secret Config fields: {sorted(missing)}')
    print(f'         Either add them to keep, or add them to _SECRET_FIELDS in config.py.')
    sys.exit(1)

try:
    raw = json.loads(src.read_text(encoding='utf-8'))
except Exception as e:
    print(f'  could not read {src}: {e}')
    sys.exit(0)
if not isinstance(raw, dict):
    print('  config.json root is not an object; skipping')
    sys.exit(0)
scrubbed = {k: v for k, v in raw.items() if k in keep}
# Always set token to None so the built app prompts for fresh auth.
scrubbed['token'] = None
dropped = [k for k in raw.keys() if k not in keep]
if dropped:
    print(f'  dropped non-kept keys: {sorted(dropped)}')
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(scrubbed, indent=2), encoding='utf-8')
print(f'  wrote {dst} (kept keys: {sorted(scrubbed)})')
"@
        & $VenvPy -c $scrubScript "$DevConfig" "$DistConfig"
        if ($LASTEXITCODE -ne 0) {
            throw "Config seeding failed. The build is aborted to prevent a stale or leaky distribution config."
        }
    }

    Write-Host ""
    Write-Host "Done. Your .exe is at:" -ForegroundColor Green
    Write-Host "  $BuiltExe"
    exit 0
}

# --- 8. Launch -------------------------------------------------------------

Write-Step "Launching Mynah"
& $VenvPy run.py
exit $LASTEXITCODE
