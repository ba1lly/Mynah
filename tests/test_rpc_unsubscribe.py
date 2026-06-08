"""Tests for DiscordRPC.unsubscribe() wire ordering."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mynah.rpc import DiscordRPC


def _make_rpc() -> DiscordRPC:
    """Construct a DiscordRPC instance without actually opening a pipe."""
    rpc = DiscordRPC("client_id", "client_secret")
    rpc._cmd = MagicMock(name="_cmd")  # type: ignore[assignment]
    return rpc


class TestUnsubscribeWireOrdering:
    """— the wire UNSUBSCRIBE must NOT be sent while other local
    listeners remain for the same event."""

    def test_single_listener_unsubscribe_sends_wire_command(self) -> None:
        rpc = _make_rpc()
        cb = lambda data: None
        rpc._event_listeners["SPEAKING_START"] = [cb]
        rpc.unsubscribe("SPEAKING_START", {"channel_id": "c1"}, callback=cb)
        rpc._cmd.assert_called_once_with(  # type: ignore[attr-defined]
            "UNSUBSCRIBE", {"channel_id": "c1"}, evt="SPEAKING_START", timeout=5.0,
        )
        assert "SPEAKING_START" not in rpc._event_listeners

    def test_multi_listener_unsubscribe_one_does_not_send_wire(self) -> None:
        # The whole point: removing one callback leaves the
        # other registered, so Discord MUST keep delivering events.
        rpc = _make_rpc()
        cb1 = lambda data: None
        cb2 = lambda data: None
        rpc._event_listeners["SPEAKING_START"] = [cb1, cb2]
        rpc.unsubscribe("SPEAKING_START", {"channel_id": "c1"}, callback=cb1)
        rpc._cmd.assert_not_called()  # type: ignore[attr-defined]
        assert rpc._event_listeners["SPEAKING_START"] == [cb2]

    def test_multi_listener_unsubscribe_last_sends_wire(self) -> None:
        rpc = _make_rpc()
        cb1 = lambda data: None
        cb2 = lambda data: None
        rpc._event_listeners["SPEAKING_START"] = [cb1, cb2]
        rpc.unsubscribe("SPEAKING_START", {"channel_id": "c1"}, callback=cb1)
        rpc.unsubscribe("SPEAKING_START", {"channel_id": "c1"}, callback=cb2)
        assert rpc._cmd.call_count == 1  # type: ignore[attr-defined]
        assert "SPEAKING_START" not in rpc._event_listeners

    def test_legacy_callback_none_drops_all_and_sends_wire(self) -> None:
        rpc = _make_rpc()
        rpc._event_listeners["SPEAKING_START"] = [lambda d: None, lambda d: None]
        rpc.unsubscribe("SPEAKING_START", {"channel_id": "c1"}, callback=None)
        rpc._cmd.assert_called_once()  # type: ignore[attr-defined]
        assert "SPEAKING_START" not in rpc._event_listeners

    def test_wire_failure_does_not_re_raise(self) -> None:
        # — UNSUBSCRIBE wire failures are logged at debug and
        # swallowed; the listener removal already happened atomically
        # under the lock, so the caller does not need to know.
        rpc = _make_rpc()
        rpc._cmd.side_effect = Exception("pipe closed")  # type: ignore[attr-defined]
        cb = lambda data: None
        rpc._event_listeners["SPEAKING_START"] = [cb]
        rpc.unsubscribe("SPEAKING_START", {"channel_id": "c1"}, callback=cb)
        assert "SPEAKING_START" not in rpc._event_listeners

    def test_unknown_callback_does_not_send_wire(self) -> None:
        rpc = _make_rpc()
        cb1 = lambda data: None
        cb_unknown = lambda data: None
        rpc._event_listeners["SPEAKING_START"] = [cb1]
        rpc.unsubscribe("SPEAKING_START", {"channel_id": "c1"}, callback=cb_unknown)
        # cb1 is still there, so no wire UNSUBSCRIBE.
        rpc._cmd.assert_not_called()  # type: ignore[attr-defined]
        assert rpc._event_listeners["SPEAKING_START"] == [cb1]
