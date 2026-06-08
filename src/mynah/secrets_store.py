"""OS credential store for Discord OAuth tokens, HuggingFace tokens, and
the (legacy) Discord client secret — issue #14.

Wraps the `keyring` library, which delegates to:
  - Windows: Credential Manager (via the `wincred` backend)
  - macOS: Keychain
  - Linux: Secret Service (libsecret) or KWallet

Why we moved off `config.json`
-------------------------------
The previous design stored credentials in a JSON file next to the
executable (`<app_root>/config.json`). On NTFS the file was protected
only by inherited ACLs, which do nothing against same-user threats —
any process running as the user could read it. originally added atomic
writes and POSIX 0600 mode as defence-in-depth, but the structural
fix is to never write the secrets to disk in cleartext at all.

The OS credential stores listed above all encrypt at rest with a key
derived from the user's login credentials. Reading the secret from a
sibling process requires either the user's password (Linux/macOS
prompt) or explicit Windows API calls that Process Monitor logs.

Graceful fallback
-----------------
If `keyring` is not installed or no backend is available (e.g., a
headless Linux container with no D-Bus), every function in this module
falls back to a no-op that returns `None` and logs a clear warning.
The caller (`Config`) catches the warning and keeps reading from
`config.json` as before — secrets stay in cleartext, but the app
keeps working.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)


class SecretWriteError(RuntimeError):
    """Raised when keyring is available but the backend refused/failed.

    Distinct from "keyring unavailable" (which `set_secret` signals by
    returning False). The caller must NOT silently downgrade to
    plaintext fallback in this case: a transient backend failure on a
    keyring-capable install would otherwise persist secrets to
    config.json with no user-visible signal — the #14 regression
    
    """


# Reverse-DNS-style vendor-qualified service name. The previous
# generic `"mynah"` could have collided with any other
# package using `keyring` under the same string — wincred / Keychain /
# Secret Service all key entries by exact (service, account) tuples
# with no namespace isolation between processes. This name has never
# shipped on a tagged release, so no migration is required.
SERVICE_NAME = "com.github.ba1lly.mynah"

KEY_DISCORD_CLIENT_SECRET = "discord-client-secret"
KEY_DISCORD_TOKEN = "discord-token"
KEY_HUGGINGFACE_TOKEN = "huggingface-token"

ALL_KEYS = (
    KEY_DISCORD_CLIENT_SECRET,
    KEY_DISCORD_TOKEN,
    KEY_HUGGINGFACE_TOKEN,
)


# Module-level cache for the keyring module + the "warning already
# logged" flag. The previous version re-ran `import keyring` and
# re-emitted the warning on EVERY get/set/is_available call — once
# the library was confirmed missing on a process, the log filled with
# duplicate WARNING lines for every Settings interaction. Python's
# import cache makes the import itself cheap; the goal here is to log
# the unavailability warning at most once per process lifetime.
#
# cache mutations are guarded by `_kr_lock` so two
# threads calling `_keyring()` concurrently during the first-use window
# can't both emit the unavailability warning. The lock is held only
# during cache population, not on the hot read path (`_kr_module is not
# _KR_UNRESOLVED`).
_KR_UNRESOLVED = object()
_kr_module: object = _KR_UNRESOLVED
_kr_warning_logged = False
_kr_lock = threading.Lock()


def _keyring():
    """Return the `keyring` module if importable, else None.

    The result is cached at module scope under a lock, and the
    "unavailable" warning is emitted at most once per process.
    """
    global _kr_module, _kr_warning_logged
    if _kr_module is not _KR_UNRESOLVED:
        return _kr_module
    with _kr_lock:
        if _kr_module is not _KR_UNRESOLVED:
            return _kr_module
        try:
            import keyring as _kr
            _kr_module = _kr
            return _kr
        except Exception as e:
            _kr_module = None
            if not _kr_warning_logged:
                log.warning(
                    "keyring library unavailable (%s); secrets cannot be "
                    "stored in the OS credential store and will fall back "
                    "to config.json. Install `keyring` to enable "
                    "encrypted-at-rest credential storage.",
                    e,
                )
                _kr_warning_logged = True
            return None


def get_secret(key: str) -> Optional[str]:
    """Read a secret from the OS credential store.

    Returns None if the key has never been set, if keyring is not
    available, or if the backend refuses (e.g., user cancelled a
    macOS Keychain prompt). A logged warning records the failure
    mode so an operator can distinguish "no secret stored yet" from
    "backend failure".
    """
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE_NAME, key)
    except Exception as e:
        log.warning(
            "keyring read for %s failed (%s); treating as absent.", key, e,
        )
        return None


def set_secret(key: str, value: Optional[str]) -> bool:
    """Persist a secret. `None` or empty string deletes the entry.

    Return values:
      - True: written to the OS credential store successfully.
      - False: keyring is NOT available on this install (no library,
        null backend). Caller falls back to plaintext shadow.

    Raises:
      - SecretWriteError: keyring IS available but the backend
        refused/failed (transient D-Bus / wincred / Keychain error).
        The caller MUST NOT silently write plaintext in this case —
        a transient hiccup on a keyring-capable install would
        otherwise downgrade the install to plaintext-at-rest with
        only a log warning.
    """
    kr = _keyring()
    if kr is None:
        return False
    # the docstring's "False = no library or null
    # backend" promise required this check. Previously set_secret only
    # returned False when the library was absent; a system with keyring
    # installed but no usable backend (no D-Bus on Linux, no Keychain
    # access, etc.) raised SecretWriteError instead of taking the
    # documented plaintext-fallback path. Treating a null backend as
    # unavailable here matches `is_available()`'s contract and lets
    # Config setters round-trip through `_legacy_*` shadows as
    # designed, rather than aborting Settings with a backend error.
    if not _backend_usable(kr):
        return False
    # resolve PasswordDeleteError from the active
    # `kr` instance rather than a module-level cache. The module-level
    # cache approach (initial V29 fix) broke tests that monkeypatch
    # `_keyring` to return a fake, because the cache stays at the
    # `Exception` fallback and the except clause then swallows real
    # backend failures. `keyring.errors.PasswordDeleteError` is the
    # documented public API of the keyring library, so reading it at
    # call time is not the "internal module layout coupling" the
    # review suggested.
    password_delete_error = getattr(
        getattr(kr, "errors", None),
        "PasswordDeleteError",
        Exception,
    )
    try:
        if not value:
            try:
                kr.delete_password(SERVICE_NAME, key)
            except password_delete_error:
                # No prior entry — that is fine; the post-condition is
                # "no value stored for this key", which is now true.
                pass
            return True
        kr.set_password(SERVICE_NAME, key, value)
        return True
    except Exception as e:
        log.warning("keyring write for %s failed (%s).", key, e)
        raise SecretWriteError(
            f"keyring write for {key} failed: {e}"
        ) from e


def delete_secret(key: str) -> bool:
    """Convenience wrapper for unconditional deletion."""
    return set_secret(key, None)


def clear_all() -> tuple[set[str], set[str]]:
    """Best-effort wipe of every key this module manages.

    Returns `(cleared, failed)` — two sets of key names. `cleared`
    contains the keys that are now confirmed absent (either deleted
    or already absent). `failed` contains keys whose deletion raised
    `SecretWriteError` and may still hold a value in the credential
    store.

    the previous signature returned `None`, so a
    caller wiping credentials (Settings → Clear stored credentials,
    laptop-transfer prep, suspected-compromise response) could not
    distinguish "wiped" from "wiped with stuck residuals". Returning
    the two sets lets the UI surface manual-cleanup guidance for
    `failed` entries instead of falsely reporting success.

    Never raises — partial failures are captured in `failed` and
    logged. Intended for the Settings UI action above and for test
    teardown.
    """
    cleared: set[str] = set()
    failed: set[str] = set()
    for key in ALL_KEYS:
        try:
            delete_secret(key)
            cleared.add(key)
        except SecretWriteError as e:
            log.warning(
                "clear_all: keyring delete for %s failed (%s); manual "
                "cleanup via OS credential manager required.",
                key,
                e,
            )
            failed.add(key)
    return cleared, failed


def _backend_usable(kr) -> bool:
    """True iff `kr.get_keyring()` returns a real (non-null) backend.

    Factored out so `set_secret` and `is_available` agree on what
    "available" means — the regression was that
    `set_secret` checked only `kr is None` while `is_available` checked
    the backend.name. Two definitions of "available" cause callers to
    take incompatible fallback paths for the same install state.
    """
    try:
        backend = kr.get_keyring()
        return bool(
            getattr(backend, "name", "") and backend.name != "null"
        )
    except Exception:
        return False


def is_available() -> bool:
    """True iff keyring is importable AND a working backend is selected.

    Used by `Config.load()` to decide whether to attempt migration of
    legacy secrets out of config.json.
    """
    kr = _keyring()
    if kr is None:
        return False
    return _backend_usable(kr)
