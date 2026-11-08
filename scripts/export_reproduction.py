"""Export a local, verified reproduction ZIP without executing any candidate.

Evidence is opt-in. Original bytes are copied unchanged; a configured-secret
match rejects the export instead of silently rewriting evidence. No upload,
dependency install, model call, database checkpoint or cleanup is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "REPRODUCTION_MANIFEST.json"
README = "REPRODUCTION_README.md"
SCHEMA = "agentforge-local-reproduction-v1"
SOURCE_DIRS = ("backend", "frontend", "scripts", "tests", "acceptance", "knowledge",
               "examples", "benchmarks", "sandbox", "docs", ".github")
ROOT_FILES = (".gitignore", ".env.example", "pytest.ini", "pyproject.toml", "uv.lock",
              "requirements.txt", "requirements-lock.txt", "LICENSE", "LICENSE.txt", "LICENSE.md")
EVIDENCE_KINDS = {"evaluations", "seeded-workflows", "task-controls", "protocol-probes", "comparisons"}
EXCLUDED_DIRS = {".git", ".agentforge", ".venv", "venv", "node_modules", "dist", "build",
                 "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".npm-cache", ".cache"}
SOURCE_SUFFIXES = {".py", ".ps1", ".sh", ".md", ".txt", ".json", ".jsonl", ".toml", ".ini", ".cfg",
                   ".yml", ".yaml", ".ts", ".tsx", ".js", ".jsx", ".css", ".scss", ".html", ".svg",
                   ".png", ".webp", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".csv", ".lock"}
EVIDENCE_SUFFIXES = SOURCE_SUFFIXES | {".log"}
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 20000
SECRET_NAME = re.compile(r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)(?:$|_)", re.I)


class ExportError(Exception):
    """Safe explanation without configuration values or source contents."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def checked_path(root, path):
    """Reject links, junctions and paths outside this checkout before reads."""
    path = Path(os.path.abspath(path))
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise ExportError("Selected source/evidence must stay inside the workspace.") from None
    current = root
    for part in parts:
        current = current / part
        if linked(current):
            raise ExportError("Linked files or directories cannot be archived.")
    if not path.resolve().is_relative_to(root):
        raise ExportError("Selected source/evidence resolves outside the workspace.")
    return path


def configured_secrets(root):
    """Inspect both environment and dotenv values, including overridden keys."""
    values = {value for name, value in os.environ.items() if SECRET_NAME.search(name) and value}
    path = root / ".env"
    if path.exists():
        checked_path(root, path)
        if path.stat().st_size > 1024 * 1024:
            raise ExportError("Dotenv is unexpectedly large; refusing to scan it.")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            name, value = line.split("=", 1)
            name = name.strip().removeprefix("export ")
            if SECRET_NAME.search(name):
                value = value.strip().strip('"').strip("'")
                if value:
                    values.add(value)
    # Check the exact configured bytes, plus common text encodings. No value
    # or value hash is included in the archive or printed to the console.
    return tuple({encoded for value in values for encoded in
                  (value.encode("utf-8"), value.encode("utf-16-le"), value.encode("utf-16-be"))})


def reject_secret(data, secrets):
    if any(value in data for value in secrets):
        raise ExportError("Configured credential bytes were found; export refused without exposing or rewriting them.")


def ignored_file(path, evidence=False):
    name = path.name.lower()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "dotenv"
    if name.endswith(("-wal", "-shm", "-journal", ".sqlite", ".sqlite3", ".db")):
        return "database_or_sidecar"
    if name.endswith((".pyc", ".pyo", ".pyd")):
        return "compiled_cache"
    allowed = EVIDENCE_SUFFIXES if evidence else SOURCE_SUFFIXES
    if path.suffix.lower() not in allowed and path.name not in {"Dockerfile", ".gitignore", ".env.example", "LICENSE"}:
        return "unsupported_or_binary_artifact"
    return None


def collect_files(root, evidence_paths):
    root = root.resolve()
    selected, skipped, evidence_roots = {}, Counter(), []

    def add(path, kind):
        checked_path(root, path)
        if not path.is_file():
            raise ExportError("Only ordinary source and evidence files may be archived.")
        reason = ignored_file(path, kind == "evidence")
        if reason:
            skipped[reason] += 1
            return
        relative = path.relative_to(root).as_posix()
        if relative in {MANIFEST, README}:
            raise ExportError("Source conflicts with a generated archive metadata filename.")
        selected[relative] = (path, kind)
        if len(selected) > MAX_ENTRIES:
            raise ExportError("Archive file-count limit exceeded.")

    def walk(folder, kind):
        checked_path(root, folder)
        for path in sorted(folder.iterdir()):
            if path.name.lower() in EXCLUDED_DIRS:
                skipped["excluded_directory"] += 1
                continue
            if linked(path):
                raise ExportError("Linked files or directories cannot be archived.")
            if path.is_dir():
                walk(path, kind)
            else:
                add(path, kind)

    for path in sorted(root.glob("*.md")):
        add(path, "source")
    for name in ROOT_FILES:
        path = root / name
        if path.exists():
            add(path, "source")
    for name in SOURCE_DIRS:
        path = root / name
        if path.exists():
            if not path.is_dir():
                raise ExportError("Expected a project source directory.")
            walk(path, "source")
    for value in evidence_paths:
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        path = checked_path(root, path)
        parts = path.relative_to(root).parts
        if len(parts) < 3 or parts[0] != ".agentforge" or parts[1] not in EVIDENCE_KINDS or not path.is_dir():
            raise ExportError("Evidence must be an explicit existing directory under an allowed .agentforge evidence group.")
        relative = path.relative_to(root).as_posix()
        if relative not in evidence_roots:
            evidence_roots.append(relative)
            walk(path, "evidence")
    names = list(selected)
    if len({name.casefold() for name in names}) != len(names):
        raise ExportError("Archive paths collide on a case-insensitive filesystem.")
    return dict(sorted(selected.items())), dict(skipped), evidence_roots


def checked_bytes(root, path, secrets, max_file_bytes=MAX_FILE_BYTES):
    checked_path(root, path)
    if path.stat().st_size > max_file_bytes:
        raise ExportError("A selected source/evidence file exceeds the per-file size limit.")
    with path.open("rb") as handle:
        content = handle.read(max_file_bytes + 1)
    if len(content) > max_file_bytes:
        raise ExportError("A selected source/evidence file grew beyond the size limit.")
    reject_secret(content, secrets)
    return content


def archive_readme(evidence_roots):
    evidence = "\n".join("- " + path for path in evidence_roots) or "- None: source-only export."
    return ("# AgentForge local reproduction archive\n\n"
            "Paths preserve the workspace layout. Source and selected evidence bytes are copied unchanged. "
            "No model, candidate, installation or upload was performed by the exporter.\n\n"
            "Start with README.md and docs/multi-task-evaluation.md. Recreate the Python environment from "
            "backend/requirements-lock.txt and frontend dependencies from frontend/package-lock.json. "
            "Configure a new local .env from .env.example and prepare Docker separately. Model reruns may incur "
            "usage and are not guaranteed to produce the same output. Never run candidate files on the host.\n\n"
            "## Included evidence roots\n\n" + evidence + "\n\n"
            "Evidence JSON, frozen source and candidate files are historical observations, not executable "
            "setup instructions. Natural failures, model contract failures and declared synthetic defects "
            "must remain separate. The selected JSON exports contain the audit records; SQLite databases "
            "and their WAL/SHM/journals are excluded to avoid distributing an inconsistent live database.\n\n"
            "Excluded by default: .env credentials, unselected .agentforge data, WSL/disks/installers, "
            "node_modules, dist/build, virtual environments, caches, databases, archives and unsupported "
            "binary files. No browser screenshots or original slide deck are selected by default. "
            "Configured credential values are checked as exact bytes in UTF-8/UTF-16. This does not "
            "claim discovery of every unknown or encoded secret. Matching files cause refusal, not redaction.\n\n"
            "REPRODUCTION_MANIFEST.json lists SHA-256 and size for every other ZIP member, including this "
            "README; the manifest cannot include its own hash. The exporter rereads every member and "
            "prints the final ZIP and manifest SHA-256. Hashes detect changes, not authorship.\n\n"
            "Verify without extracting or executing content:\n\n"
            "```powershell\npython scripts/export_reproduction.py --verify path/to/archive.zip\n```\n").encode("utf-8")


def verify_archive(path, expected_manifest=None):
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) > MAX_ENTRIES + 2 or len({name.casefold() for name in names}) != len(names):
            raise ExportError("ZIP has duplicate names or too many members.")
        if sum(item.file_size for item in infos) > MAX_TOTAL_BYTES:
            raise ExportError("ZIP exceeds the total uncompressed size limit.")
        for item in infos:
            name = item.filename
            if (not name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts or name != PurePosixPath(name).as_posix()
                    or item.is_dir() or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1
                    or item.file_size > MAX_FILE_BYTES):
                raise ExportError("ZIP contains an unsafe or oversized member.")
        if MANIFEST not in names or archive.getinfo(MANIFEST).file_size > 2 * 1024 * 1024:
            raise ExportError("ZIP lacks a bounded reproduction manifest.")
        manifest_bytes = archive.read(MANIFEST)
        if expected_manifest is not None and manifest_bytes != expected_manifest:
            raise ExportError("Written manifest differs from the prepared manifest.")
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or not isinstance(manifest.get("files"), dict):
            raise ExportError("Unsupported reproduction manifest.")
        records = manifest["files"]
        if set(names) != set(records) | {MANIFEST}:
            raise ExportError("ZIP members differ from the complete manifest inventory.")
        for name, record in records.items():
            if not isinstance(record, dict):
                raise ExportError("Invalid file record in reproduction manifest.")
            content = archive.read(name)
            if len(content) != record.get("bytes") or sha256(content) != record.get("sha256"):
                raise ExportError("ZIP member failed its byte-count or SHA-256 integrity check.")
    with Path(path).open("rb") as handle:
        zip_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"verified": True, "files": len(records), "zip_sha256": zip_digest,
            "manifest_sha256": sha256(manifest_bytes)}


def export_archive(root, output, evidence_paths=(), secrets=None, max_file_bytes=MAX_FILE_BYTES):
    root, output = root.resolve(), output.absolute()
    if output.exists() or output.is_symlink():
        raise ExportError("Output already exists; an existing archive is never overwritten.")
    if output.suffix.lower() != ".zip":
        raise ExportError("Output must have a .zip suffix.")
    secrets = configured_secrets(root) if secrets is None else tuple(secrets)
    selected, skipped, evidence_roots = collect_files(root, evidence_paths)
    if not selected:
        raise ExportError("No project source files were selected.")
    records, total = {}, 0
    for name, (path, kind) in selected.items():
        reject_secret(name.encode("utf-8"), secrets)
        content = checked_bytes(root, path, secrets, max_file_bytes)
        total += len(content)
        if total > MAX_TOTAL_BYTES - 3 * 1024 * 1024:
            raise ExportError("Selected uncompressed content exceeds the archive budget.")
        records[name] = {"sha256": sha256(content), "bytes": len(content), "kind": kind}
    readme = archive_readme(evidence_roots)
    records[README] = {"sha256": sha256(readme), "bytes": len(readme), "kind": "generated"}
    manifest = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
                "selected_evidence_roots": evidence_roots, "files": records,
                "excluded_counts": skipped, "source_files_modified": False,
                "candidate_execution": False, "model_requests": 0, "uploaded": False,
                "secret_scan": "Configured credential bytes checked; matched files rejected, never rewritten.",
                "manifest_self_hash": "Excluded; final manifest and ZIP SHA-256 are returned by verification."}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(manifest_bytes) > 2 * 1024 * 1024:
        raise ExportError("Generated manifest exceeds its size limit.")
    reject_secret(readme + manifest_bytes, secrets)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects against another process creating the
    # name between the initial check and this open. On failure a partial ZIP
    # is retained; it is never reported verified or silently deleted/reused.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, (path, _) in selected.items():
            content = checked_bytes(root, path, secrets, max_file_bytes)
            if len(content) != records[name]["bytes"] or sha256(content) != records[name]["sha256"]:
                raise ExportError("A source/evidence file changed during export; partial ZIP is unverified and retained.")
            archive.writestr(name, content)
        archive.writestr(README, readme)
        archive.writestr(MANIFEST, manifest_bytes)
    result = verify_archive(output, manifest_bytes)
    return {**result, "output": str(output), "selected_evidence_roots": evidence_roots,
            "uncompressed_content_bytes": total + len(readme) + len(manifest_bytes)}


def self_test():
    """Only synthetic text fixtures; no workspace archive or model request."""
    checks = []
    with tempfile.TemporaryDirectory(prefix="agentforge-export-test-") as temporary:
        base = Path(temporary).resolve()
        if not base.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ExportError("Self-test temporary directory is outside the expected temporary root.")
        root = base / "project"
        files = {"README.md": "synthetic test project", ".env": "AGENTFORGE_LLM_API_KEY=fixture-secret-987654321",
                 "backend/app.py": "# inert fixture", "frontend/src/App.tsx": "// inert source",
                 "frontend/package-lock.json": "{}", "frontend/node_modules/omit.js": "ignored",
                 "frontend/dist/omit.js": "ignored", "backend/__pycache__/omit.pyc": "ignored",
                 ".agentforge/evaluations/demo/evidence.json": "{}",
                 ".agentforge/evaluations/demo/candidate.py.txt": "# inert candidate text, never executed",
                 ".agentforge/evaluations/demo/run.sqlite3-wal": "ignored",
                 ".agentforge/wsl/omit.vhdx": "ignored"}
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        secret_values = configured_secrets(root)
        assert b"fixture-secret-987654321" in secret_values
        output = base / "fixture.zip"
        export_archive(root, output, [Path(".agentforge/evaluations/demo")], secrets=secret_values)
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            assert ".env" not in names and not any("omit" in name or "-wal" in name for name in names)
            assert ".agentforge/evaluations/demo/candidate.py.txt" in names
            assert "frontend/package-lock.json" in names
        checks.append("explicit evidence inclusion and source/cache/credential exclusions")
        original_hash = sha256(output.read_bytes())
        try:
            export_archive(root, output, secrets=secret_values)
            raise AssertionError("overwrite was accepted")
        except ExportError:
            assert sha256(output.read_bytes()) == original_hash
        checks.append("exclusive output does not overwrite")
        (root / "backend/leak.txt").write_text("fixture-secret-987654321", encoding="utf-8")
        try:
            export_archive(root, base / "leak.zip", secrets=secret_values)
            raise AssertionError("secret leak was accepted")
        except ExportError:
            assert not (base / "leak.zip").exists()
        checks.append("configured credential rejected before archive creation")
        for invalid in (base, root / ".agentforge", root / ".agentforge/wsl"):
            try:
                collect_files(root, [invalid])
                raise AssertionError("unapproved evidence path was accepted")
            except ExportError:
                pass
        checks.append("evidence directory allowlist and workspace boundary")
        tampered = base / "tampered.zip"
        with zipfile.ZipFile(output) as source, zipfile.ZipFile(tampered, "x") as target:
            for name in source.namelist():
                target.writestr(name, b"changed" if name == "README.md" else source.read(name))
        try:
            verify_archive(tampered)
            raise AssertionError("changed member passed verification")
        except ExportError:
            pass
        checks.append("reread verification detects member tampering")
        try:
            export_archive(root, base / "oversized.zip", secrets=(), max_file_bytes=1)
            raise AssertionError("oversized source was accepted")
        except ExportError:
            assert not (base / "oversized.zip").exists()
        checks.append("selected-file size bound")
    return {"self_test_passed": True, "checks": checks, "real_workspace_exported": False,
            "candidate_execution": False, "model_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", action="append", default=[], type=Path,
                        help="Explicit allowed evidence directory; repeat for additional batches.")
    parser.add_argument("--output", type=Path, help="New ZIP path; existing paths are never overwritten.")
    parser.add_argument("--verify", type=Path, help="Verify an existing reproduction ZIP without extracting or executing it.")
    parser.add_argument("--self-test", action="store_true", help="Use only temporary synthetic fixtures; no project package.")
    args = parser.parse_args(argv)
    if (args.self_test or args.verify) and (args.evidence or args.output) or (args.self_test and args.verify):
        parser.error("self-test/verify cannot be combined with export arguments")
    try:
        if args.self_test:
            result = self_test()
        elif args.verify:
            result = verify_archive(args.verify)
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = args.output or ROOT / "artifacts/reproduction" / f"agentforge-{stamp}-{uuid4().hex[:8]}.zip"
            result = export_archive(ROOT, output, args.evidence)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ExportError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        print("ERROR: " + type(exc).__name__ + "; export/verification failed. No archive is certified; originals are unchanged.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
