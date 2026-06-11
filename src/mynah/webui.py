"""Launch the pywebview-based UI (the default UI since v1.0.1).

Renders src/mynah/web/index.html in the OS webview (WebView2 on
Windows) with MynahBackend as the JS bridge. The legacy Tkinter UI in
gui.py remains available via --legacy-ui.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import __version__
from .backend import MynahBackend, WebLogHandler
from .config import Config

log = logging.getLogger(__name__)


def _web_asset(name: str) -> Path:
    """Resolve a bundled web asset both in dev and in the frozen build.

    PyInstaller places the datas at ``_internal/mynah/web/`` which is
    exactly ``Path(__file__).parent / "web"`` for the frozen module, so
    one expression covers both layouts.
    """
    return Path(__file__).resolve().parent / "web" / name


def _apply_windows_branding(window) -> None:
    """Make the window/taskbar show the Mynah icon on Windows.

    In the frozen build the exe's own icon covers this, but the
    bootstrap-installed app runs under pythonw.exe — without these two
    calls the taskbar shows the Python rocket. Both are best-effort.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        # Stop the taskbar from grouping us under pythonw.exe's identity.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ba1lly.Mynah"
        )
    except Exception:
        pass

    def _set_icon(*_args) -> None:
        try:
            ico = Path(__file__).resolve().parent / "assets" / "mynah.ico"
            if not ico.exists():
                return
            hwnd = ctypes.windll.user32.FindWindowW(None, window.title)
            if not hwnd:
                return
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
            for which, dim in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
                hicon = ctypes.windll.user32.LoadImageW(
                    None, str(ico), IMAGE_ICON, dim, dim, LR_LOADFROMFILE,
                )
                if hicon:
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, which, hicon)
        except Exception:
            log.debug("Could not apply window icon", exc_info=True)

    window.events.shown += _set_icon


def _wire_logging(backend: MynahBackend) -> None:
    """Make the frontend log pane THE log destination (mirrors
    gui._wire_logging): remove the bootstrap stderr handler so every
    line isn't printed twice with different formatters."""
    handler = WebLogHandler(backend)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for h in list(root_logger.handlers):
        if not isinstance(h, WebLogHandler):
            root_logger.removeHandler(h)
    root_logger.addHandler(handler)


def run() -> int:
    import webview  # deferred: keeps `import mynah.webui` cheap for tooling

    index = _web_asset("index.html")
    if not index.is_file():
        raise FileNotFoundError(
            f"Web UI assets missing: {index} — broken install/build."
        )

    config = Config.load()
    backend = MynahBackend(config)

    window = webview.create_window(
        f"Mynah {__version__}",
        url=index.as_uri(),
        js_api=backend,
        width=900,
        height=720,
        min_size=(760, 560),
        # Matches the frontend's dark default so the first paint isn't a
        # white flash before the stylesheet loads.
        background_color="#101014",
    )
    backend._attach_window(window)
    window.events.closing += backend._on_window_closing
    _apply_windows_branding(window)

    _wire_logging(backend)
    backend._start_environment_check()

    # Force the Chromium-based backend: pywebview's MSHTML fallback is the
    # IE engine and cannot render the frontend. WebView2 ships with
    # Windows 11 and current Windows 10; if it's genuinely absent this
    # raises and app.main() falls back to the legacy Tk UI with a clear
    # log message.
    webview.start(gui="edgechromium")
    return 0
