"""Entry point: configure logging and launch the GUI."""
from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional


def _candidate_crash_log_paths() -> list[Path]:
    """Ordered crash-log fallback candidates.

    Default install puts the exe next to a writable Recordings/ folder, so
    `<app_root>/crash.log` works for the portable / per-user install
    pattern this app is designed for. But a user can equally install into
    `C:\\Program Files\\Mynah\\`, which is read-only for
    unprivileged processes — and the windowed PyInstaller build runs
    unprivileged. In that case the crash log silently fails to write,
    which is exactly the failure mode the crash log exists to capture.

    Fall back to `%LOCALAPPDATA%\\Mynah\\crash.log` (Windows)
    or `~/.local/share/Mynah/crash.log` (POSIX dev), both of
    which are reliably writable by the user the process runs as.
    """
    from .config import app_root

    candidates: list[Path] = [app_root() / "crash.log"]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Mynah" / "crash.log")
    else:
        candidates.append(
            Path.home() / ".local" / "share" / "Mynah" / "crash.log"
        )
    return candidates


def _write_crash_log(exc: BaseException) -> Optional[Path]:
    """Write traceback to the first writable candidate path.

    Returns the path actually used, or None if all candidates failed
    (genuinely-broken environment — disk full, every candidate denied).
    """
    for path in _candidate_crash_log_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now().isoformat()} =====\n")
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
            return path
        except Exception:
            # Most likely PermissionError on a Program Files install for
            # the first candidate. Fall through to the next.
            continue
    return None


def _silence_known_benign_warnings() -> None:
    """Issue #7: suppress two cosmetic warnings from bundled ML deps.

    torchcodec is pyannote's *optional* audio decoder; our pipeline never
    feeds pyannote a file path that needs decoding (audio is pre-loaded
    via soundfile / whisperx's loader), so the failing-DLL warning it
    emits 4x at import time is dead-code noise. The filter is scoped to
    that exact message so new, unrelated UserWarnings still surface.

    (The companion suppression — Lightning's checkpoint auto-upgrade
    INFO — lives in transcription.py next to whisperx.load_model.)
    """
    import warnings

    warnings.filterwarnings(
        "ignore",
        message=r"torchcodec is not installed correctly",
        category=UserWarning,
    )


def _run_legacy_tk_ui() -> int:
    import tkinter as tk

    from .gui import MainWindow

    root = tk.Tk()
    try:
        from tkinter import ttk

        ttk.Style().theme_use("vista")
    except tk.TclError:
        # Non-Windows or older Tk — fall back to whatever's default.
        pass
    MainWindow(root)
    root.mainloop()
    return 0


def main() -> int:
    # Bare-minimum logging until the UI installs its log handler. The UI
    # then removes other handlers, so this only matters for messages emitted
    # during startup and for diagnostic logs in frozen builds where stderr
    # is discarded.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    _silence_known_benign_warnings()

    try:
        if "--legacy-ui" in sys.argv[1:]:
            return _run_legacy_tk_ui()

        try:
            from .webui import run as run_webui

            return run_webui()
        except ImportError as e:
            # pywebview not installed (stale venv). The Tk UI needs nothing
            # beyond the stdlib, so degrade instead of dying.
            logging.warning(
                "Web UI unavailable (%s); falling back to legacy Tk UI. "
                "Run start.ps1 to install pywebview.", e,
            )
            return _run_legacy_tk_ui()
        except Exception as e:
            # Most likely: WebView2 runtime genuinely absent, so the
            # edgechromium backend refused to start. Same degrade path.
            logging.warning(
                "Web UI failed to start (%s); falling back to legacy Tk UI. "
                "Install the Microsoft Edge WebView2 runtime for the new UI.",
                e,
            )
            return _run_legacy_tk_ui()
    except BaseException as exc:  # noqa: BLE001
        logging.exception("Fatal error in main()")
        log_path = _write_crash_log(exc)
        # In a frozen, windowed PyInstaller build there is no console for
        # stderr. Try to surface the error to the user via a native dialog
        # before exiting so they don't see the window just vanish.
        if log_path is not None:
            detail = f"Details written to:\n{log_path}"
        else:
            detail = (
                "Could not write a crash log to disk "
                "(no writable location available)."
            )
        message = f"{type(exc).__name__}: {exc}\n\n{detail}"
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Mynah — fatal error", message)
            root.destroy()
        except Exception:
            # The bootstrap-installed runtime (issue #9) ships without
            # tkinter — fall back to the Win32 message box so the user
            # still sees *something* instead of a silently-vanished app.
            if sys.platform == "win32":
                try:
                    import ctypes

                    ctypes.windll.user32.MessageBoxW(
                        None, message, "Mynah — fatal error", 0x10,  # MB_ICONERROR
                    )
                except Exception:
                    pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
