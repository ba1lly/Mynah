"""Persistent configuration. Lives next to the app — config.json and the
Recordings folder both sit in the install/project directory, so everything
the app produces stays together and is easy to back up or move.

Issue #14: secrets (Discord client_secret, HuggingFace token, OAuth
access/refresh tokens) are NOT written to config.json on installs where
the OS credential store is available. They live in:

  - Windows: Credential Manager (via keyring's wincred backend)
  - macOS: Keychain
  - Linux: Secret Service / KWallet

`Config.load()` performs a one-shot migration: any pre-#14 config.json
that still contains plaintext secrets has them moved into the OS
credential store and removed from the JSON on the next save. The JSON
on disk then holds only non-sensitive settings.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from . import secrets_store

log = logging.getLogger(__name__)


_VALID_AUDIO_SOURCES = {"mixed", "mic_only", "system_only"}
_DEFAULT_WHISPER_MODEL = "large-v3-turbo"

# JSON keys that historically held live credentials. We still recognise
# them in `Config.load()` for migration purposes — they are read out,
# moved into the OS credential store, and stripped from the saved JSON
# the next time `Config.save()` runs. The set is also the source of
# truth for the .bad-backup secret-scrubber and the start.ps1 -Build
# dist-config keep-list assertion.
_SECRET_FIELDS = frozenset({
    # discord_client_secret is no longer used (PKCE, issue #1) but stays
    # in this set so the .bad-backup scrubber keeps redacting it from
    # legacy configs and the start.ps1 keep-list assertion keeps
    # rejecting it from dist configs.
    "discord_client_secret",
    "hf_token",
    "token",
})

# Explicit allow-list of private dataclass field names that are NOT
# persisted to config.json — they hold the in-memory shadow of secret
# values for installs without a working keyring backend. v2
# the previous prefix filter (`startswith("_legacy_")`)
# would silently drop any future field that happened to start with
# `_legacy_` (e.g. a `_legacy_audit_path` migration metadata field).
# Listing them explicitly here makes the contract typo-resistant and
# IDE-discoverable, and the dataclass field definitions reference this
# set in a class-body sanity check at import time.
_SHADOW_FIELDS = frozenset({
    "_legacy_hf_token",
    "_legacy_token",
})


def app_root() -> Path:
    """The directory the user thinks of as 'where the app lives'.

    - MYNAH_APP_ROOT env var, when set: the bootstrap-installer layout
      (issue #9). There the package lives in <install>\\python\\Lib\\
      site-packages, so neither heuristic below lands anywhere a user
      would look for config.json/Recordings; the installer's launcher
      sets the env var to the install dir before importing mynah.
    - In a PyInstaller build: the folder containing Mynah.exe.
    - In dev (running run.py or python -m mynah): the project root
      (the parent of src/).
    """
    override = os.environ.get("MYNAH_APP_ROOT")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


APP_ROOT = app_root()
CONFIG_PATH = APP_ROOT / "config.json"
DEFAULT_RECORDINGS_DIR = APP_ROOT / "Recordings"


def log_dir() -> Path:
    """Where the rotating on-disk logs live (issue #19).

    Computed via app_root() per call (not the import-time APP_ROOT
    constant) so the MYNAH_APP_ROOT override keeps working for tests
    and the installer launcher.
    """
    return app_root() / "logs"


def _scrub_and_backup_corrupted_config(
    src: Path, backup: Path, raw_text: Optional[str]
) -> None:
    """Move a malformed config aside, with secrets redacted.

    The `.bad` backup is intended as a diagnostic so the user can see
    what they hand-edited wrong. Keeping raw OAuth tokens / client
    secrets / HF tokens in that file indefinitely (the previous behavior)
    leaks credentials. We try to parse the malformed text, redact the
    known secret fields, write the redacted copy, then delete the
    original. If the text is unparseable as JSON at all (the most likely
    cause of getting here), we drop the file entirely rather than copy
    plaintext bytes that may still contain a recognizable token.
    """
    redacted: Optional[str] = None
    if raw_text is not None:
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict):
                for key in _SECRET_FIELDS:
                    if key in data:
                        data[key] = None
                redacted = json.dumps(data, indent=2)
        except Exception:
            redacted = None

    try:
        if redacted is not None:
            backup.write_text(redacted, encoding="utf-8")
            if os.name == "posix":
                try:
                    os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            try:
                src.unlink()
            except OSError:
                pass
        else:
            try:
                src.unlink()
            except OSError:
                pass
            log.warning(
                "config.json was unparseable as JSON; dropped instead of "
                "preserving plaintext (it may have contained secrets)."
            )
    except OSError as e:
        log.warning("Could not move corrupted config aside: %s", e)


def _drop_legacy_client_secret() -> None:
    """One-time cleanup for the PKCE migration (issue #1).

    Pre-PKCE versions stored the Discord Client Secret in the OS
    credential store. The secret is no longer used anywhere — keeping
    it readable by any same-user process is pure attack surface, so
    delete it on sight. Cheap no-op once gone (get returns None).
    """
    try:
        if secrets_store.get_secret(secrets_store.KEY_DISCORD_CLIENT_SECRET):
            secrets_store.delete_secret(secrets_store.KEY_DISCORD_CLIENT_SECRET)
            log.info(
                "Removed stored Discord Client Secret: no longer needed — "
                "the app now uses PKCE (no secret required)."
            )
    except secrets_store.SecretWriteError as e:
        log.warning(
            "Could not remove the obsolete Discord Client Secret from the "
            "OS credential store (%s); you can delete it manually via "
            "Credential Manager.", e,
        )


def _migrate_legacy_secrets(
    cfg: "Config",
    legacy_hf_token: str,
    legacy_token_obj: Optional["OAuthToken"],
) -> tuple[list[tuple[str, str, object]], bool]:
    """Lift legacy plaintext secrets through Config setters into the OS
    credential store.

    Returns `(migrated_writes, any_write_failure)`:
    - `migrated_writes` is a list of `(key_name, shadow_attr_name,
      original_value)` tuples recording each successful keyring write,
      so a later `cfg.save()` failure can selectively roll back only
      those keys (V2) and restore the corresponding in-memory shadow
      to the pre-migration value (V6).
    - `any_write_failure` is True if any setter raised
      `SecretWriteError`. In that case the value is stashed in the
      `_legacy_*` shadow (so the running session can still read it
      via the property getter) and the caller skips `cfg.save()` so
      the original plaintext config.json remains for next-launch
      retry.

    Extracted from `Config.load` to keep
    `load()` focused on JSON parsing, recovery, and save/rollback
    orchestration.
    """
    migrated: list[tuple[str, str, object]] = []
    any_failure = False

    if legacy_hf_token:
        try:
            cfg.hf_token = legacy_hf_token
            migrated.append((
                secrets_store.KEY_HUGGINGFACE_TOKEN,
                "_legacy_hf_token",
                legacy_hf_token,
            ))
        except secrets_store.SecretWriteError as e:
            log.error("Migration of hf_token failed: %s", e)
            cfg._legacy_hf_token = legacy_hf_token
            any_failure = True
    if legacy_token_obj is not None:
        try:
            cfg.token = legacy_token_obj
            migrated.append((
                secrets_store.KEY_DISCORD_TOKEN,
                "_legacy_token",
                legacy_token_obj,
            ))
        except secrets_store.SecretWriteError as e:
            log.error("Migration of OAuth token failed: %s", e)
            cfg._legacy_token = legacy_token_obj
            any_failure = True

    return migrated, any_failure


@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds

    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < (self.expires_at - 60)


@dataclass
class Config:
    """User-facing configuration.

    Persistence split (issue #14):
      - Non-sensitive fields below are written verbatim to config.json.
      - Secrets (hf_token, OAuth token) are accessed via the `hf_token`
        / `token` properties, which read from the OS credential store
        when available and fall back to the in-memory `_legacy_*`
        shadow fields populated by `Config.load()` for installs
        without a keyring backend.

    Direct attribute access to the secret properties looks identical
    to the pre-#14 API — `cfg.hf_token`, `cfg.token` — so call sites
    need not change. (`discord_client_secret` was removed in the PKCE
    migration, issue #1.)
    """

    discord_client_id: str = ""
    recordings_dir: str = field(default_factory=lambda: str(DEFAULT_RECORDINGS_DIR))
    whisper_model: str = _DEFAULT_WHISPER_MODEL
    audio_source: str = "mixed"  # one of: "mixed" | "mic_only" | "system_only"
    # Empty string = follow the Windows default playback device (the
    # historical behaviour). Set to a PortAudio device "name" string
    # to override — used when the Windows default output isn't the
    # device that's actually carrying Discord audio (common when the
    # user has multiple HDMI monitors or a separate headset with its
    # own dongle). When the configured name no longer matches any
    # currently-enumerated device (machine moved, device unplugged),
    # AudioRecorder.start() falls back to the Windows default with a
    # WARNING so the recording still proceeds.
    loopback_device_name: str = ""
    # Query the GitHub releases API once per launch (and on demand) to
    # surface new versions in the UI. The ONLY phone-home in the app —
    # documented in the README privacy section, off-switchable here and
    # in Settings, and it sends nothing but the HTTP request itself.
    check_updates: bool = True

    # In-memory shadow of the secret values. Populated by `load()` from
    # the OS credential store on every launch (or from legacy config.json
    # on first launch after the #14 upgrade, then migrated to the store).
    # On installs where keyring is unavailable, these are the only home
    # for the secrets — `save()` writes them back to config.json with a
    # loud warning so the operator knows credentials are at rest in
    # cleartext.
    _legacy_hf_token: str = field(default="", repr=False)
    _legacy_token: Optional[OAuthToken] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.audio_source not in _VALID_AUDIO_SOURCES:
            log.warning(
                "Invalid audio_source %r; falling back to 'mixed'", self.audio_source
            )
            self.audio_source = "mixed"
        if not self.whisper_model or not isinstance(self.whisper_model, str):
            self.whisper_model = _DEFAULT_WHISPER_MODEL

    # ---- secret accessors ----------------------------------------------------
    # (discord_client_secret was removed in the PKCE migration, issue #1 —
    # the OAuth flow no longer uses a secret at all.)

    @property
    def hf_token(self) -> str:
        stored = secrets_store.get_secret(secrets_store.KEY_HUGGINGFACE_TOKEN)
        if stored is not None:
            return stored
        return self._legacy_hf_token

    @hf_token.setter
    def hf_token(self, value: str) -> None:
        value = value or ""
        # set_secret raises SecretWriteError if keyring IS available but
        # the backend write failed. Let it propagate — the UI caller
        # surfaces the error and keeps the previous value. We must NOT
        # silently populate the legacy shadow on a write failure: that
        # would persist plaintext to disk on the next save() with only a
        # log warning (the #14 regression). A False return means keyring
        # is genuinely unavailable on this install — the intentional
        # plaintext fallback.
        if secrets_store.set_secret(
            secrets_store.KEY_HUGGINGFACE_TOKEN, value,
        ):
            self._legacy_hf_token = ""
        else:
            self._legacy_hf_token = value

    @property
    def token(self) -> Optional[OAuthToken]:
        stored = secrets_store.get_secret(secrets_store.KEY_DISCORD_TOKEN)
        if stored:
            try:
                data = json.loads(stored)
                return OAuthToken(
                    access_token=str(data.get("access_token") or ""),
                    refresh_token=str(data.get("refresh_token") or ""),
                    expires_at=float(data.get("expires_at") or 0.0),
                )
            except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
                AttributeError,
            ) as e:
                log.warning(
                    "Stored OAuth token is malformed (%s); ignoring.", e,
                )
                return None
        return self._legacy_token

    @token.setter
    def token(self, value: Optional[OAuthToken]) -> None:
        if value is None:
            # Best-effort clear; a SecretWriteError from the backend
            # on delete is logged but not raised here — the caller's
            # intent ("forget this token") still succeeds in memory.
            try:
                secrets_store.delete_secret(secrets_store.KEY_DISCORD_TOKEN)
            except secrets_store.SecretWriteError:
                pass
            self._legacy_token = None
            return
        encoded = json.dumps(asdict(value))
        # See hf_token.setter for the SecretWriteError propagation
        # rationale.
        if secrets_store.set_secret(secrets_store.KEY_DISCORD_TOKEN, encoded):
            self._legacy_token = None
        else:
            self._legacy_token = value

    @classmethod
    def create(
        cls,
        *,
        discord_client_id: str = "",
        recordings_dir: Optional[str] = None,
        whisper_model: str = _DEFAULT_WHISPER_MODEL,
        audio_source: str = "mixed",
        hf_token: str = "",
        token: Optional[OAuthToken] = None,
    ) -> "Config":
        """Build a Config with both non-secret and secret values.

        the dataclass `__init__` only accepts
        non-secret fields because the secret values are exposed via
        property setters that route through the OS credential store —
        `Config(hf_token="...")` raises `TypeError`. Tests
        and scripts that need a fully-populated Config previously had
        to construct first and then assign every secret separately,
        which is asymmetric with `load()` (which accepts a full JSON
        config) and easy to get wrong (missing one assignment leaves
        a secret unset).

        This factory accepts every persisted field as a keyword arg
        and applies the secrets via the property setters in
        consistent order, so callers can build a Config in a single
        call. May raise `secrets_store.SecretWriteError` if a keyring
        write fails — same contract as the property setters.
        """
        cfg = cls(
            discord_client_id=discord_client_id,
            recordings_dir=(
                recordings_dir or str(DEFAULT_RECORDINGS_DIR)
            ),
            whisper_model=whisper_model,
            audio_source=audio_source,
        )
        if hf_token:
            cfg.hf_token = hf_token
        if token is not None:
            cfg.token = token
        return cfg

    @classmethod
    def load(cls) -> "Config":
        # PKCE migration (issue #1): a Client Secret stored by a pre-PKCE
        # version is dead weight in the credential store — remove it
        # unconditionally, including on the fresh-config paths below.
        _drop_legacy_client_secret()

        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg

        text: Optional[str] = None
        try:
            # utf-8-sig transparently strips a UTF-8 BOM if present. Windows
            # PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM by
            # default (no `-Encoding utf8NoBOM` until PS 7), and Notepad
            # sometimes adds one — without this, hand-edited configs crashed
            # json.loads with "Unexpected UTF-8 BOM". Originally 
            text = CONFIG_PATH.read_text(encoding="utf-8-sig")
            raw = json.loads(text)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            # A corrupted config (truncated mid-write, hand-edited badly,
            # encoding glitch) used to crash the GUI before the window
            # appeared. Move the bad file aside, log loudly, and start
            # fresh so the user can at least reach Settings to re-enter
            # their credentials.
            log.error(
                "config.json is unreadable (%s); moving aside to config.json.bad", e
            )
            backup = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".bad")
            _scrub_and_backup_corrupted_config(CONFIG_PATH, backup, text)
            cfg = cls()
            cfg.save()
            return cfg

        if not isinstance(raw, dict):
            log.error("config.json root is not an object; starting fresh.")
            cfg = cls()
            cfg.save()
            return cfg

        # Lift any legacy secrets out of the JSON BEFORE constructing the
        # Config so they never appear as dataclass fields. The HF token
        # and OAuth token are migrated into the OS credential store (or
        # held in the `_legacy_*` shadows as a fallback); a plaintext
        # Client Secret from a pre-PKCE config is simply discarded —
        # the OAuth flow no longer uses one (issue #1).
        if raw.pop("discord_client_secret", None):
            log.info(
                "Dropping discord_client_secret from config.json: the app "
                "now authenticates with PKCE and needs no Client Secret."
            )
        legacy_hf_token = str(raw.pop("hf_token", "") or "")
        legacy_token_raw = raw.pop("token", None)

        # Filter unknown keys so a config written by a newer version (or
        # hand-edited with extra fields) doesn't crash cls(**raw). The
        # `_legacy_*` fields are intentionally private — they should never
        # appear in config.json on disk — so we exclude them from the
        # accepted set via the explicit `_SHADOW_FIELDS` allow-list
        #.
        known = {f.name for f in fields(cls)
                 if f.name not in _SHADOW_FIELDS}
        accepted = {k: v for k, v in raw.items() if k in known}
        dropped = set(raw) - accepted.keys()
        if dropped:
            log.warning("Ignoring unknown config keys: %s", sorted(dropped))

        try:
            cfg = cls(**accepted)
        except TypeError as e:
            log.error("config.json field types invalid (%s); starting fresh.", e)
            cfg = cls()
            cfg.save()
            return cfg

        # Parse the legacy token blob if present so we can hand a real
        # OAuthToken to `cfg.token`'s setter — which routes it through
        # the credential store with the same fallback logic as a fresh
        # user-driven Settings save.
        legacy_token_obj: Optional[OAuthToken] = None
        if isinstance(legacy_token_raw, dict):
            try:
                legacy_token_obj = OAuthToken(
                    access_token=str(legacy_token_raw.get("access_token") or ""),
                    refresh_token=str(legacy_token_raw.get("refresh_token") or ""),
                    expires_at=float(legacy_token_raw.get("expires_at") or 0.0),
                )
            except (TypeError, ValueError) as e:
                log.warning("Cached OAuth token is malformed (%s); ignoring.", e)

        # the per-secret migration loop is
        # extracted into the module-level `_migrate_legacy_secrets`
        # helper so this method does not also own the migration's
        # SecretWriteError handling and `migrated_writes` bookkeeping.
        # `Config.load` keeps the save-and-rollback orchestration
        # because that path needs the rollback to live next to the
        # `cfg.save()` call.
        migrated_writes, any_write_failure = _migrate_legacy_secrets(
            cfg,
            legacy_hf_token,
            legacy_token_obj,
        )
        any_migrated = bool(migrated_writes)

        # Drop the post-write `is_available()` re-probe 
        # V11): if any setter returned without raising, the backend
        # accepted at least one write, so the migration's clean-rewrite
        # save() must run. A second availability check here is a TOCTOU
        # window — a transient false from `is_available()` would skip
        # the save, leaving plaintext on disk AND copies in keyring.
        #
        # distinguish writes that actually landed
        # in keyring from writes that fell back to the in-memory shadow
        # because keyring was unavailable. After a successful property
        # setter, the shadow attribute is empty IFF the keyring write
        # succeeded (`set_secret` returned True). A non-empty shadow
        # means `set_secret` returned False (no library / null backend)
        # and the value was kept in plaintext. This lets the log
        # accurately report what happened and lets the rollback path
        # delete only entries that are actually in keyring.
        keys_in_keyring = [
            (key, shadow_attr, original_value)
            for key, shadow_attr, original_value in migrated_writes
            if not getattr(cfg, shadow_attr)
        ]
        any_in_keyring = bool(keys_in_keyring)

        if any_migrated and not any_write_failure:
            if any_in_keyring:
                log.info(
                    "Migrated legacy secrets from config.json to OS "
                    "credential store. Rewriting config.json without "
                    "secrets."
                )
            else:
                log.warning(
                    "Legacy secrets remain in config.json: OS credential "
                    "store is not available, falling back to plaintext "
                    "storage. Install `keyring` for encrypted-at-rest "
                    "credential storage."
                )
            try:
                cfg.save()
            except OSError as e:
                # Roll back keyring writes (entries that actually landed
                # in the credential store) so we don't leave secrets in
                # BOTH stores. Without this, an antivirus lock or disk
                # hiccup mid-rename would persist plaintext in
                # config.json (atomic save left the original intact)
                # AND copies in keyring — doubling the attack surface.
                # After rollback the next launch sees a clean retry:
                # original config.json still has plaintext, keyring is
                # empty for these keys, migration block runs fresh.
                #
                # Restore each `_legacy_*` shadow to its pre-migration
                # value so the CURRENT session can still read the
                # credential via the property getters. Only the keys
                # this migration actually wrote to keyring are rolled
                # back — pre-existing keyring entries from earlier
                # successful migrations are preserved.
                log.error(
                    "Migration save() failed (%s): rolling back keyring "
                    "writes to avoid duplicated secrets. Re-launch once "
                    "the disk error is resolved to re-run migration.",
                    e,
                )
                for key, shadow_attr, original_value in keys_in_keyring:
                    try:
                        secrets_store.delete_secret(key)
                    except secrets_store.SecretWriteError as rollback_err:
                        log.warning(
                            "Rollback of %s failed (%s); manual cleanup "
                            "via OS credential manager may be required.",
                            key,
                            rollback_err,
                        )
                    setattr(cfg, shadow_attr, original_value)
        elif any_migrated and any_write_failure:
            # partial migration (some secrets
            # successfully written to keyring, another raised
            # SecretWriteError) used to log-and-return — leaving the
            # successfully-written secret in BOTH keyring AND plaintext
            # config.json. That is the exact dual-store regression V2
            # and V6 were closing. Roll back the successful keyring
            # writes here so the next launch retries from a clean
            # state: original config.json untouched, keyring cleared
            # for the migrated keys, in-memory shadows restored.
            log.warning(
                "Partial migration: rolling back %d successful keyring "
                "write(s) to avoid dual-store state with the secret(s) "
                "that failed. Original config.json is preserved for "
                "next-launch retry. Open Settings → Save once the "
                "credential backend recovers.",
                len(keys_in_keyring),
            )
            for key, shadow_attr, original_value in keys_in_keyring:
                try:
                    secrets_store.delete_secret(key)
                except secrets_store.SecretWriteError as rollback_err:
                    log.warning(
                        "Rollback of %s failed (%s); manual cleanup "
                        "via OS credential manager may be required.",
                        key,
                        rollback_err,
                    )
                setattr(cfg, shadow_attr, original_value)
        elif any_write_failure:
            log.warning(
                "Migration of legacy secrets failed; they remain in "
                "config.json for now. Open Settings → Save to retry."
            )

        return cfg

    def _persistable_dict(self) -> dict:
        """The subset of state that goes into config.json on disk.

        Derived dynamically from the dataclass fields, excluding the
        private `_legacy_*` secret shadows. Those are written ONLY
        when non-empty — which happens exclusively on installs where
        the keyring write failed and the secret must round-trip via
        the JSON file. On installs where keyring succeeds, the
        shadow fields stay empty and no secret key appears in the
        file. Building the field list at runtime instead of hardcoding
        names prevents the silent-drop-on-future-field bug where a
        new dataclass field would be accepted by load() but lost by
        save().
        """
        d: dict = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in _SHADOW_FIELDS
        }
        if self._legacy_hf_token:
            d["hf_token"] = self._legacy_hf_token
        # only persist a legacy token that has a
        # meaningful access_token. The setter accepts any OAuthToken
        # (even one with empty strings — invalid in practice but
        # constructible), so without this guard an empty token on a
        # keyring-unavailable install would land in config.json as
        # `{"access_token": "", "refresh_token": "", "expires_at": 0}`,
        # polluting the audit trail with a value that fails
        # `OAuthToken.is_valid()` anyway.
        if (
            self._legacy_token is not None
            and self._legacy_token.access_token
        ):
            d["token"] = asdict(self._legacy_token)
        return d

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = self._persistable_dict()
        payload = json.dumps(data, indent=2).encode("utf-8")

        # Write atomically via tmp + os.replace so a crash mid-write can't
        # produce a truncated config.json (which then fails to parse on the
        # next launch and used to silently lose the user's credentials).
        #
        # On POSIX we create the tmp file with O_EXCL|0600 so secrets are
        # never readable by another local user — not even for the brief
        # window between `os.replace()` and a follow-up `os.chmod()`,
        # which is what the previous implementation left exposed. O_EXCL
        # also refuses to follow a symlink at the tmp path, which closes
        # a classic local-symlink redirection.
        tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
        try:
            if os.name == "posix":
                # Drop any leftover tmp from a prior crashed save.
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                fd = os.open(
                    str(tmp),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
            else:
                with tmp.open("wb") as f:
                    f.write(payload)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
            os.replace(tmp, CONFIG_PATH)
        except OSError:
            # Best-effort cleanup; re-raise so the caller surfaces the error.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        # On POSIX, os.replace() preserves the source mode (0600 from
        # O_EXCL above) on the destination inode, so no follow-up chmod
        # is required. On Windows, ACLs are inherited from the parent
        # directory and we don't lock the install dir down behind the
        # user's back.

    @property
    def recordings_path(self) -> Path:
        # NOTE: this property is read-only — the directory is created on
        # demand by the recorder in RecordingSession.start(), not here.
        # Having mkdir() inside a property getter (the previous behaviour)
        # turned every read into a write and made set-and-validate flows
        # surprising.
        return Path(self.recordings_dir)

    def ensure_recordings_dir(self) -> Path:
        """Create the recordings directory if missing and return its Path.

        Call this from the small handful of places that actually need the
        directory to exist (RecordingSession output_dir, recordings folder
        scan, File→Open recordings folder).
        """
        p = self.recordings_path
        p.mkdir(parents=True, exist_ok=True)
        return p
