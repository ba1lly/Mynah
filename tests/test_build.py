"""Tests for build.py helpers (F3/F17)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def build_mod():
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import build as _build
    return _build


@pytest.fixture
def move_aside(build_mod):
    return build_mod._move_aside


class TestMoveAsidePreservesLastKnownGood:
    """— repeated failed builds must not destroy the existing .bak."""

    def test_no_existing_p_returns_existing_bak(self, move_aside, tmp_path: Path) -> None:
        p = tmp_path / "dist"
        bak = tmp_path / "dist.bak"
        bak.mkdir()
        (bak / "Mynah.exe").write_bytes(b"good-build")
        result = move_aside(p)
        assert result == bak
        assert (bak / "Mynah.exe").read_bytes() == b"good-build"

    def test_no_existing_p_no_existing_bak_returns_none(self, move_aside, tmp_path: Path) -> None:
        assert move_aside(tmp_path / "dist") is None

    def test_first_failure_creates_bak(self, move_aside, tmp_path: Path) -> None:
        p = tmp_path / "dist"
        p.mkdir()
        (p / "marker.txt").write_text("v1")
        result = move_aside(p)
        assert result == tmp_path / "dist.bak"
        assert (tmp_path / "dist.bak" / "marker.txt").read_text() == "v1"
        assert not p.exists()

    def test_repeated_failure_preserves_first_bak(self, move_aside, tmp_path: Path) -> None:
        # Simulate: v1 build, success → no bak. Then v2 build fails,
        # leaves partial dist. _move_aside saves v1 dist as dist.bak.
        p = tmp_path / "dist"
        p.mkdir()
        (p / "marker.txt").write_text("v1")
        move_aside(p)
        assert (tmp_path / "dist.bak" / "marker.txt").read_text() == "v1"

        # Second build attempt creates a partial dist (e.g. a half-baked v2)
        # that fails. The pre-existing dist.bak (v1) is the last known good.
        p.mkdir()
        (p / "marker.txt").write_text("v2-partial")

        # A third build runs _move_aside again. The pre-existing v1 .bak
        # MUST survive — the previous bug overwrote it with v2-partial.
        result = move_aside(p)
        assert result == tmp_path / "dist.bak"
        assert (tmp_path / "dist.bak" / "marker.txt").read_text() == "v1"
        assert not p.exists()


# ---- F3/F17: SBOM + license-extraction tests ----


def _meta(items: dict, classifiers=None):
    """Build a fake email.Message-like object matching the importlib.metadata
    PackageMetadata surface we touch in build._extract_license / _extract_homepage."""
    classifiers = classifiers or []

    class FakeMeta:
        def __init__(self, items, classifiers):
            self._items = items
            self._classifiers = classifiers

        def get(self, key, default=None):
            return self._items.get(key, default)

        def get_all(self, key):
            if key == "Classifier":
                return self._classifiers
            return None

        def __getitem__(self, key):
            return self._items[key]

    return FakeMeta(items, classifiers)


class TestExtractLicense:
    """license.id is reserved for SPDX expressions
    (PEP 639 License-Expression). Non-SPDX values from `License:` or
    Trove classifiers must signal is_spdx=False so the SBOM emits
    them under `license.name` instead."""

    def test_license_expression_is_spdx(self, build_mod):
        meta = _meta({"License-Expression": "MIT"})
        assert build_mod._extract_license(meta) == ("MIT", True)

    def test_license_expression_compound_spdx(self, build_mod):
        meta = _meta({"License-Expression": "Apache-2.0 OR MIT"})
        assert build_mod._extract_license(meta) == ("Apache-2.0 OR MIT", True)

    def test_license_field_falls_back_to_name(self, build_mod):
        meta = _meta({"License": "BSD"})
        assert build_mod._extract_license(meta) == ("BSD", False)

    def test_trove_classifier_falls_back_to_name(self, build_mod):
        meta = _meta({}, classifiers=["License :: OSI Approved :: MIT License"])
        assert build_mod._extract_license(meta) == ("MIT License", False)

    def test_license_field_unknown_skips(self, build_mod):
        meta = _meta({"License": "UNKNOWN"})
        assert build_mod._extract_license(meta) is None

    def test_no_license_returns_none(self, build_mod):
        meta = _meta({})
        assert build_mod._extract_license(meta) is None


class TestExtractHomepage:
    def test_homepage_field(self, build_mod):
        meta = _meta({"Home-page": "https://example.org"})
        assert build_mod._extract_homepage(meta) == "https://example.org"

    def test_project_url_homepage_label(self, build_mod):
        meta = MagicMock()
        meta.get = lambda key, default=None: default
        meta.get_all = lambda key: ["Homepage, https://example.org"] if key == "Project-URL" else None
        assert build_mod._extract_homepage(meta) == "https://example.org"


class TestGenerateSbom:
    """cover SBOM scaffolding + F3 (SPDX vs name) + F4
    (runtime-closure filter excludes dev-only packages)."""

    def test_writes_cyclonedx_with_sorted_components(self, build_mod, tmp_path, monkeypatch):
        app_dir = tmp_path / build_mod.DIST_APP_DIRNAME
        app_dir.mkdir()

        dists = [
            MagicMock(metadata=_meta({
                "Name": "Beta", "Version": "1.0", "License-Expression": "MIT",
            })),
            MagicMock(metadata=_meta({
                "Name": "Alpha", "Version": "2.0",
            }, classifiers=["License :: OSI Approved :: Apache Software License"])),
        ]
        monkeypatch.setattr(
            build_mod.importlib.metadata, "distributions", lambda: iter(dists),
        )
        # Disable closure filter so the mocked distributions reach the
        # output (their names are not in any real pyproject closure).
        monkeypatch.setattr(build_mod, "_project_runtime_closure", lambda: None)

        out = build_mod.generate_sbom(tmp_path)
        sbom = json.loads(out.read_text())

        assert sbom["bomFormat"] == "CycloneDX"
        assert sbom["specVersion"] == "1.5"
        assert sbom["metadata"]["licenses"] == [
            {"license": {"name": "PolyForm-Noncommercial-1.0.0"}},
        ]
        names = [c["name"] for c in sbom["components"]]
        assert names == ["Alpha", "Beta"]

    def test_spdx_emits_license_id_non_spdx_emits_name(
        self, build_mod, tmp_path, monkeypatch,
    ):
        """F3 regression: SBOM was writing Trove-classifier strings
        like "MIT License" into license.id, which is reserved for SPDX
        identifiers (e.g. "MIT", not "MIT License")."""
        app_dir = tmp_path / build_mod.DIST_APP_DIRNAME
        app_dir.mkdir()

        dists = [
            MagicMock(metadata=_meta({
                "Name": "SpdxPkg", "Version": "1.0",
                "License-Expression": "Apache-2.0",
            })),
            MagicMock(metadata=_meta({
                "Name": "TrovePkg", "Version": "1.0",
            }, classifiers=["License :: OSI Approved :: MIT License"])),
        ]
        monkeypatch.setattr(
            build_mod.importlib.metadata, "distributions", lambda: iter(dists),
        )
        monkeypatch.setattr(build_mod, "_project_runtime_closure", lambda: None)

        build_mod.generate_sbom(tmp_path)
        sbom = json.loads((app_dir / "SBOM.json").read_text())
        by_name = {c["name"]: c for c in sbom["components"]}

        assert by_name["SpdxPkg"]["licenses"] == [
            {"license": {"id": "Apache-2.0"}},
        ]
        assert by_name["TrovePkg"]["licenses"] == [
            {"license": {"name": "MIT License"}},
        ]

    def test_closure_filter_excludes_packages_not_in_runtime_closure(
        self, build_mod, tmp_path, monkeypatch,
    ):
        """F4 regression: SBOM enumerated every package in the dev
        interpreter (pyinstaller, pytest, etc.). With the closure
        filter, only declared/transitive runtime deps are emitted."""
        app_dir = tmp_path / build_mod.DIST_APP_DIRNAME
        app_dir.mkdir()

        dists = [
            MagicMock(metadata=_meta({"Name": "numpy", "Version": "1.0"})),
            MagicMock(metadata=_meta({"Name": "pytest", "Version": "8.0"})),
            MagicMock(metadata=_meta({"Name": "pyinstaller", "Version": "6.0"})),
        ]
        monkeypatch.setattr(
            build_mod.importlib.metadata, "distributions", lambda: iter(dists),
        )
        # Pretend the project's runtime closure is just numpy. The
        # SBOM must drop the other two even though they appear in
        # importlib.metadata.distributions().
        monkeypatch.setattr(
            build_mod, "_project_runtime_closure", lambda: {"numpy"},
        )

        build_mod.generate_sbom(tmp_path)
        sbom = json.loads((app_dir / "SBOM.json").read_text())
        names = [c["name"] for c in sbom["components"]]
        assert names == ["numpy"]

    def test_missing_app_dir_skips_silently(self, build_mod, tmp_path):
        out = build_mod.generate_sbom(tmp_path)
        assert not out.exists()


class TestProjectRuntimeClosure:
    """verify the canonicalization + PEP 508 parsing
    that drives the closure filter."""

    def test_canon_normalises_dashes_underscores_dots(self, build_mod):
        assert build_mod._canon("My_Package.Name") == "my-package-name"
        assert build_mod._canon("MY-PACKAGE-NAME") == "my-package-name"

    def test_parse_req_name_strips_version_and_markers(self, build_mod):
        assert build_mod._parse_req_name(
            "PyAudioWPatch>=0.2.12.7; sys_platform == 'win32'"
        ) == "pyaudiowpatch"
        assert build_mod._parse_req_name("numpy>=1.24") == "numpy"
        assert build_mod._parse_req_name("requests") == "requests"

    def test_closure_includes_declared_runtime_deps(self, build_mod):
        """When pyproject.toml is present, the closure must include
        each declared runtime dep by canonical name."""
        closure = build_mod._project_runtime_closure()
        if closure is None:
            pytest.skip("tomllib not available and pyproject.toml unreadable")
        for name in ("numpy", "requests", "scipy", "soundfile", "keyring"):
            assert name in closure

    def test_closure_walks_transitive_dependencies(self, build_mod, monkeypatch, tmp_path):
        """closure walks transitive deps via
        importlib.metadata.requires() — not just the declared seeds."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["alpha"]\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(build_mod, "ROOT", tmp_path)
        monkeypatch.setattr(
            build_mod, "_spec_bundled_packages", lambda: set(),
        )

        def fake_requires(name):
            if name == "alpha":
                return ["beta>=1.0"]
            if name == "beta":
                return ["gamma"]
            return []

        monkeypatch.setattr(
            build_mod.importlib.metadata, "requires", fake_requires,
        )

        closure = build_mod._project_runtime_closure()

        assert closure is not None
        assert "alpha" in closure
        assert "beta" in closure
        assert "gamma" in closure

    def test_closure_returns_none_when_tomllib_unavailable(
        self, build_mod, monkeypatch, tmp_path,
    ):
        """explicit assertion that the fallback
        returns None — previously the test class skipped this path,
        so a regression where the fallback raised would skip rather
        than fail."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")
        monkeypatch.setattr(build_mod, "ROOT", tmp_path)

        import builtins
        real_import = builtins.__import__

        def _fail_toml(name, *args, **kwargs):
            if name in ("tomllib", "tomli"):
                raise ImportError(f"no module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_toml)

        assert build_mod._project_runtime_closure() is None

    def test_closure_returns_none_on_malformed_pyproject(
        self, build_mod, monkeypatch, tmp_path,
    ):
        """malformed TOML must hit the
        `except Exception: return None` fallback, not crash."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project\nname = \"x\"\n", encoding="utf-8")
        monkeypatch.setattr(build_mod, "ROOT", tmp_path)

        assert build_mod._project_runtime_closure() is None


class TestGenerateDllManifest:
    def test_hashes_only_listed_suffixes(self, build_mod, tmp_path):
        app_dir = tmp_path / build_mod.DIST_APP_DIRNAME
        internal = app_dir / "_internal"
        internal.mkdir(parents=True)
        (internal / "ok.dll").write_bytes(b"abc")
        (internal / "ok.pyd").write_bytes(b"def")
        (internal / "ignore.txt").write_bytes(b"ghi")

        build_mod.generate_dll_manifest(tmp_path)
        manifest = json.loads((app_dir / "DLL_MANIFEST.json").read_text())

        names = {e["filename"] for e in manifest["entries"]}
        assert names == {"ok.dll", "ok.pyd"}
        for entry in manifest["entries"]:
            assert entry["sha256"] == hashlib.sha256(
                b"abc" if entry["filename"] == "ok.dll" else b"def"
            ).hexdigest()

    def test_falls_back_to_app_dir_when_internal_missing(self, build_mod, tmp_path):
        app_dir = tmp_path / build_mod.DIST_APP_DIRNAME
        app_dir.mkdir()
        (app_dir / "lone.dll").write_bytes(b"x")

        build_mod.generate_dll_manifest(tmp_path)
        manifest = json.loads((app_dir / "DLL_MANIFEST.json").read_text())

        assert [e["filename"] for e in manifest["entries"]] == ["lone.dll"]
        assert manifest["scan_root"] == "."
