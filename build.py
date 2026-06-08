"""Build a standalone Windows distribution with PyInstaller.

Usage:
    python build.py
    python build.py --sbom-only        # regenerate SBOM.json against existing dist/
    python build.py --dll-manifest-only  # regenerate DLL_MANIFEST.json against existing dist/

Output:
    dist/Mynah/Mynah.exe  (plus support files)
    dist/Mynah/SBOM.json            (CycloneDX 1.5)
    dist/Mynah/DLL_MANIFEST.json    (bundled native binaries)

The previous build is preserved as `dist.bak/` until PyInstaller succeeds,
so a failed build doesn't destroy the last known-good `.exe` (a real cost
when a full build takes ~10 minutes).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST_APP_DIRNAME = "Mynah"


def _canon(name: str) -> str:
    """Canonicalise a PEP 503 distribution name for comparison.

    `Pillow`, `pillow`, `PILLOW` all hash to the same canonical key
    `pillow`, matching how pip / importlib.metadata resolve names.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def _parse_req_name(spec: str) -> str | None:
    """Pull the bare distribution name out of a PEP 508 requirement.

    `"PyAudioWPatch>=0.2.12.7; sys_platform == 'win32'"` -> `"pyaudiowpatch"`.
    We do NOT need the version specifier or environment markers — only
    the canonical name to seed the transitive closure walk.
    """
    m = _NAME_RE.match(spec)
    return _canon(m.group(1)) if m else None


_SPEC_PACKAGES_RE = re.compile(
    r"_(?:critical|optional)_packages\s*=\s*\[(.*?)\]",
    re.DOTALL,
)
_SPEC_PACKAGE_ENTRY_RE = re.compile(r"[\"']([A-Za-z0-9_.\-]+)[\"']")


def _spec_bundled_packages() -> set[str]:
    """Read the PyInstaller spec for the package names actually bundled.

    fix: `pyproject.toml [project].dependencies`
    intentionally omits the transcription stack (torch, whisperx,
    pyannote, …) because those are installed out-of-band through the
    CUDA wheel index URL — the runbook installs them via `start.ps1`
    rather than `pip install`. PyInstaller still bundles them into
    the shipped `.exe` via `Mynah.spec`'s
    `_critical_packages` + `_optional_packages` lists. Without
    including these in the SBOM closure, the supply-chain artifact
    omits the largest and riskiest dependencies in the bundle.

    Returns canonical (PEP 503) names of every package listed in
    either spec array, or an empty set if the spec is unreachable.
    """
    spec = ROOT / "Mynah.spec"
    if not spec.exists():
        return set()
    try:
        text = spec.read_text(encoding="utf-8")
    except OSError:
        return set()
    names: set[str] = set()
    for block in _SPEC_PACKAGES_RE.findall(text):
        for entry in _SPEC_PACKAGE_ENTRY_RE.findall(block):
            names.add(_canon(entry))
    return names


def _marker_excludes(spec: str) -> bool:
    """True iff the requirement's environment marker excludes the
    current platform.

    fix: the previous transitive walk added every
    `requires()` entry to the closure regardless of PEP 508 markers,
    so `pywin32; sys_platform == "win32"` leaked into Linux SBOMs and
    extras-scoped entries (`PySocks; extra == "socks"`) leaked even
    when the extra was not requested.

    Falls back to NOT excluding on any parse/evaluation error so an
    unparseable marker doesn't silently strip a legitimate dep.
    """
    if ";" not in spec:
        return False
    marker_part = spec.split(";", 1)[1].strip()
    if not marker_part:
        return False
    # Extras-scoped entries (`pkg; extra == 'foo'`) are runtime-optional
    # by definition — exclude them from the runtime closure unless the
    # extra is actively in play (which we don't track in the SBOM tool).
    if "extra ==" in marker_part or 'extra == "' in marker_part:
        return True
    try:
        from packaging.markers import Marker
    except ImportError:
        return False
    try:
        return not Marker(marker_part).evaluate()
    except Exception:
        return False


def _project_runtime_closure() -> set[str] | None:
    """Return the set of canonical distribution names the shipped binary
    actually depends on at runtime (pyproject deps + PyInstaller
    spec-bundled packages + their transitive requirements), or None if
    we can't determine it (e.g. running from a frozen bundle with no
    pyproject.toml).

    Used by `generate_sbom` to filter `importlib.metadata.distributions()`
    down to packages the shipped binary really pulls in. Without this
    filter the SBOM enumerates the dev interpreter's entire site-packages
    (pyinstaller, pytest, black, …) and becomes non-reproducible across
    machines.
    """
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # 3.10 fallback if installed
        except ImportError:
            return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return None

    seeds: list[str] = list(data.get("project", {}).get("dependencies", []))
    seed_names: set[str] = set()
    for spec in seeds:
        if _marker_excludes(spec):
            continue
        name = _parse_req_name(spec)
        if name:
            seed_names.add(name)

    # Union with PyInstaller spec's bundled list so the SBOM covers what
    # actually ships, not just what the manifest declares.
    seed_names |= _spec_bundled_packages()

    if not seed_names:
        return None

    closure: set[str] = set()
    pending = list(seed_names)
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        try:
            requires = importlib.metadata.requires(name) or []
        except importlib.metadata.PackageNotFoundError:
            # Declared but not installed in this venv. Keep it in the
            # closure so the SBOM at least lists the promised name even
            # if no version/license can be resolved.
            continue
        for req_spec in requires:
            if _marker_excludes(req_spec):
                continue
            req_name = _parse_req_name(req_spec)
            if req_name and req_name not in closure:
                pending.append(req_name)
    return closure


def _move_aside(p: Path, suffix: str = ".bak") -> Path | None:
    """Rename `p` to `<p>.bak`, preserving any existing backup.

    On repeated failed builds, the existing `.bak` is the last
    known-good output. We do NOT overwrite it here. Instead, the
    current (possibly-broken) `p` is dropped/discarded — we are about
    to run PyInstaller and produce fresh output, so the contents of
    `p` are not interesting once the existing backup is preserved.

    Returns the existing backup path (or None if none existed and
    nothing needed moving aside).
    """
    if not p.exists():
        backup = p.with_name(p.name + suffix)
        return backup if backup.exists() else None

    backup = p.with_name(p.name + suffix)
    if backup.exists():
        # Last known-good output already preserved. Just discard the
        # current `p` (likely a partial output from the previous
        # failed build).
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass
        return backup

    p.rename(backup)
    return backup


def _extract_license(
    meta: importlib.metadata.PackageMetadata,
) -> tuple[str, bool] | None:
    """Pull a license string out of PEP 566 metadata.

    Returns (value, is_spdx) or None. `is_spdx` is True only for the
    `License-Expression` field (PEP 639), which is contractually an
    SPDX expression. The legacy free-text `License:` field and Trove
    classifier names are NOT valid SPDX identifiers (e.g. classifiers
    yield "MIT License", but SPDX uses "MIT") — they belong in
    CycloneDX `license.name`, not `license.id`.
    """
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip(), True
    lic = meta.get("License")
    if lic and lic.strip().upper() not in {"", "UNKNOWN"}:
        return lic.strip(), False
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            return classifier.split("::")[-1].strip(), False
    return None


def _extract_homepage(meta: importlib.metadata.PackageMetadata) -> str | None:
    home = meta.get("Home-page")
    if home and home.strip():
        return home.strip()
    # PEP 621 stores URLs as `Project-URL: Label, https://...`
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in {"homepage", "home"}:
            return url.strip()
    return None


def generate_sbom(dist_root: Path) -> Path:
    """Write a CycloneDX 1.5 JSON SBOM of the project's runtime dependency
    closure (declared deps + their transitive requirements).

    Versions/licenses are resolved against the current Python
    environment's installed metadata. Packages not in the declared
    closure (pyinstaller, pytest, dev tooling, etc.) are excluded so
    the SBOM is reproducible across machines and accurately reflects
    the supply chain a downstream scanner cares about — fixing the
    F4 regression flagged in 

    CycloneDX 1.5 is chosen over 1.4 because 1.5 finalised the
    `metadata.licenses[].name` shape we use for the PolyForm license,
    which has no SPDX id.
    """
    app_dir = dist_root / DIST_APP_DIRNAME
    if not app_dir.exists():
        print(
            f"[sbom] skipping: {app_dir} does not exist (PyInstaller "
            "produced output at a different path?)",
            file=sys.stderr,
        )
        return app_dir / "SBOM.json"

    closure = _project_runtime_closure()
    if closure is None:
        print(
            "[sbom] warning: could not determine runtime closure from "
            "pyproject.toml; falling back to enumerating the full "
            "interpreter environment (SBOM will include dev tools).",
            file=sys.stderr,
        )

    components: list[dict] = []
    for dist in importlib.metadata.distributions():
        try:
            meta = dist.metadata
            name = meta["Name"]
            version = meta["Version"]
        except Exception as exc:  # noqa: BLE001 — metadata can be malformed
            print(f"[sbom] skipping unreadable distribution: {exc}", file=sys.stderr)
            continue
        if not name or not version:
            continue
        if closure is not None and _canon(name) not in closure:
            continue

        component: dict = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{_canon(name)}@{version}",
        }
        lic = _extract_license(meta)
        if lic:
            value, is_spdx = lic
            license_field = "id" if is_spdx else "name"
            component["licenses"] = [{"license": {license_field: value}}]
        homepage = _extract_homepage(meta)
        if homepage:
            component["externalReferences"] = [
                {"type": "website", "url": homepage}
            ]
        components.append(component)

    components.sort(key=lambda c: (c["name"].lower(), c["version"]))

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "Mynah",
            },
            "licenses": [{"license": {"name": "PolyForm-Noncommercial-1.0.0"}}],
        },
        "components": components,
    }

    out = app_dir / "SBOM.json"
    out.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote SBOM.json with {len(components)} components")
    return out


def generate_dll_manifest(dist_root: Path) -> Path:
    """Hash every bundled native binary and write a stable JSON manifest."""
    app_dir = dist_root / DIST_APP_DIRNAME
    if not app_dir.exists():
        print(
            f"[dll-manifest] skipping: {app_dir} does not exist",
            file=sys.stderr,
        )
        return app_dir / "DLL_MANIFEST.json"

    # PyInstaller's onedir layout puts bundled binaries under `_internal/`
    # since v6. Fall back to the app dir itself for older layouts.
    scan_root = app_dir / "_internal"
    if not scan_root.exists():
        scan_root = app_dir

    suffixes = {".dll", ".pyd", ".exe"}
    entries: list[dict] = []
    for path in scan_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"[dll-manifest] cannot read {path}: {exc}", file=sys.stderr)
            continue
        entries.append({
            "filename": path.name,
            "relative_path": path.relative_to(app_dir).as_posix(),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    entries.sort(key=lambda e: (e["filename"].lower(), e["relative_path"]))

    manifest = {
        "scan_root": scan_root.relative_to(app_dir).as_posix() or ".",
        "entries": entries,
    }

    out = app_dir / "DLL_MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote DLL_MANIFEST.json with {len(entries)} entries")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--sbom-only",
        action="store_true",
        help="Regenerate SBOM.json against the existing dist/ and exit.",
    )
    parser.add_argument(
        "--dll-manifest-only",
        action="store_true",
        help="Regenerate DLL_MANIFEST.json against the existing dist/ and exit.",
    )
    args = parser.parse_args()

    dist_root = ROOT / "dist"

    if args.sbom_only or args.dll_manifest_only:
        if args.sbom_only:
            generate_sbom(dist_root)
        if args.dll_manifest_only:
            generate_dll_manifest(dist_root)
        return 0

    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "PyInstaller is not installed in this Python.\n"
            "Install with: pip install \".[build]\"",
            file=sys.stderr,
        )
        return 2

    spec = ROOT / "Mynah.spec"
    if not spec.exists():
        print(f"Missing spec file: {spec}", file=sys.stderr)
        return 2

    # Stage previous outputs aside instead of deleting outright. If
    # PyInstaller fails partway through, the operator still has the
    # previous working dist/ to roll back to. _move_aside preserves
    # any existing .bak across repeated failures so a series of
    # broken builds can't accidentally destroy the last known-good
    # executable.
    backups: list[Path | None] = []
    for d in ("build", "dist"):
        backups.append(_move_aside(ROOT / d))

    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "Mynah.spec"]
    print(">", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=ROOT)

    if rc == 0:
        # Successful build — drop the stale backups now that we have a
        # known-good new dist/.
        for b in backups:
            if b is not None and b.exists():
                shutil.rmtree(b, ignore_errors=True)
        generate_sbom(dist_root)
        generate_dll_manifest(dist_root)
    else:
        print(
            "Build FAILED. Previous known-good outputs preserved as "
            "build.bak / dist.bak.",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
