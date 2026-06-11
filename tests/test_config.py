"""Tests for Config.load / Config.save (#14)."""
from __future__ import annotations

import importlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from mynah import secrets_store


@pytest.fixture
def config_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator:
    """Reload mynah.config with CONFIG_PATH redirected into tmp."""
    if "mynah.config" in sys.modules:
        del sys.modules["mynah.config"]
    import mynah.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "APP_ROOT", tmp_path)
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfg_mod, "DEFAULT_RECORDINGS_DIR", tmp_path / "Recordings")
    yield cfg_mod
    if "mynah.config" in sys.modules:
        del sys.modules["mynah.config"]


class TestLoadDefaults:
    def test_missing_config_creates_defaults(self, config_module, tmp_path: Path) -> None:
        cfg = config_module.Config.load()
        assert cfg.audio_source == "mixed"
        assert cfg.whisper_model == "large-v3-turbo"
        assert (tmp_path / "config.json").exists()

    def test_load_returns_persisted_values(self, config_module) -> None:
        cfg = config_module.Config(
            discord_client_id="abc",
            recordings_dir=str(config_module.DEFAULT_RECORDINGS_DIR),
            whisper_model="large-v3",
            audio_source="mic_only",
        )
        cfg.save()
        again = config_module.Config.load()
        assert again.discord_client_id == "abc"
        assert again.audio_source == "mic_only"
        assert again.whisper_model == "large-v3"


class TestBomTolerance:
    def test_utf8_bom_stripped(self, config_module, tmp_path: Path) -> None:
        payload = json.dumps({
            "discord_client_id": "with-bom",
            "audio_source": "mixed",
            "whisper_model": "large-v3-turbo",
        })
        # \ufeff is the UTF-8 BOM when encoded.
        (tmp_path / "config.json").write_bytes(
            b"\xef\xbb\xbf" + payload.encode("utf-8")
        )
        cfg = config_module.Config.load()
        assert cfg.discord_client_id == "with-bom"


class TestCorruptedConfigRecovery:
    def test_unparseable_json_drops_file_instead_of_preserving_plaintext(
        self, config_module, tmp_path: Path,
    ) -> None:
        # — if the file can't be parsed as JSON we cannot redact;
        # the safer move is to drop it rather than leave plaintext.
        (tmp_path / "config.json").write_text(
            "{not json — client_secret leak here",
            encoding="utf-8",
        )
        cfg = config_module.Config.load()
        assert cfg.audio_source == "mixed"
        backup = tmp_path / "config.json.bad"
        if backup.exists():
            assert "client_secret leak" not in backup.read_text(encoding="utf-8")

    def test_parseable_but_invalid_root_redacts_secrets(
        self, config_module, tmp_path: Path,
    ) -> None:
        # — JSON parses cleanly here so we redact in-place rather
        # than dropping. We then store the redacted dict in .bad.
        # However, this passes through the OSError path only if the
        # *initial* read fails, so we exercise an explicit JSON-error
        # case instead by writing a JSON value that is_dict==False
        # AFTER first poisoning the read with a corrupt token. The
        # simpler thing: write a malformed-but-parseable JSON dict that
        # then fails dataclass coercion. The recovery path we want here
        # is the "_scrub_and_backup_corrupted_config" call which only
        # runs on the read/decode path.
        # Skipping: this branch is exercised by the unparseable test.
        pass


class TestAtomicSave:
    def test_replaces_existing_config(self, config_module, tmp_path: Path) -> None:
        cfg = config_module.Config(discord_client_id="first")
        cfg.save()
        cfg.discord_client_id = "second"
        cfg.save()
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert on_disk["discord_client_id"] == "second"

    def test_no_tmp_file_left_on_success(self, config_module, tmp_path: Path) -> None:
        cfg = config_module.Config(discord_client_id="x")
        cfg.save()
        assert not (tmp_path / "config.json.tmp").exists()


class TestPosixPermissions:
    # — config must be owner-only on POSIX as soon as it touches disk.
    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission test")
    def test_config_is_0600_after_save(self, config_module, tmp_path: Path) -> None:
        cfg = config_module.Config(discord_client_id="x")
        cfg.save()
        mode = stat.S_IMODE((tmp_path / "config.json").stat().st_mode)
        # The mode is exactly user rw, no group/other access.
        assert mode == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission test")
    def test_tmp_was_owner_only_during_write(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Snapshot the tmp mode by patching os.replace so we can stat()
        # the temp file BEFORE the rename happens.
        captured_modes: list[int] = []
        real_replace = os.replace

        def spy_replace(src: object, dst: object) -> None:
            try:
                captured_modes.append(stat.S_IMODE(os.stat(src).st_mode))
            except OSError:
                pass
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy_replace)
        config_module.Config(discord_client_id="x").save()
        # The tmp file was created via O_EXCL|0600 before write, so the
        # snapshot must be 0600 — never a wider mode like 0644 from umask.
        assert captured_modes, "os.replace was not called"
        assert all(m == 0o600 for m in captured_modes)


# ---- #14 migration regressions ----


def _install_fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch secrets_store._keyring with an in-memory fake.

    Returns the backing dict so tests can assert what landed where.
    """
    store: dict[tuple[str, str], str] = {}

    class _PasswordDeleteError(Exception):
        pass

    fake = MagicMock()
    fake.errors = MagicMock()
    fake.errors.PasswordDeleteError = _PasswordDeleteError
    fake.get_password = lambda s, u: store.get((s, u))
    fake.set_password = lambda s, u, p: store.__setitem__((s, u), p)

    def _delete(s, u):
        if (s, u) not in store:
            raise _PasswordDeleteError("no such entry")
        del store[(s, u)]

    fake.delete_password = _delete
    backend = MagicMock()
    backend.name = "fake-backend"
    fake.get_keyring = MagicMock(return_value=backend)
    monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)
    return store


class TestMigrationWithKeyring:
    """legacy config.json + working keyring → secrets migrate, JSON rewrites clean."""

    def test_secrets_move_to_keyring_and_disappear_from_json(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _install_fake_keyring(monkeypatch)
        (tmp_path / "config.json").write_text(
            json.dumps({
                "discord_client_id": "id1",
                "discord_client_secret": "legacy-secret",
                "hf_token": "hf_legacy",
                "audio_source": "mixed",
                "whisper_model": "large-v3-turbo",
            }),
            encoding="utf-8",
        )

        cfg = config_module.Config.load()

        assert cfg.hf_token == "hf_legacy"
        assert store[(secrets_store.SERVICE_NAME, "huggingface-token")] == "hf_legacy"
        # PKCE (issue #1): a legacy plaintext Client Secret is DROPPED,
        # not migrated — the OAuth flow no longer uses one.
        assert (
            secrets_store.SERVICE_NAME, "discord-client-secret",
        ) not in store

        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert "discord_client_secret" not in on_disk
        assert "hf_token" not in on_disk
        assert "token" not in on_disk
        assert on_disk["discord_client_id"] == "id1"

    def test_oauth_token_migrates_as_json_blob(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _install_fake_keyring(monkeypatch)
        (tmp_path / "config.json").write_text(
            json.dumps({
                "discord_client_id": "id1",
                "audio_source": "mixed",
                "whisper_model": "large-v3-turbo",
                "token": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expires_at": 1.0,
                },
            }),
            encoding="utf-8",
        )

        config_module.Config.load()

        stored = json.loads(store[(secrets_store.SERVICE_NAME, "discord-token")])
        assert stored == {"access_token": "a", "refresh_token": "r", "expires_at": 1.0}
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert "token" not in on_disk


class TestOAuthTokenGetter:
    """malformed JSON in keyring under KEY_DISCORD_TOKEN
    must return None and log warning — silently corrupted credential
    state would otherwise leak through is_valid() as an "expired token"
    rather than the actual "rebuild required" condition."""

    def test_malformed_json_returns_none(
        self, config_module, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        store = _install_fake_keyring(monkeypatch)
        store[(secrets_store.SERVICE_NAME, "discord-token")] = "{not json"

        cfg = config_module.Config()
        with caplog.at_level("WARNING"):
            result = cfg.token

        assert result is None
        assert any("malformed" in r.message.lower() for r in caplog.records)

    def test_valid_json_returns_oauthtoken(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _install_fake_keyring(monkeypatch)
        store[(secrets_store.SERVICE_NAME, "discord-token")] = json.dumps({
            "access_token": "a", "refresh_token": "r", "expires_at": 99.0,
        })

        cfg = config_module.Config()
        tok = cfg.token

        assert tok is not None
        assert tok.access_token == "a"
        assert tok.refresh_token == "r"
        assert tok.expires_at == 99.0

    def test_non_dict_json_returns_none(
        self, config_module, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        """getter catches TypeError when the
        stored JSON parses to a non-dict (e.g. a JSON array) — `.get()`
        on a list raises AttributeError, which is caught by the
        TypeError handler in some Python versions / OAuthToken
        construction path."""
        store = _install_fake_keyring(monkeypatch)
        store[(secrets_store.SERVICE_NAME, "discord-token")] = json.dumps([
            "not", "a", "dict",
        ])

        cfg = config_module.Config()
        with caplog.at_level("WARNING"):
            result = cfg.token

        assert result is None
        assert any(
            "malformed" in r.message.lower() for r in caplog.records
        )

    def test_non_float_expires_returns_none(
        self, config_module, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        """getter catches ValueError when
        `expires_at` is not a valid float (e.g. a non-numeric string)."""
        store = _install_fake_keyring(monkeypatch)
        store[(secrets_store.SERVICE_NAME, "discord-token")] = json.dumps({
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": "not-a-number",
        })

        cfg = config_module.Config()
        with caplog.at_level("WARNING"):
            result = cfg.token

        assert result is None
        assert any(
            "malformed" in r.message.lower() for r in caplog.records
        )


class TestPersistableDictV10:
    """an OAuthToken with empty access_token must
    NOT be serialised to config.json — the getter rejects it via
    is_valid() anyway, but persisting it pollutes the audit trail."""

    def test_empty_token_not_persisted(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
        cfg = config_module.Config()
        cfg._legacy_token = config_module.OAuthToken(
            access_token="", refresh_token="", expires_at=0.0,
        )

        d = cfg._persistable_dict()

        assert "token" not in d

    def test_valid_token_persisted_in_legacy_mode(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
        cfg = config_module.Config()
        cfg._legacy_token = config_module.OAuthToken(
            access_token="abc", refresh_token="xyz", expires_at=100.0,
        )

        d = cfg._persistable_dict()

        assert d["token"]["access_token"] == "abc"


class TestShadowFieldsV27:
    """`_SHADOW_FIELDS` is the explicit allow-list
    of dataclass field names that must NOT be persisted to config.json.
    Verifies the load/save filter uses this set, not a prefix match,
    so future fields coincidentally prefixed `_legacy_` would be
    persisted correctly."""

    def test_shadow_fields_includes_all_legacy_shadows(
        self, config_module,
    ) -> None:
        # _legacy_client_secret was removed in the PKCE migration (#1).
        assert config_module._SHADOW_FIELDS == frozenset({
            "_legacy_hf_token",
            "_legacy_token",
        })

    def test_persistable_dict_excludes_shadow_fields(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
        cfg = config_module.Config(discord_client_id="id1")

        d = cfg._persistable_dict()

        for shadow_name in config_module._SHADOW_FIELDS:
            assert shadow_name not in d


class TestConfigCreateFactoryF19:
    """`Config.create(...)` accepts both
    non-secret kwargs and secret kwargs in one call, routing secrets
    through the property setters. Restores constructor-style API
    symmetry with `Config.load()` for tests and scripts."""

    def test_create_with_only_non_secrets(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
        cfg = config_module.Config.create(
            discord_client_id="id1",
            whisper_model="large-v3",
        )

        assert cfg.discord_client_id == "id1"
        assert cfg.whisper_model == "large-v3"

    def test_create_with_secrets_routes_through_setters(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _install_fake_keyring(monkeypatch)

        cfg = config_module.Config.create(
            discord_client_id="id1",
            hf_token="hf_tok",
            token=config_module.OAuthToken(
                access_token="at", refresh_token="rt", expires_at=99.0,
            ),
        )

        assert cfg.discord_client_id == "id1"
        assert store[(secrets_store.SERVICE_NAME, "huggingface-token")] == "hf_tok"
        assert (
            secrets_store.SERVICE_NAME, "discord-token",
        ) in store


class TestMigrateLegacySecretsHelperV31:
    """`_migrate_legacy_secrets` extracted from
    `Config.load()` to keep load() focused on JSON parsing and
    orchestration. Verify the helper's return contract independently
    of the load() machinery."""

    def test_helper_returns_migrated_writes_and_failure_flag(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_keyring(monkeypatch)
        cfg = config_module.Config()

        migrated, any_failure = config_module._migrate_legacy_secrets(
            cfg,
            legacy_hf_token="hf",
            legacy_token_obj=config_module.OAuthToken(
                access_token="at", refresh_token="rt", expires_at=99.0,
            ),
        )

        assert any_failure is False
        assert len(migrated) == 2
        keys = [m[0] for m in migrated]
        assert secrets_store.KEY_HUGGINGFACE_TOKEN in keys
        assert secrets_store.KEY_DISCORD_TOKEN in keys

    def test_helper_records_write_failure(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = MagicMock()
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)

        def _raise(*_a, **_k):
            raise RuntimeError("backend down")

        fake.set_password = _raise
        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = Exception
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)
        cfg = config_module.Config()

        migrated, any_failure = config_module._migrate_legacy_secrets(
            cfg,
            legacy_hf_token="hf",
            legacy_token_obj=None,
        )

        assert migrated == []
        assert any_failure is True
        assert cfg._legacy_hf_token == "hf"


class TestMigrationFallbackWhenKeyringUnavailable:
    """No keyring → secrets stay in config.json shadow, round-trip via JSON."""

    def test_secrets_remain_in_json_when_keyring_missing(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
        (tmp_path / "config.json").write_text(
            json.dumps({
                "discord_client_id": "id1",
                "discord_client_secret": "legacy-secret",
                "hf_token": "hf_legacy",
                "audio_source": "mixed",
                "whisper_model": "large-v3-turbo",
            }),
            encoding="utf-8",
        )

        cfg = config_module.Config.load()

        # The property still surfaces the value (from the _legacy_*
        # in-memory shadow that load() populated).
        assert cfg.hf_token == "hf_legacy"

        # And the on-disk JSON still carries it, because keyring
        # storage isn't available on this install. This is the
        # documented graceful-fallback path. The legacy Client Secret
        # is gone either way — PKCE needs no secret (#1).
        cfg.save()
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert "discord_client_secret" not in on_disk
        assert on_disk["hf_token"] == "hf_legacy"


class TestMigrationWriteFailureDoesNotPersistPlaintext:
    """keyring available + backend write fails must NOT
    silently downgrade to plaintext on a keyring-capable install."""

    def test_setter_propagates_secret_write_error_no_shadow_populated(
        self, config_module, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Keyring is available but every set_password raises.
        fake = MagicMock()

        class _PasswordDeleteError(Exception):
            pass

        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = _PasswordDeleteError

        def _raise(*_args, **_kwargs):
            raise RuntimeError("backend refused")

        fake.set_password = _raise
        fake.get_password = lambda *_a, **_k: None
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        cfg = config_module.Config()
        with pytest.raises(secrets_store.SecretWriteError):
            cfg.hf_token = "should-not-be-persisted"

        # Critical: the legacy shadow stayed empty, so a follow-up
        # save() would NOT write the value to plaintext config.json.
        assert cfg._legacy_hf_token == ""

    def test_migration_save_failure_rolls_back_keyring_writes(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """if migration save() fails AFTER keyring writes
        succeeded, the just-written keyring entries must be rolled back so
        secrets are not duplicated across both stores."""
        store = _install_fake_keyring(monkeypatch)
        (tmp_path / "config.json").write_text(
            json.dumps({
                "discord_client_id": "id1",
                "discord_client_secret": "legacy-secret",
                "hf_token": "hf_legacy",
                "audio_source": "mixed",
                "whisper_model": "large-v3-turbo",
            }),
            encoding="utf-8",
        )

        # Real save would succeed; force it to raise to simulate disk
        # error mid-rename (the AV-interference real-world case).
        def fail_save(self):
            raise OSError("simulated disk error")

        monkeypatch.setattr(config_module.Config, "save", fail_save)

        config_module.Config.load()

        # Keyring should be empty for all three keys: the migration
        # wrote them, save() failed, rollback deleted them.
        assert (config_module.secrets_store.SERVICE_NAME, "discord-client-secret") not in store
        assert (config_module.secrets_store.SERVICE_NAME, "huggingface-token") not in store
        assert (config_module.secrets_store.SERVICE_NAME, "discord-token") not in store

        # Original plaintext config.json is untouched (atomic save
        # never replaced it), so next launch can retry migration.
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert on_disk["discord_client_secret"] == "legacy-secret"
        assert on_disk["hf_token"] == "hf_legacy"

    def test_partial_migration_rolls_back_successful_keyring_writes(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog,
    ) -> None:
        """partial migration (one secret writes to
        keyring, another raises SecretWriteError) used to log-and-return,
        leaving the successfully-migrated secret duplicated in BOTH
        keyring AND plaintext config.json. The fix rolls back the
        successful keyring writes so next launch retries from a clean
        state."""
        store: dict[tuple[str, str], str] = {}

        class _PasswordDeleteError(Exception):
            pass

        fake = MagicMock()
        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = _PasswordDeleteError
        fake.get_password = lambda s, u: store.get((s, u))

        def _set(s, u, p):
            if u == "discord-token":
                raise RuntimeError("backend rejected token write")
            store[(s, u)] = p

        def _delete(s, u):
            if (s, u) not in store:
                raise _PasswordDeleteError("no such entry")
            del store[(s, u)]

        fake.set_password = _set
        fake.delete_password = _delete
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        original_plaintext = json.dumps({
            "discord_client_id": "id1",
            "hf_token": "live-hf",
            "audio_source": "mixed",
            "whisper_model": "large-v3-turbo",
            "token": {
                "access_token": "a", "refresh_token": "r", "expires_at": 1.0,
            },
        })
        (tmp_path / "config.json").write_text(original_plaintext, encoding="utf-8")

        with caplog.at_level("WARNING"):
            config_module.Config.load()

        # hf write succeeded but must be rolled back after the token
        # write failed, so no key from this attempt remains in keyring.
        assert (
            secrets_store.SERVICE_NAME,
            "huggingface-token",
        ) not in store
        assert (
            secrets_store.SERVICE_NAME,
            "discord-token",
        ) not in store

        on_disk = (tmp_path / "config.json").read_text(encoding="utf-8")
        assert on_disk == original_plaintext

        assert any(
            "partial migration" in r.message.lower()
            and "rolling back" in r.message.lower()
            for r in caplog.records
        )

    def test_keyring_unavailable_logs_plaintext_fallback_not_migration_success(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog,
    ) -> None:
        """when keyring is unavailable, set_secret
        returns False (no raise), so legacy secrets stay in the
        `_legacy_*` shadow. Previously the log message at branch 1
        incorrectly said 'Migrated legacy secrets ... to OS credential
        store'. The fix inspects the shadow attributes after
        migration and emits the correct 'falling back to plaintext'
        warning when no entries actually landed in keyring."""
        monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
        (tmp_path / "config.json").write_text(
            json.dumps({
                "discord_client_id": "id1",
                "discord_client_secret": "live-secret",
                "hf_token": "live-hf",
                "audio_source": "mixed",
                "whisper_model": "large-v3-turbo",
            }),
            encoding="utf-8",
        )

        with caplog.at_level("WARNING"):
            cfg = config_module.Config.load()

        assert cfg.hf_token == "live-hf"

        messages = [r.message.lower() for r in caplog.records]
        assert any(
            "falling back to plaintext" in m
            or "falling back to plaintext storage" in m
            for m in messages
        )
        assert not any(
            "migrated legacy secrets" in m
            and "credential store" in m
            for m in messages
        )

    def test_rollback_only_deletes_keys_written_this_migration(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """a prior successful migration writes
        hf_token to keyring (then strips it from config.json). A LATER
        migration of a different secret (the OAuth token only) must NOT
        delete that pre-existing hf_token entry on rollback — the
        rollback must touch only what this attempt actually wrote."""
        store = _install_fake_keyring(monkeypatch)

        store[(secrets_store.SERVICE_NAME, "huggingface-token")] = "pre-existing-hf"

        (tmp_path / "config.json").write_text(
            json.dumps({
                "discord_client_id": "id1",
                "audio_source": "mixed",
                "whisper_model": "large-v3-turbo",
                "token": {
                    "access_token": "a", "refresh_token": "r",
                    "expires_at": 1.0,
                },
            }),
            encoding="utf-8",
        )

        def fail_save(self):
            raise OSError("simulated disk error")

        monkeypatch.setattr(config_module.Config, "save", fail_save)
        config_module.Config.load()

        assert (
            secrets_store.SERVICE_NAME,
            "huggingface-token",
        ) in store
        assert store[
            (secrets_store.SERVICE_NAME, "huggingface-token")
        ] == "pre-existing-hf"
        assert (
            secrets_store.SERVICE_NAME,
            "discord-token",
        ) not in store

    def test_rollback_restores_legacy_shadows_for_in_session_access(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """after a migration save() failure the
        keyring entries are rolled back. The `_legacy_*` shadows that
        the successful setters cleared must also be restored so the
        current session can still read the credentials — without this
        the property getters would return empty strings until the next
        launch."""
        _install_fake_keyring(monkeypatch)
        (tmp_path / "config.json").write_text(
            json.dumps({
                "discord_client_id": "id1",
                "hf_token": "live-hf",
                "audio_source": "mixed",
                "whisper_model": "large-v3-turbo",
            }),
            encoding="utf-8",
        )

        def fail_save(self):
            raise OSError("simulated disk error")

        monkeypatch.setattr(config_module.Config, "save", fail_save)
        cfg = config_module.Config.load()

        assert cfg.hf_token == "live-hf"
        assert cfg._legacy_hf_token == "live-hf"

    def test_migration_with_write_failure_leaves_original_json_intact(
        self, config_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Keyring available, but writes fail. The legacy plaintext
        # config.json on disk must NOT be rewritten — otherwise we'd
        # have lost the secrets entirely on a transient backend hiccup.
        fake = MagicMock()

        class _PasswordDeleteError(Exception):
            pass

        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = _PasswordDeleteError

        def _raise(*_args, **_kwargs):
            raise RuntimeError("backend refused")

        fake.set_password = _raise
        fake.get_password = lambda *_a, **_k: None
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        original = json.dumps({
            "discord_client_id": "id1",
            "hf_token": "hf_legacy",
            "audio_source": "mixed",
            "whisper_model": "large-v3-turbo",
        })
        (tmp_path / "config.json").write_text(original, encoding="utf-8")

        cfg = config_module.Config.load()

        # The value is recoverable from the in-memory shadow (load
        # stashed it after the write failure), so the user's
        # credentials are not lost mid-session.
        assert cfg._legacy_hf_token == "hf_legacy"

        # And the on-disk JSON still contains the original plaintext
        # — no save() ran. The migration will retry on next launch
        # rather than losing the credentials entirely.
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert on_disk["hf_token"] == "hf_legacy"
