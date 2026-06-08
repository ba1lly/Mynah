"""Development launcher. Run with: python run.py

Relies on the package being installed editable (`pip install -e .`).
Previous versions did `sys.path.insert(0, "src")` which made the launcher
appear to work even when the package wasn't installed properly — the
resulting dev environment then diverged from the frozen `.exe`.
"""
from __future__ import annotations

import sys


def _bail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


def main() -> int:
    try:
        from mynah.app import main as app_main
    except ImportError:
        return _bail(
            "Could not import mynah. Run `.\\start.ps1` (or "
            "`pip install -e .`) once before `python run.py`."
        )
    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())
