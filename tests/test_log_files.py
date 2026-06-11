"""On-disk logs (issue #19): scrubbed-but-newline-preserving file
handler, log_dir path resolution, and the open-folder backend method."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mynah.config import log_dir
from mynah.webui import ScrubbedTimedFileHandler

class TestLogDir:
    def test_honours_app_root_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MYNAH_APP_ROOT", str(tmp_path))
        assert log_dir() == tmp_path / "logs"

class TestScrubbedFileHandler:
    def _emit(self, tmp_path: Path, message: str) -> str:
        handler = ScrubbedTimedFileHandler(
            str(tmp_path / "mynah.log"), when="midnight",
            backupCount=7, encoding="utf-8", delay=True,
        )
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, message, None, None,
        )
        handler.emit(record)
        handler.close()
        return (tmp_path / "mynah.log").read_text(encoding="utf-8")

    def test_bidi_override_scrubbed(self, tmp_path):
        out = self._emit(tmp_path, "name ‮ evil")
        assert "‮" not in out
        assert "?" in out

    def test_newlines_preserved_for_tracebacks(self, tmp_path):
        out = self._emit(tmp_path, "line one\nline two")
        assert "line one\nline two" in out

    def test_plain_text_unchanged(self, tmp_path):
        out = self._emit(tmp_path, "Recording started with: Alice, Bob")
        assert "Recording started with: Alice, Bob" in out

class TestOpenLogFolder:
    def test_creates_and_reports_ok(self, tmp_path, monkeypatch):
        from mynah.backend import MynahBackend
        from mynah.config import Config

        monkeypatch.setenv("MYNAH_APP_ROOT", str(tmp_path))
        opened = []
        b = MynahBackend(Config())
        monkeypatch.setattr(
            MynahBackend, "_open_folder",
            staticmethod(lambda path: opened.append(path) or {"ok": True}),
        )
        res = b.open_log_folder()
        assert res["ok"] is True
        assert opened == [tmp_path / "logs"]
        assert (tmp_path / "logs").is_dir()
