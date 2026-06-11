"""Update check (backend): version comparison and the GitHub releases
probe — newer-only, scrubbed notes, fail-silent on any error."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mynah.backend import MynahBackend, _parse_version
from mynah.config import Config


class TestParseVersion:
    @pytest.mark.parametrize("tag,expected", [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("v10.0.0", (10, 0, 0)),
        ("v1.2", (1, 2)),
        ("v1.2.3rc1", (1, 2, 3)),
        ("nonsense", ()),
        ("", ()),
    ])
    def test_cases(self, tag, expected):
        assert _parse_version(tag) == expected

    def test_ordering(self):
        assert _parse_version("v1.10.0") > _parse_version("v1.9.9")
        assert _parse_version("v2.0.0") > _parse_version("v1.99.99")


def _backend() -> MynahBackend:
    return MynahBackend(Config())


def _response(tag: str, body: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"tag_name": tag, "body": body}
    return resp


class TestFetchLatestRelease:
    def test_newer_release_returned_with_scrubbed_notes(self, monkeypatch):
        b = _backend()
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: _response("v999.0.0", "## New\n- stuff‮"),
        )
        update, err = b._fetch_latest_release()
        assert err is None
        assert update is not None
        assert update["version"] == "999.0.0"
        assert "\n" in update["notes"]          # newlines preserved
        assert "‮" not in update["notes"]  # bidi override scrubbed
        assert update["url"].startswith("https://github.com/")

    def test_same_version_returns_none(self, monkeypatch):
        import mynah

        b = _backend()
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: _response(f"v{mynah.__version__}", "notes"),
        )
        assert b._fetch_latest_release() == (None, None)

    def test_older_version_returns_none(self, monkeypatch):
        b = _backend()
        monkeypatch.setattr(
            "requests.get", lambda *a, **k: _response("v0.0.1", "notes"),
        )
        assert b._fetch_latest_release() == (None, None)

    def test_network_error_returns_none(self, monkeypatch):
        b = _backend()

        def boom(*_a, **_k):
            raise OSError("no network")

        monkeypatch.setattr("requests.get", boom)
        update, err = b._fetch_latest_release()
        assert update is None
        assert err is not None and "no network" in err

    def test_garbage_tag_returns_none(self, monkeypatch):
        b = _backend()
        monkeypatch.setattr(
            "requests.get", lambda *a, **k: _response("", "notes"),
        )
        update, err = b._fetch_latest_release()
        assert update is None
        assert err is not None

    def test_crlf_notes_keep_clean_line_endings(self, monkeypatch):
        # CR sits inside the scrubbed control range; normalising after
        # scrubbing left a stray '?' at the end of every CRLF line.
        b = _backend()
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: _response("v999.0.0", "line one\r\nline two\r\n"),
        )
        update, _err = b._fetch_latest_release()
        assert update is not None
        assert "?" not in update["notes"]
        assert update["notes"] == "line one\nline two\n"

    def test_notes_capped(self, monkeypatch):
        b = _backend()
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: _response("v999.0.0", "x" * 100_000),
        )
        update, _err = b._fetch_latest_release()
        assert update is not None
        assert len(update["notes"]) <= 20_000


class TestManualCheck:
    def test_check_for_updates_caches_result(self, monkeypatch):
        b = _backend()
        calls = []

        def fake_get(*a, **k):
            calls.append(1)
            return _response("v999.0.0", "notes")

        monkeypatch.setattr("requests.get", fake_get)
        first = b.check_for_updates()
        second = b.check_for_updates()
        assert first["update"]["version"] == "999.0.0"
        assert second["update"]["version"] == "999.0.0"
        assert len(calls) == 1  # cached after the first hit

    def test_fetch_failure_reports_error_not_up_to_date(self, monkeypatch):
        """Offline must NOT produce a confident 'you're on the latest
        version' — the UI shows a couldn't-check error instead."""
        b = _backend()

        def boom(*_a, **_k):
            raise OSError("no network")

        monkeypatch.setattr("requests.get", boom)
        res = b.check_for_updates()
        assert res["ok"] is False
        assert "no network" in res["error"]

    def test_disabled_setting_skips_launch_check(self):
        cfg = Config()
        cfg.check_updates = False
        b = MynahBackend(cfg)
        b._start_update_check()  # must not spawn the probe thread
        import threading

        assert not any(
            t.name == "update-check" for t in threading.enumerate()
        )
