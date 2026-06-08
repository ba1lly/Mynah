"""Test our DiscordRPC.connect() flow headlessly — no GUI, no tkinter.

If the inline diagnostic script works but this one hangs, the bug is
specifically in our DiscordRPC class (most likely the background reader
thread interacting badly with the AUTHORIZE wait).

Usage:
    python diagnose_rpc_connect.py
"""
import logging
import sys
from pathlib import Path

# Make sure we use the editable-installed source tree.
sys.path.insert(0, str(Path(__file__).parent / "src"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from mynah.config import Config
from mynah.rpc import DiscordRPC


def main() -> int:
    cfg = Config.load()
    print(f"client_id: {cfg.discord_client_id}")
    print(f"client_secret length: {len(cfg.discord_client_secret)}")
    print(f"cached token: {'present' if cfg.token else 'none'}")
    print()

    rpc = DiscordRPC(cfg.discord_client_id, cfg.discord_client_secret)

    from dataclasses import asdict
    token_dict = asdict(cfg.token) if cfg.token else None

    print("Calling rpc.connect()...")
    try:
        new_token = rpc.connect(existing_token=token_dict)
        print()
        print(f"SUCCESS — connected as {rpc.identity}")
        print(f"new token expires_at: {new_token.get('expires_at')}")
        return 0
    except Exception as e:
        print()
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1
    finally:
        rpc.close()


if __name__ == "__main__":
    raise SystemExit(main())
