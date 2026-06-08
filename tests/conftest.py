"""Shared pytest fixtures + Windows-only module mocks.

Several source modules (audio.py, gui.py, rpc.py) import Windows-only
packages at the top level. To run logic tests on Linux / CI, we install
lightweight stubs in sys.modules BEFORE the tests import the source
modules. This is the recommended pytest pattern for cross-platform
test execution of Windows-targeted code.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _install_stub(name: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = MagicMock()
    mod.__name__ = name
    sys.modules[name] = mod
    return mod


# Audio capture — Windows-only pyaudiowpatch stub.
_install_stub("pyaudiowpatch")

# Windows RPC — pywin32 stubs (rpc.py imports these lazily inside methods,
# but the conftest covers anything that hoists them to module scope).
for _name in (
    "win32file", "win32pipe", "pywintypes",
    "win32api", "win32con", "win32process",
):
    _install_stub(_name)

# Tkinter — present on most desktops; mocked here so logic tests can run
# in headless CI containers that lack the python3-tk package.
try:
    import tkinter  # noqa: F401
except ImportError:
    tk_stub = _install_stub("tkinter")
    tk_stub.TclError = type("TclError", (Exception,), {})
    _install_stub("tkinter.ttk")
    _install_stub("tkinter.filedialog")
    _install_stub("tkinter.messagebox")
    _install_stub("tkinter.scrolledtext")
