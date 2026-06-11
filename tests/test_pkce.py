"""PKCE OAuth flow (issue #1): no client_secret anywhere, S256 challenge
bound at AUTHORIZE, verifier proven at the token exchange, state checked
when echoed, and actionable guidance when 'Public Client' is missing."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mynah.rpc import DiscordRPC, RpcError
import mynah.rpc as rpc_mod


def _rpc() -> DiscordRPC:
    return DiscordRPC("123456789012345678")


class TestConstructor:
    def test_client_id_only(self):
        rpc = _rpc()
        assert rpc.client_id == "123456789012345678"
        assert not hasattr(rpc, "client_secret")

    def test_missing_client_id_raises(self):
        with pytest.raises(RpcError, match="client_id is required"):
            DiscordRPC("")


class TestPkcePair:
    def test_verifier_meets_rfc7636(self):
        verifier, _ = DiscordRPC._make_pkce_pair()
        assert 43 <= len(verifier) <= 128
        allowed = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        )
        assert set(verifier) <= allowed

    def test_challenge_is_s256_of_verifier_unpadded(self):
        verifier, challenge = DiscordRPC._make_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected
        assert "=" not in challenge

    def test_pair_is_fresh_per_call(self):
        v1, _ = DiscordRPC._make_pkce_pair()
        v2, _ = DiscordRPC._make_pkce_pair()
        assert v1 != v2


def _ok_token_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_in": 3600,
    }
    resp.raise_for_status.return_value = None
    return resp


class TestAuthorize:
    def _run_authorize(self, monkeypatch, cmd_response_extra=None):
        """Drive _authorize with a mocked pipe + token endpoint and
        return (authorize_args, token_post_form)."""
        rpc = _rpc()
        captured: dict = {}

        def fake_cmd(cmd, args, timeout=None):
            assert cmd == "AUTHORIZE"
            captured["args"] = args
            data = {"code": "the-code"}
            if cmd_response_extra:
                data.update(cmd_response_extra(args))
            return data

        monkeypatch.setattr(rpc, "_cmd", fake_cmd)

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["form"] = data
            return _ok_token_response()

        monkeypatch.setattr(rpc_mod.requests, "post", fake_post)
        token = rpc._authorize()
        return captured["args"], captured["form"], token

    def test_authorize_sends_challenge_method_and_state(self, monkeypatch):
        args, _, _ = self._run_authorize(monkeypatch)
        assert args["code_challenge_method"] == "S256"
        assert args["code_challenge"]
        assert args["state"]
        assert "client_secret" not in args

    def test_exchange_posts_verifier_and_no_secret(self, monkeypatch):
        args, form, token = self._run_authorize(monkeypatch)
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "the-code"
        assert "client_secret" not in form
        # The posted verifier must hash to exactly the challenge that
        # was bound at AUTHORIZE.
        rehashed = (
            base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        assert rehashed == args["code_challenge"]
        assert token["access_token"] == "at"

    def test_echoed_matching_state_accepted(self, monkeypatch):
        _, _, token = self._run_authorize(
            monkeypatch, cmd_response_extra=lambda args: {"state": args["state"]}
        )
        assert token["access_token"] == "at"

    def test_echoed_mismatched_state_raises(self, monkeypatch):
        with pytest.raises(RpcError, match="state mismatch"):
            self._run_authorize(
                monkeypatch,
                cmd_response_extra=lambda args: {"state": "attacker-state"},
            )

    def test_absent_state_tolerated(self, monkeypatch):
        # Discord's RPC AUTHORIZE does not currently echo state; that
        # must not break connect.
        _, _, token = self._run_authorize(monkeypatch)
        assert token["access_token"] == "at"

    def test_no_code_raises(self, monkeypatch):
        rpc = _rpc()
        monkeypatch.setattr(rpc, "_cmd", lambda *a, **k: {})
        with pytest.raises(RpcError, match="no code"):
            rpc._authorize()


class TestRefresh:
    def test_refresh_posts_client_id_only(self, monkeypatch):
        rpc = _rpc()
        captured: dict = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["form"] = data
            return _ok_token_response()

        monkeypatch.setattr(rpc_mod.requests, "post", fake_post)
        token = rpc._refresh_token("rt-old")

        form = captured["form"]
        assert form["grant_type"] == "refresh_token"
        assert form["client_id"] == rpc.client_id
        assert "client_secret" not in form
        assert "code_verifier" not in form
        assert token["access_token"] == "at"


class TestInvalidClientGuidance:
    def test_invalid_client_explains_public_client_flag(self, monkeypatch):
        rpc = _rpc()
        resp = MagicMock()
        resp.status_code = 401
        resp.json.return_value = {"error": "invalid_client"}
        monkeypatch.setattr(rpc_mod.requests, "post", lambda *a, **k: resp)

        with pytest.raises(RpcError, match="Public Client"):
            rpc._refresh_token("rt")

    def test_other_http_errors_still_raise(self, monkeypatch):
        rpc = _rpc()
        resp = MagicMock()
        resp.status_code = 500
        resp.json.side_effect = ValueError("not json")
        resp.raise_for_status.side_effect = Exception("boom")
        monkeypatch.setattr(rpc_mod.requests, "post", lambda *a, **k: resp)

        with pytest.raises(Exception, match="boom"):
            rpc._refresh_token("rt")


class TestLegacySecretCleanup:
    """Config.load() must purge a Client Secret stored by pre-PKCE
    versions from the OS credential store."""

    def test_load_deletes_stored_client_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ):
        from tests.test_config import _install_fake_keyring  # reuse fixture helper
        import mynah.config as config_module
        from mynah import secrets_store

        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")
        store = _install_fake_keyring(monkeypatch)
        store[(secrets_store.SERVICE_NAME, "discord-client-secret")] = "old-secret"
        (tmp_path / "config.json").write_text(
            json.dumps({"discord_client_id": "id1"}), encoding="utf-8",
        )

        with caplog.at_level("INFO"):
            config_module.Config.load()

        assert (
            secrets_store.SERVICE_NAME, "discord-client-secret",
        ) not in store
        assert any("PKCE" in r.message for r in caplog.records)

    def test_load_quiet_when_no_stored_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ):
        from tests.test_config import _install_fake_keyring
        import mynah.config as config_module

        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")
        _install_fake_keyring(monkeypatch)
        (tmp_path / "config.json").write_text(
            json.dumps({"discord_client_id": "id1"}), encoding="utf-8",
        )

        with caplog.at_level("INFO"):
            config_module.Config.load()

        assert not any(
            "client secret" in r.message.lower() for r in caplog.records
        )
