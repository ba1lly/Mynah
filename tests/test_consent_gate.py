"""Tests for the recording-consent gate (issue #25).

The gate's behavioural contract:
1. RecordingSession accepts a `consent_record` constructor arg.
2. The record is persisted verbatim to participants.json on stop().
3. A None record persists as null — distinguishable from a missing
   field so downstream tools can detect "consent not collected" vs
   "this session predates the consent gate".
"""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest

from mynah.recorder import RecordingSession


def _session(consent_record):
    sess = RecordingSession.__new__(RecordingSession)
    sess.rpc = MagicMock()
    sess.audio = MagicMock()
    sess.audio.audio_layout.return_value = "channel_split"
    sess.audio.stop = MagicMock()
    sess.consent_record = consent_record
    sess._running = True
    sess._participants = ["Alice"]
    sess._participants_detailed = [{"id": "111", "name": "Alice"}]
    sess._self_user_id = "111"
    sess._voice_channel_id = "999"
    sess._events = []
    sess._events_lock = threading.Lock()
    sess._speaking_events = []
    sess._speaking_lock = threading.Lock()
    sess._speaking_subscribed = False
    sess._monitor_thread = None
    return sess


class TestConsentRecordPersisted:
    def test_consent_record_written_to_participants_json(self, tmp_path):
        record = {
            "granted_at": "2026-06-04T08:00:00+00:00",
            "granted_by_user_id": "111",
            "granted_by_username": "alice",
            "dialog_text": "This will record audio…",
        }
        sess = _session(record)
        sess.output_dir = tmp_path
        sess._base_name = "test"

        sess.stop()

        path = tmp_path / "test_participants.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["consent"] == record

    def test_none_consent_persisted_as_null(self, tmp_path):
        sess = _session(None)
        sess.output_dir = tmp_path
        sess._base_name = "test"
        sess.stop()
        data = json.loads((tmp_path / "test_participants.json").read_text())
        assert data["consent"] is None
        assert "consent" in data

    def test_init_accepts_consent_record(self, tmp_path):
        rpc = MagicMock()
        record = {"granted_at": "2026-06-04T08:00:00+00:00"}
        sess = RecordingSession(
            rpc=rpc,
            output_dir=tmp_path,
            consent_record=record,
        )
        assert sess.consent_record == record

    def test_init_consent_record_defaults_to_none(self, tmp_path):
        sess = RecordingSession(rpc=MagicMock(), output_dir=tmp_path)
        assert sess.consent_record is None


class TestCollectRecordingConsentDialog:
    """GUI-side coverage of `_collect_recording_consent`.

    Asserts the dialog accept-path returns a structured record, the
    decline-path returns None (recording is aborted before any audio
    capture), and identity-payload edge cases (missing fields, missing
    identity attr) do not crash and produce None-valued fields rather
    than KeyError.
    """

    def _bind_method(self, rpc_identity):
        """Build a minimal stand-in that exposes the method under test.

        MainWindow.__init__ touches tkinter, the filesystem, and the
        logging stack — none of which are needed by this method.
        We bind the unbound method onto a SimpleNamespace shell so we
        get the real implementation without the heavyweight setup.
        """
        from types import SimpleNamespace
        from mynah.gui import MainWindow

        rpc = MagicMock()
        rpc.identity = rpc_identity
        stub = SimpleNamespace(
            rpc=rpc,
            root=MagicMock(),
            _CONSENT_DIALOG_TEXT=MainWindow._CONSENT_DIALOG_TEXT,
            _CONSENT_DIALOG_VERSION=MainWindow._CONSENT_DIALOG_VERSION,
            _CONSENT_DIALOG_SHA256=MainWindow._CONSENT_DIALOG_SHA256,
        )
        return MainWindow._collect_recording_consent.__get__(stub, MainWindow)

    def test_accept_returns_record_with_identity(self, monkeypatch):
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: True)

        run = self._bind_method({
            "id": "111",
            "global_name": "Alice",
            "username": "alice#1234",
        })
        record = run()

        assert record is not None
        assert record["granted_by_user_id"] == "111"
        assert record["granted_by_username"] == "Alice"
        assert "granted_at" in record
        assert record["dialog_text"] == gui.MainWindow._CONSENT_DIALOG_TEXT

    def test_record_includes_dialog_version_and_sha256(self, monkeypatch):
        """consent record carries `dialog_version`
        and `dialog_sha256` so an auditor can verify byte-for-byte
        which dialog wording the user actually saw, even if the text
        is later reworded in a future release."""
        import hashlib
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: True)

        run = self._bind_method({"id": "111", "username": "alice"})
        record = run()

        assert record["dialog_version"] == "v1"
        expected_sha = hashlib.sha256(
            gui.MainWindow._CONSENT_DIALOG_TEXT.encode("utf-8"),
        ).hexdigest()
        assert record["dialog_sha256"] == expected_sha

    def test_decline_returns_none(self, monkeypatch):
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: False)

        run = self._bind_method({"id": "111", "username": "alice"})
        assert run() is None

    def test_window_close_returns_none(self, monkeypatch):
        """Tkinter's askokcancel returns None on some platforms when the
        window is closed via the X button — the `if not ok:` guard must
        treat None the same as False to abort recording."""
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: None)

        run = self._bind_method({"id": "111", "username": "alice"})
        assert run() is None

    def test_missing_identity_attr_does_not_crash(self, monkeypatch):
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: True)

        # Identity returns None — getattr fallback path.
        run = self._bind_method(None)
        record = run()

        assert record is not None
        assert record["granted_by_user_id"] is None
        assert record["granted_by_username"] is None

    def test_username_fallback_when_global_name_missing(self, monkeypatch):
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: True)

        run = self._bind_method({"id": "111", "username": "alice"})
        record = run()

        assert record["granted_by_username"] == "alice"

    def test_identity_fields_capped_at_256_chars(self, monkeypatch):
        """hostile/oversized display name must not
        inflate participants.json verbatim."""
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: True)

        bomb = "A" * 10_000
        run = self._bind_method({"id": bomb, "global_name": bomb})
        record = run()

        assert len(record["granted_by_user_id"]) == 256
        assert len(record["granted_by_username"]) == 256

    def test_identity_fields_scrubbed_of_bidi_and_control_chars(self, monkeypatch):
        """Discord display names are user-controlled
        and reach us via RPC. Bidi-override (\\u202e) or zero-width chars
        in a malicious name would otherwise land verbatim in the consent
        audit trail and the consent log line, corrupting both. _capped
        must apply _scrub before truncation."""
        from mynah import gui

        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: True)

        evil = "Alice\u202eybob\ufeff"  # RLO + BOM
        run = self._bind_method({"id": "111", "global_name": evil})
        record = run()

        assert "\u202e" not in record["granted_by_username"]
        assert "\ufeff" not in record["granted_by_username"]
        assert "Alice" in record["granted_by_username"]


class TestCappedHelperV23:
    """direct unit tests for _capped — previously
    only exercised indirectly via consent gate integration tests."""

    def test_none_passthrough(self):
        from mynah.gui import _capped
        assert _capped(None) is None

    def test_empty_string_unchanged(self):
        from mynah.gui import _capped
        assert _capped("") == ""

    def test_short_string_unchanged(self):
        from mynah.gui import _capped
        assert _capped("alice") == "alice"

    def test_boundary_256_chars_unchanged(self):
        from mynah.gui import _capped
        s = "A" * 256
        assert _capped(s) == s

    def test_one_over_boundary_truncated(self):
        from mynah.gui import _capped
        s = "A" * 257
        assert len(_capped(s)) == 256

    def test_far_over_boundary_truncated(self):
        from mynah.gui import _capped
        assert len(_capped("A" * 100_000)) == 256

    def test_integer_coerced_to_string(self):
        from mynah.gui import _capped
        assert _capped(12345) == "12345"

    def test_bidi_chars_stripped_before_cap(self):
        from mynah.gui import _capped
        result = _capped("hi\u202ethere\ufeff")
        assert "\u202e" not in result
        assert "\ufeff" not in result


class TestApplySettingsAtomically:
    """extracted helper for the atomic-save
    rollback semantics. The previous `SettingsDialog._save` flow
    mutated `discord_client_id` and cleared the token BEFORE the
    secret writes that might fail, so a partial failure left the
    dialog telling the user 'Settings were NOT saved' while the
    in-memory config carried the new client_id, a deleted token,
    and a half-applied keyring state."""

    def _make_config(self, hf_token_set_secret_raises: bool):
        from unittest.mock import MagicMock
        from mynah import secrets_store

        existing_hf = "live-hf"

        class _Config:
            def __init__(self):
                self.discord_client_id = "orig_id"
                self._hf = existing_hf
                self._token = MagicMock()
                self.whisper_model = "large-v3-turbo"
                self.audio_source = "mixed"
                self.recordings_dir = "/tmp/x"
                self.save_called = False

            @property
            def hf_token(self):
                return self._hf

            @hf_token.setter
            def hf_token(self, value):
                if hf_token_set_secret_raises and value != existing_hf:
                    raise secrets_store.SecretWriteError(
                        "keyring write for huggingface-token failed"
                    )
                self._hf = value

            @property
            def token(self):
                return self._token

            @token.setter
            def token(self, value):
                self._token = value

            def save(self):
                self.save_called = True

        return _Config()

    def _new_values(self):
        return {
            "discord_client_id": "new_id",
            "hf_token": "new-hf",
            "whisper_model": "large-v3-turbo",
            "audio_source": "mixed",
            "recordings_dir": "/tmp/y",
        }

    def test_secret_write_failure_rolls_back_and_leaves_client_id_unchanged(self):
        import mynah.gui as gui_mod
        from mynah import secrets_store

        cfg = self._make_config(hf_token_set_secret_raises=True)
        err = gui_mod.apply_settings_atomically(cfg, self._new_values())

        assert isinstance(err, secrets_store.SecretWriteError)
        assert cfg.discord_client_id == "orig_id"
        assert cfg._hf == "live-hf"
        assert cfg.recordings_dir == "/tmp/x"

    def test_all_secret_writes_succeed_commits_non_secret_fields(self):
        import mynah.gui as gui_mod

        cfg = self._make_config(hf_token_set_secret_raises=False)
        err = gui_mod.apply_settings_atomically(cfg, self._new_values())

        assert err is None
        assert cfg.discord_client_id == "new_id"
        assert cfg._hf == "new-hf"
        assert cfg.recordings_dir == "/tmp/y"

    def test_rollback_logs_when_restore_itself_raises(self, monkeypatch, caplog):
        """when the primary secret write fails
        AND the rollback's restore-to-original also raises (double
        failure), the rollback was silently swallowed without any log
        line. The fix matches the config.py rollback pattern and
        emits a warning naming the failed rollback key."""
        from unittest.mock import MagicMock
        from mynah import secrets_store
        import mynah.gui as gui_mod

        class _Config:
            def __init__(self):
                self.discord_client_id = "orig_id"
                self._hf = "live-hf"
                self._token = MagicMock()
                self.whisper_model = "large-v3-turbo"
                self.audio_source = "mixed"
                self.recordings_dir = "/tmp/x"
                self._token_set_count = 0

            @property
            def hf_token(self):
                return self._hf

            @hf_token.setter
            def hf_token(self, value):
                if value != "live-hf":
                    raise secrets_store.SecretWriteError(
                        "primary write of hf_token failed"
                    )
                self._hf = value

            @property
            def token(self):
                return self._token

            @token.setter
            def token(self, value):
                # First set (clear to None when client_id changes)
                # succeeds; the rollback's restore-to-original raises.
                self._token_set_count += 1
                if self._token_set_count >= 2:
                    raise secrets_store.SecretWriteError(
                        "rollback restore for token failed"
                    )
                self._token = value

            def save(self):
                pass

        cfg = _Config()
        new_values = {
            "discord_client_id": "new_id",
            "hf_token": "new-hf",
            "whisper_model": "large-v3-turbo",
            "audio_source": "mixed",
            "recordings_dir": "/tmp/y",
        }
        with caplog.at_level("WARNING"):
            err = gui_mod.apply_settings_atomically(cfg, new_values)

        assert isinstance(err, secrets_store.SecretWriteError)
        assert any(
            "rollback of token failed" in r.message.lower()
            for r in caplog.records
        )


class TestStartRecordingConsentAbort:
    """the consent gate's behavioural contract
    requires that recording aborts BEFORE any audio capture when the
    user declines. The previous tests verified `_collect_recording_consent`
    returns None on decline; this verifies the consumer (`_start_recording`)
    actually respects that signal and does not construct a `RecordingSession`."""

    def test_start_recording_does_not_construct_session_when_consent_declined(
        self, monkeypatch,
    ):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import mynah.gui as gui_mod

        session_was_constructed = []

        class _BoomSession:
            def __init__(self, *_a, **_k):
                session_was_constructed.append(True)
                raise AssertionError(
                    "RecordingSession constructed despite consent decline"
                )

        monkeypatch.setattr(gui_mod, "RecordingSession", _BoomSession)
        monkeypatch.setattr(
            gui_mod.messagebox, "askokcancel", lambda *a, **k: False,
        )
        monkeypatch.setattr(
            gui_mod.messagebox, "showerror", lambda *a, **k: None,
        )

        rpc = MagicMock()
        rpc.identity = {"id": "111", "username": "alice"}
        rpc.get_voice_channel.return_value = "999"

        stub = SimpleNamespace(
            rpc=rpc,
            root=MagicMock(),
            config=MagicMock(
                ensure_recordings_dir=MagicMock(return_value="/tmp/x"),
                audio_source="mixed",
            ),
            meeting_name=MagicMock(get=lambda: ""),
            session=None,
            start_btn=MagicMock(),
            stop_btn=MagicMock(),
            rec_lbl=MagicMock(),
            _set_status=lambda *a, **k: None,
            _CONSENT_DIALOG_TEXT=gui_mod.MainWindow._CONSENT_DIALOG_TEXT,
        )

        collect = gui_mod.MainWindow._collect_recording_consent.__get__(
            stub, gui_mod.MainWindow,
        )
        start = gui_mod.MainWindow._start_recording.__get__(
            stub, gui_mod.MainWindow,
        )
        stub._collect_recording_consent = collect
        start()

        assert not session_was_constructed
        assert stub.session is None
