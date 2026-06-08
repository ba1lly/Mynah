"""Tests for mynah.secrets_store (issue #14)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mynah import secrets_store


@pytest.fixture
def fake_keyring(monkeypatch):
    """A fake `keyring` module backed by an in-memory dict."""
    store: dict[tuple[str, str], str] = {}

    fake = MagicMock()
    fake.errors = MagicMock()

    class _PasswordDeleteError(Exception):
        pass

    fake.errors.PasswordDeleteError = _PasswordDeleteError

    def _get(service, username):
        return store.get((service, username))

    def _set(service, username, password):
        store[(service, username)] = password

    def _delete(service, username):
        if (service, username) not in store:
            raise _PasswordDeleteError("no such entry")
        del store[(service, username)]

    fake.get_password = _get
    fake.set_password = _set
    fake.delete_password = _delete

    backend = MagicMock()
    backend.name = "fake-backend"
    fake.get_keyring = MagicMock(return_value=backend)

    monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)
    return store


@pytest.fixture
def no_keyring(monkeypatch):
    """Simulate `keyring` import failure / no backend."""
    monkeypatch.setattr(secrets_store, "_keyring", lambda: None)


class TestGetSecret:
    def test_returns_stored_value(self, fake_keyring):
        fake_keyring[(secrets_store.SERVICE_NAME, "discord-client-secret")] = "abc"
        assert secrets_store.get_secret("discord-client-secret") == "abc"

    def test_returns_none_when_absent(self, fake_keyring):
        assert secrets_store.get_secret("discord-client-secret") is None

    def test_returns_none_when_keyring_unavailable(self, no_keyring):
        assert secrets_store.get_secret("discord-client-secret") is None


class TestSetSecret:
    def test_writes_value(self, fake_keyring):
        assert secrets_store.set_secret("discord-client-secret", "xyz") is True
        assert fake_keyring[(secrets_store.SERVICE_NAME, "discord-client-secret")] == "xyz"

    def test_empty_value_deletes(self, fake_keyring):
        fake_keyring[(secrets_store.SERVICE_NAME, "discord-client-secret")] = "abc"
        assert secrets_store.set_secret("discord-client-secret", "") is True
        assert (secrets_store.SERVICE_NAME, "discord-client-secret") not in fake_keyring

    def test_none_value_deletes(self, fake_keyring):
        fake_keyring[(secrets_store.SERVICE_NAME, "discord-client-secret")] = "abc"
        assert secrets_store.set_secret("discord-client-secret", None) is True
        assert (secrets_store.SERVICE_NAME, "discord-client-secret") not in fake_keyring

    def test_delete_when_absent_is_noop(self, fake_keyring):
        # Per-spec: post-condition is "no value stored". If nothing was
        # stored, the post-condition is already met — return True.
        assert secrets_store.set_secret("discord-client-secret", None) is True

    def test_returns_false_when_keyring_unavailable(self, no_keyring):
        assert secrets_store.set_secret("discord-client-secret", "abc") is False


class TestDeleteSecret:
    def test_delete_existing(self, fake_keyring):
        fake_keyring[(secrets_store.SERVICE_NAME, "hf-token")] = "tok"
        assert secrets_store.delete_secret("hf-token") is True
        assert (secrets_store.SERVICE_NAME, "hf-token") not in fake_keyring


class TestClearAll:
    def test_removes_every_known_key(self, fake_keyring):
        for key in secrets_store.ALL_KEYS:
            fake_keyring[(secrets_store.SERVICE_NAME, key)] = "x"
        secrets_store.clear_all()
        for key in secrets_store.ALL_KEYS:
            assert (secrets_store.SERVICE_NAME, key) not in fake_keyring

    def test_returns_cleared_and_failed_sets(self, fake_keyring):
        """signature changed from None to
        tuple[set, set] so callers can detect partial failures."""
        for key in secrets_store.ALL_KEYS:
            fake_keyring[(secrets_store.SERVICE_NAME, key)] = "x"

        cleared, failed = secrets_store.clear_all()

        assert cleared == set(secrets_store.ALL_KEYS)
        assert failed == set()

    def test_partial_failure_reported_in_failed_set(self, monkeypatch):
        """a stuck key (SecretWriteError on
        delete) must NOT block clearing the rest AND must appear in
        the `failed` set so the UI can surface manual-cleanup
        guidance instead of falsely reporting success."""
        store: dict[tuple[str, str], str] = {}
        for key in secrets_store.ALL_KEYS:
            store[(secrets_store.SERVICE_NAME, key)] = "x"

        class _PasswordDeleteError(Exception):
            pass

        fake = MagicMock()
        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = _PasswordDeleteError
        fake.get_password = lambda s, u: store.get((s, u))
        fake.set_password = lambda s, u, p: store.__setitem__((s, u), p)

        stuck_key = secrets_store.KEY_HUGGINGFACE_TOKEN

        def _delete(s, u):
            if u == stuck_key:
                raise RuntimeError("backend stuck on this key")
            if (s, u) not in store:
                raise _PasswordDeleteError("no such entry")
            del store[(s, u)]

        fake.delete_password = _delete
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        cleared, failed = secrets_store.clear_all()

        assert stuck_key in failed
        assert secrets_store.KEY_DISCORD_CLIENT_SECRET in cleared
        assert secrets_store.KEY_DISCORD_TOKEN in cleared


class TestIsAvailable:
    def test_true_when_real_backend(self, fake_keyring):
        assert secrets_store.is_available() is True

    def test_false_when_null_backend(self, monkeypatch):
        fake = MagicMock()
        backend = MagicMock()
        backend.name = "null"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)
        assert secrets_store.is_available() is False

    def test_false_when_keyring_unavailable(self, no_keyring):
        assert secrets_store.is_available() is False

    def test_false_when_get_keyring_raises(self, monkeypatch):
        """a backend that raises during
        get_keyring() (e.g. D-Bus down on Linux) must return False,
        not crash. The except clause at secrets_store.py:213-214
        had no test coverage."""
        fake = MagicMock()

        def _raise():
            raise RuntimeError("D-Bus unavailable")

        fake.get_keyring = _raise
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)
        assert secrets_store.is_available() is False


class TestBackendWriteFailure:
    """keyring available + backend write fails must
    raise SecretWriteError, not silently return False. A False return
    on a keyring-available install is what the prior code did and
    caused callers to silently downgrade to plaintext storage."""

    def test_set_raises_on_backend_exception(self, monkeypatch):
        fake = MagicMock()

        class _PasswordDeleteError(Exception):
            pass

        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = _PasswordDeleteError

        def _raise(*_args, **_kwargs):
            raise RuntimeError("backend refused")

        fake.set_password = _raise
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        with pytest.raises(secrets_store.SecretWriteError):
            secrets_store.set_secret("discord-client-secret", "value")

    def test_delete_raises_on_backend_exception(self, monkeypatch):
        fake = MagicMock()

        class _PasswordDeleteError(Exception):
            pass

        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = _PasswordDeleteError

        def _raise(*_args, **_kwargs):
            raise RuntimeError("backend refused")

        fake.delete_password = _raise
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        with pytest.raises(secrets_store.SecretWriteError):
            secrets_store.set_secret("discord-client-secret", "")

    def test_get_returns_none_on_backend_exception(self, monkeypatch):
        """get_secret keeps catch-and-return-None semantics: a read
        failure can't damage anything, the property fallback path
        handles a missing value safely."""
        fake = MagicMock()

        def _raise(*_args, **_kwargs):
            raise RuntimeError("backend refused")

        fake.get_password = _raise
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)
        assert secrets_store.get_secret("discord-client-secret") is None

    def test_set_returns_false_on_null_backend_instead_of_raising(self, monkeypatch):
        """a null backend (keyring importable but no
        usable backend, e.g. headless Linux without D-Bus) used to raise
        SecretWriteError from set_secret while is_available() returned
        False — the two predicates disagreed about "available". A null
        backend must take the documented plaintext-fallback path
        (return False) so Config setters route to the _legacy_*
        shadows rather than aborting Settings."""
        fake = MagicMock()
        backend = MagicMock()
        backend.name = "null"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        assert secrets_store.set_secret("discord-client-secret", "v") is False
        assert secrets_store.is_available() is False

    def test_clear_all_swallows_write_failures(self, monkeypatch):
        """clear_all is documented as never raising — a stuck single
        key must not block clearing the rest of the credential set."""
        fake = MagicMock()

        class _PasswordDeleteError(Exception):
            pass

        fake.errors = MagicMock()
        fake.errors.PasswordDeleteError = _PasswordDeleteError

        def _raise(*_args, **_kwargs):
            raise RuntimeError("backend refused")

        fake.delete_password = _raise
        backend = MagicMock()
        backend.name = "fake-backend"
        fake.get_keyring = MagicMock(return_value=backend)
        monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)

        # Must not raise.
        secrets_store.clear_all()
