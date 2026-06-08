"""Pre-download Whisper and pyannote model weights to the Hugging Face cache.

Run via: python -m mynah.prefetch

- Whisper turbo model (~1.6 GB) is always fetched.
- Pyannote diarization + segmentation (~40 MB total) are fetched only if a
  Hugging Face token is configured in config.json (i.e. the user has accepted
  the model licenses and wants speaker labels).

Idempotent: Hugging Face's snapshot_download skips files already cached.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Optional

from .config import Config

PYANNOTE_REPOS = [
    "pyannote/speaker-diarization-community-1",  # gated; preferred for accuracy
    "pyannote/segmentation-3.0",                  # dependency of community-1
]


@contextmanager
def _hf_token_env(token: Optional[str]):
    """Set HUGGING_FACE_HUB_TOKEN for the duration of a download.

    faster_whisper.utils.download_model does not accept a token kwarg
    directly, but it delegates to huggingface_hub.snapshot_download which
    honours this env var. Restoring the prior value afterwards keeps the
    side effect scoped to the download call, so importing prefetch from
    a wider context (e.g. tests) doesn't leak credentials into globals.

    On (token inheritable by sibling
    subprocesses via `os.environ`): prefetch.py is invoked exclusively
    as `python -m mynah.prefetch` from `start.ps1` — a
    standalone process tree. The cited risk sources (rpc.py
    Authenticode `pwsh` check, gui.py `open`/`xdg-open`,
    transcription.py ffmpeg) all live in the GUI process and never
    share an address space with this context manager. The env-var
    window therefore only leaks to subprocesses prefetch itself
    spawns, and the download libraries used here do not spawn any.
    """
    if not token:
        yield
        return
    keys = ("HUGGING_FACE_HUB_TOKEN", "HF_TOKEN")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ[k] = token
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _download_whisper(model_name: str, token: Optional[str]) -> bool:
    """Download Whisper weights into the HF cache.

    The `token` is propagated via the HUGGING_FACE_HUB_TOKEN env var so
    that authenticated repos (e.g. a private fine-tune the user pointed
    at via Settings → Whisper Model) actually use the configured token
    instead of falling back to anonymous and 401-ing.
    """
    print(f"  > whisper:{model_name}", flush=True)
    try:
        from faster_whisper.utils import download_model

        with _hf_token_env(token):
            download_model(model_name, cache_dir=None, local_files_only=False)
        return True
    except Exception as e:
        print(f"    error: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _snapshot(repo_id: str, token: Optional[str], gated: bool) -> bool:
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import HfHubHTTPError

    print(f"  > {repo_id}", flush=True)
    try:
        snapshot_download(repo_id=repo_id, token=token)
        return True
    except HfHubHTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            if gated:
                print(
                    f"    rejected ({status}): accept the license at "
                    f"https://huggingface.co/{repo_id} and make sure your HF "
                    f"token has Read scope.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"    rejected ({status}): Hugging Face required auth for "
                    f"this download. Add an HF token in Edit -> Settings (any "
                    f"Read-scope token works, the model itself is public).",
                    file=sys.stderr,
                )
        else:
            print(f"    HTTP error: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"    error: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def main() -> int:
    cfg = Config.load()
    print("Prefetching model weights (caches them so first transcription is instant)...")

    token = cfg.hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or None
    ok = True

    print("Whisper model:")
    ok &= _download_whisper(cfg.whisper_model, token=token)

    # WhisperX word-alignment model. Required for per-word speaker labels
    # (the assign_word_speakers path); without it the transcript falls
    # back to one speaker per Whisper segment, which mis-attributes
    # cross-talk. English-only — covers our actual workload; non-English
    # transcriptions fall through to runtime download on first use.
    print("WhisperX alignment model (English):")
    try:
        import whisperx
        whisperx.load_align_model(language_code="en", device="cpu")
        print("  > whisperx:align:en (cached)")
    except Exception as e:
        print(f"  error: {type(e).__name__}: {e}", file=sys.stderr)
        ok = False

    if token:
        print("Pyannote diarization models:")
        for repo in PYANNOTE_REPOS:
            ok &= _snapshot(repo, token=token, gated=True)
    else:
        print(
            "Pyannote models: skipped (no HF token in settings or HF_TOKEN env). "
            "Add one in Edit -> Settings to enable speaker labels on non-Discord recordings."
        )

    print("Done." if ok else "Done with some failures - see messages above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
