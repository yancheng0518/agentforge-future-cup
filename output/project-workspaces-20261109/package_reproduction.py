"""Package this completed project-workspace experiment into a NEW archive.

No candidate, model, Docker, installation or cleanup is executed. Run only after
all validation and documentation are final, with no ongoing workspace writes.
Every SQLite input is opened read-only and copied with SQLite backup, including
historical snapshots and per-run scratch databases. Live DB bytes/WAL are never
copied as archive members. Existing outputs and historical files are untouched.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from export_reproduction import (collect_files, checked_bytes, checked_path,
    configured_secrets, reject_secret, linked, verify_archive, ExportError,
    SCHEMA, MANIFEST, EXCLUDED_DIRS, MAX_ENTRIES, MAX_TOTAL_BYTES, MAX_FILE_BYTES)

OUTDIR = ROOT / "output/project-workspaces-20261109"
ARCHIVE = ROOT / "output/agentforge-project-workspaces-20261109.zip"
SNAPSHOTS = OUTDIR / "package-database-snapshots"
RESTORE_GUIDE = "docs/project-workspaces-restore.md"
EVIDENCE_ROOTS = (
    ".agentforge/runs", ".agentforge/autonomous/runs",
    ".agentforge/autonomous-chat-posthoc-20261108",
    ".agentforge/collaboration-graph-checks-20261108",
    ".agentforge/projects", "output/project-workspaces-20261109",
)
LIVE_DATABASES = {
    ".agentforge/agentforge.sqlite3": {"runs": 17},
    ".agentforge/autonomous/agentforge.sqlite3": {"runs": 8},
    ".agentforge/projects/projects.sqlite3": {},
    ".agentforge/projects/requests.sqlite3": {"requests": 2},
}
NEW_RUNS = ("ce012be5a56e452c86805463b3ba65e6", "c77677a111d641ebb743b7678d5546e4")
PREVIOUS_ARCHIVES = tuple("output/agentforge-" + name + "-20261108.zip"
                          for name in ("autonomous-core", "autonomous-chat", "collaboration-graph"))
DATABASE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
PRIVATE_NAME = re.compile(r"^(?:credentials?|secrets?|tokens?)(?:[._-]|$)|\.(?:pem|p12|pfx|key)$", re.I)
SQLITE_HEADER = b"SQLite format 3\x00"
# Frozen exceptions INSIDE two historical archives only. Never applied to new
# files or a current database. The enclosing archive digest and member digest
# must both match; these archives remain sealed, not merged during restoration.
LEGACY_ARCHIVE_MEMBERS = {
    "d760b5fe49c10425f53cf8529a20505412767731cc6fd66bc300d4279302b708": {
        ".agentforge/autonomous-validation-20261108/agentforge.sqlite3-shm": "fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb",
        ".agentforge/autonomous-validation-20261108/agentforge.sqlite3-wal": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    },
    "79beca3a73824807497c81295826ef6cf6cfb0327e711db368588bfe732d604e": {
        "output/collaboration-graph-20261108/frontend-final-build-20261108T141043278Z-d8e88f.log": "173aab8e81e3ee931cb4516fbf79590fa8105d084e5c7c36fa0bf74b14a9a230",
        "output/collaboration-graph-20261108/frontend-final-test-20261108T141043278Z-d8e88f.log": "d4a679753848df0b9064bca936185f11d30d06490c27e64f6d122ed3d6812305",
    },
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def excluded_name(name):
    lowered = name.casefold()
    if lowered == ".env" or lowered.startswith(".env.") and lowered != ".env.example":
        return "dotenv"
    if PRIVATE_NAME.search(name):
        return "credential_file"
    if lowered.endswith(("-wal", "-shm", "-journal", ".pyc", ".pyo", ".pyd")):
        return "database_sidecar_or_cache"
    if lowered.endswith(".log"):
        return "operational_log"
    return None


def collect_evidence(selected):
    skipped, databases = Counter(), set(LIVE_DATABASES)
    for relative in EVIDENCE_ROOTS:
        folder = checked_path(ROOT, ROOT / relative)
        if not folder.is_dir():
            raise ExportError("A required explicit evidence root is missing.")
        for directory, directories, filenames in os.walk(folder, followlinks=False):
            parent = Path(directory)
            for name in list(directories):
                path = checked_path(ROOT, parent / name)
                if name.casefold() in EXCLUDED_DIRS | {"logs", "log"} or path == SNAPSHOTS:
                    directories.remove(name)
                    skipped["excluded_directory"] += 1
            for name in sorted(filenames):
                path = checked_path(ROOT, parent / name)
                reason = excluded_name(name)
                if reason:
                    skipped[reason] += 1
                    continue
                if not path.is_file():
                    raise ExportError("Evidence must be an ordinary file.")
                relative_file = path.relative_to(ROOT).as_posix()
                with path.open("rb") as stream:
                    header = stream.read(16)
                if path.suffix.casefold() in DATABASE_SUFFIXES or header == SQLITE_HEADER:
                    if header != SQLITE_HEADER:
                        raise ExportError("A database-named input is not a SQLite database.")
                    databases.add(relative_file)
                else:
                    selected[relative_file] = (path, "evidence")
    # Source selection is shared with the established exporter; extend its
    # exclusions to named credential files and logs before reading any contents.
    for name in list(selected):
        reason = excluded_name(PurePosixPath(name).name)
        if reason:
            del selected[name]
            skipped[reason] += 1
    return sorted(databases), dict(skipped)


def scan_nested_zip(data, secrets, budget, depth=0):
    """Inspect compressed payloads too; never extract or execute ZIP members."""
    if depth > 8:
        raise ExportError("Nested archive depth exceeds the audit limit.")
    historical_members = LEGACY_ARCHIVE_MEMBERS.get(sha(data), {})
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set()
        for item in archive.infolist():
            name = item.filename
            parts = PurePosixPath(name).parts
            if (not name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
                    or ".." in parts or name.rstrip("/") != PurePosixPath(name).as_posix()
                    or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1
                    or name.casefold() in names or item.file_size > MAX_FILE_BYTES):
                raise ExportError("A nested archive has an unsafe or oversized member.")
            names.add(name.casefold())
            reject_secret(name.encode("utf-8"), secrets)
            if item.is_dir():
                continue
            exclusion = excluded_name(parts[-1])
            if exclusion and name not in historical_members:
                raise ExportError("A nested archive includes a forbidden credential/cache/sidecar path.")
            if any(part.casefold() in EXCLUDED_DIRS - {".agentforge"} for part in parts[:-1]):
                raise ExportError("A nested archive includes excluded cache/dependency contents.")
            budget["entries"] += 1
            budget["bytes"] += item.file_size
            if budget["entries"] > 100000 or budget["bytes"] > 1024 * 1024 * 1024:
                raise ExportError("Recursive archive inspection exceeds its bounded budget.")
            content = archive.read(item)
            reject_secret(content, secrets)
            if name in historical_members and sha(content) != historical_members[name]:
                raise ExportError("A sealed historical archive exception changed.")
            if name.casefold().endswith(".zip") or zipfile.is_zipfile(io.BytesIO(content)):
                scan_nested_zip(content, secrets, budget, depth + 1)


def table_metadata(path):
    result = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)) as source:
        source.execute("BEGIN")
        names = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            cursor = source.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')
            rows = cursor.fetchall()
            result[name] = {"columns": [item[0] for item in cursor.description], "rows": len(rows),
                            "sha256": sha(json.dumps(rows, ensure_ascii=False).encode())}
    return result


def frozen_backup_references():
    manifest = json.loads(checked_bytes(ROOT, OUTDIR / "preservation-before.json", ()).decode("utf-8"))
    result = {}
    for original, info in manifest["databases"].items():
        relative = info["backup"]
        path = checked_path(ROOT, ROOT / relative)
        if not path.is_relative_to(OUTDIR) or not path.is_file():
            raise ExportError("A historical database reference is missing or outside this evidence batch.")
        result[relative] = {"original_database": original, "expected_tables": info["tables"]}
    return result


def validate_database(source, relative):
    names = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {name: source.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}
    for table, expected in LIVE_DATABASES.get(relative, {}).items():
        if counts.get(table) != expected:
            raise ExportError("Expected run/project/revision count changed; review the packaging contract before retrying.")
    run_ids = []
    if "runs" in names:
        columns = [row[1] for row in source.execute('PRAGMA table_info("runs")')]
        if "body" in columns:
            runs = [json.loads(row[0]) for row in source.execute("SELECT body FROM runs ORDER BY rowid")]
        else:
            source.row_factory = sqlite3.Row
            runs = [dict(row) for row in source.execute("SELECT * FROM runs ORDER BY rowid")]
            source.row_factory = None
        if any(run.get("status") in ("queued", "running") for run in runs):
            raise ExportError("Active runs remain; finish them before packaging.")
        run_ids = [run["id"] for run in runs]
        if relative == ".agentforge/autonomous/agentforge.sqlite3":
            by_id = {run["id"]: run for run in runs}
            if any(by_id.get(run_id, {}).get("publication", {}).get("status") != "saved" for run_id in NEW_RUNS):
                raise ExportError("The two declared project runs lack saved revision publications.")
    if relative == ".agentforge/projects/requests.sqlite3":
        if source.execute("SELECT COUNT(*) FROM requests WHERE completed=0").fetchone()[0]:
            raise ExportError("A project request/publication is still pending.")
        if source.execute("SELECT COUNT(*) FROM execution_requests WHERE response IS NULL").fetchone()[0]:
            raise ExportError("An application execution receipt is still pending.")
    return counts, run_ids


def snapshot_databases(relatives, historical, selected):
    snapshots = []
    # All read transactions are acquired before copying. They remain open until
    # every backup is complete; a later table recheck rejects intervening writes.
    with ExitStack() as stack:
        sources = []
        for relative in relatives:
            path = checked_path(ROOT, ROOT / relative)
            source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
            stack.callback(source.close)
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            counts, run_ids = validate_database(source, relative)
            sources.append((relative, source, counts, run_ids))
        SNAPSHOTS.mkdir(exist_ok=False)
        for relative, source, counts, run_ids in sources:
            filename = sha(relative.encode())[:16] + "-" + Path(relative).name
            target_path = checked_path(ROOT, SNAPSHOTS / filename)
            with target_path.open("xb"):
                pass
            with closing(sqlite3.connect(target_path)) as target:
                source.backup(target)
                # SQLite backup inherits journal mode. Make only this NEW copy
                # standalone, avoiding restore dependence on backup WAL files.
                target.execute("PRAGMA journal_mode=DELETE")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ExportError("A new database snapshot failed integrity checking.")
            tables = table_metadata(target_path)
            if relative in historical:
                expected = historical[relative]["expected_tables"]
                if set(tables) != set(expected) or any(any(tables[name][key] != expected[name][key] for key in ("rows", "sha256"))
                        or "columns" in expected[name] and tables[name]["columns"] != expected[name]["columns"] for name in tables):
                    raise ExportError("A historical backup differs from its pre-change table manifest.")
            backup_member = target_path.relative_to(ROOT).as_posix()
            role = "historical_snapshot" if relative in historical else "restorable_database"
            selected[relative] = (target_path, role + "_alias")
            selected[backup_member] = (target_path, role + "_backup")
            snapshots.append({"source_database": relative, "source_read_only": True,
                "archive_member": relative, "backup_member": backup_member,
                "role": role, "original_database": historical.get(relative, {}).get("original_database"),
                "backup_sha256": sha(target_path.read_bytes()), "bytes": target_path.stat().st_size,
                "row_counts": counts, "run_ids": run_ids, "tables": tables})
    return snapshots


def ensure_database_inputs_unchanged(snapshots):
    for item in snapshots:
        if table_metadata(ROOT / item["source_database"]) != item["tables"]:
            raise ExportError("A database changed during packaging; partial artifacts are unverified and retained.")


def validate_snapshot_references(snapshots, selected):
    """Validate cross-database links using the already frozen copies only."""
    by_source = {item["source_database"]: item for item in snapshots}
    def open_copy(relative):
        return sqlite3.connect((ROOT / by_source[relative]["backup_member"]).as_uri() + "?mode=ro", uri=True)
    with closing(open_copy(".agentforge/autonomous/agentforge.sqlite3")) as db:
        runs = {value["id"]: value for value in (json.loads(row[0]) for row in db.execute("SELECT body FROM runs"))}
    with closing(open_copy(".agentforge/projects/projects.sqlite3")) as db:
        projects = {row[0]: row[1] for row in db.execute("SELECT id,head_revision_id FROM projects")}
        revisions = {value["id"]: value for value in (json.loads(row[0]) for row in db.execute("SELECT body FROM revisions"))}
        executions = {}
        for (body,) in db.execute("SELECT body FROM executions"):
            wrapper = json.loads(body)
            record = wrapper["record"]
            if record.get("project_id") != wrapper["project_id"] or record["id"] in executions:
                raise ExportError("A stored execution wrapper has an inconsistent or duplicate receipt identity.")
            executions[record["id"]] = record
        for project_id, head in projects.items():
            if revisions.get(head, {}).get("project_id") != project_id:
                raise ExportError("A project head does not identify its own frozen revision.")
        for revision in revisions.values():
            parent = revision.get("parent_id")
            if parent and (revisions.get(parent, {}).get("project_id") != revision["project_id"] or revisions[parent]["number"] + 1 != revision["number"]):
                raise ExportError("A frozen project revision chain is inconsistent.")
            expected = sha(json.dumps(revision["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
            if revision["source_sha256"] != expected:
                raise ExportError("A frozen project revision source hash is inconsistent.")
            if revision.get("run_id"):
                run = runs.get(revision["run_id"], {})
                if (run.get("project_id") != revision["project_id"] or run.get("base_revision_id") != parent
                        or run.get("publication", {}).get("revision_id") != revision["id"]
                        or run.get("verified_hash") != expected):
                    raise ExportError("Project publication and dynamic-run evidence disagree.")
        for project_id, body, fingerprint in db.execute("SELECT project_id,body,sha256 FROM application_states"):
            if project_id not in projects or sha(body.encode()) != fingerprint:
                raise ExportError("Application state does not match its authoritative frozen hash.")
        for record in executions.values():
            revision = revisions.get(record.get("revision_id"), {})
            if revision.get("project_id") != record.get("project_id") or revision.get("source_sha256") != record.get("source_sha256"):
                raise ExportError("A project execution does not identify the matching frozen source.")
    with closing(open_copy(".agentforge/projects/requests.sqlite3")) as db:
        for project_id, run_id in db.execute("SELECT project_id,run_id FROM requests"):
            if runs.get(run_id, {}).get("project_id") != project_id:
                raise ExportError("An idempotency request points to an unrelated or missing run.")
        for project_id, response in db.execute("SELECT project_id,response FROM execution_requests"):
            receipt = json.loads(response)
            saved = executions.get(receipt.get("id"), {})
            if saved.get("project_id") != project_id or saved.get("source_sha256") != receipt.get("source_sha256"):
                raise ExportError("An idempotency receipt points to an unrelated or missing execution.")
    for relative in by_source:
        if relative.endswith("/application-tools/scratch.sqlite3"):
            marker = str(PurePosixPath(relative).parent / "last-project.json")
            path, _ = selected[marker]
            project_id = json.loads(path.read_text(encoding="utf-8"))["project_id"]
            with closing(open_copy(relative)) as db:
                if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                    raise ExportError("A tool scratch marker points to a missing temporary project.")
    return {"project_heads": True, "revision_chain_and_hashes": True, "run_publications": True,
            "application_state_hashes": True, "execution_sources": True,
            "idempotency_references": True, "scratch_markers": True}


def implementation_sources(selected, secrets):
    result = []
    for run_id in NEW_RUNS:
        folder = f".agentforge/autonomous/runs/{run_id}"
        manifest_member = folder + "/implementation-manifest.json"
        path, _ = selected[manifest_member]
        values = json.loads(checked_bytes(ROOT, path, secrets))
        for relative, expected in values.items():
            member = folder + "/implementation/" + relative
            source, _ = selected[member]
            if sha(checked_bytes(ROOT, source, secrets)) != expected:
                raise ExportError("A real-run implementation snapshot does not match its frozen manifest.")
        result.append({"run_id": run_id, "manifest": manifest_member,
            "manifest_sha256": sha(checked_bytes(ROOT, path, secrets)),
            "implementation_root": folder + "/implementation", "files": len(values),
            "meaning": "Host implementation frozen for this actual run; candidate and acceptance are separate evidence members."})
    return result


def main():
    report_path = OUTDIR / "archive-manifest.json"
    sidecar = ARCHIVE.with_suffix(".zip.sha256")
    for path in (ARCHIVE, sidecar, report_path, SNAPSHOTS):
        checked_path(ROOT, path)
        if path.exists() or linked(path):
            raise ExportError("An output already exists; this script never overwrites or deletes prior outputs.")
    secrets = configured_secrets(ROOT)
    selected, skipped_source, _ = collect_files(ROOT, [])
    databases, skipped_evidence = collect_evidence(selected)
    historical = frozen_backup_references()
    if not set(historical) <= set(databases):
        raise ExportError("A referenced pre-change backup was omitted from the inventory.")
    if RESTORE_GUIDE not in selected:
        raise ExportError("Restoration instructions are missing.")
    previous = []
    for relative in PREVIOUS_ARCHIVES:
        path = checked_path(ROOT, ROOT / relative)
        content = checked_bytes(ROOT, path, secrets)
        verified = verify_archive(path)
        digest_file = checked_path(ROOT, Path(str(path) + ".sha256"))
        if checked_bytes(ROOT, digest_file, secrets).decode("ascii").split()[0] != sha(content):
            raise ExportError("A prior archive does not match its historical SHA-256 sidecar.")
        selected[relative] = (path, "unchanged_prior_archive")
        selected[relative + ".sha256"] = (digest_file, "unchanged_prior_archive_hash")
        previous.append({"archive": relative, **verified, "bytes": len(content), "unchanged": True})
    implementations = implementation_sources(selected, secrets)
    snapshots = snapshot_databases(databases, historical, selected)
    reference_checks = validate_snapshot_references(snapshots, selected)
    project_counts = next(item["row_counts"] for item in snapshots if item["source_database"] == ".agentforge/projects/projects.sqlite3")
    if len(selected) + 1 > MAX_ENTRIES or len({name.casefold() for name in selected}) != len(selected):
        raise ExportError("Member count or case-insensitive path collision limit exceeded.")
    if MANIFEST in selected:
        raise ExportError("Generated manifest conflicts with a selected member.")
    records, total, zip_budget = {}, 0, {"entries": 0, "bytes": 0}
    for name, (path, kind) in sorted(selected.items()):
        reject_secret(name.encode("utf-8"), secrets)
        content = checked_bytes(ROOT, path, secrets)
        if name.casefold().endswith(".zip") or zipfile.is_zipfile(io.BytesIO(content)):
            scan_nested_zip(content, secrets, zip_budget)
        records[name] = {"sha256": sha(content), "bytes": len(content), "kind": kind}
        total += len(content)
    manifest = {"schema": SCHEMA, "purpose": "agentforge-project-workspaces-reproduction-v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "files": records,
        "selected_evidence_roots": list(EVIDENCE_ROOTS), "database_snapshots": snapshots,
        "ui_history_runs": 25, "legacy_runs": 17, "dynamic_runs": 8, "project_runs": list(NEW_RUNS),
        "projects": project_counts["projects"], "revisions": project_counts["revisions"], "previous_archives": previous,
        "actual_implementation_snapshots": implementations,
        "cross_database_reference_checks": reference_checks,
        "current_source": "Source members are packaging-time working tree bytes; per-run implementation manifests preserve the code used by each real run.",
        "excluded_counts": {"source": skipped_source, "evidence": skipped_evidence},
        "nested_zip_scan": zip_budget, "model_calls": 0, "docker_calls": 0,
        "sealed_legacy_archive_exceptions": LEGACY_ARCHIVE_MEMBERS,
        "candidate_execution": False, "live_databases_modified": False, "uploaded": False,
        "secret_scan": "Configured UTF-8/UTF-16 credential bytes rejected in all members and recursively decompressed ZIPs; unknown/encoded secrets are not claimed detected.",
        "restoration": "Extract only into a new empty directory. Restorable aliases and historical-only snapshots have separate roles.",
        "instructions": RESTORE_GUIDE}
    metadata = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    reject_secret(metadata, secrets)
    if len(metadata) > 2 * 1024 * 1024 or total + len(metadata) > MAX_TOTAL_BYTES:
        raise ExportError("Generated archive or manifest exceeds its bounded size limit.")
    ensure_database_inputs_unchanged(snapshots)
    with zipfile.ZipFile(ARCHIVE, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (path, _) in sorted(selected.items()):
            content = checked_bytes(ROOT, path, secrets)
            if sha(content) != records[name]["sha256"] or len(content) != records[name]["bytes"]:
                raise ExportError("A selected file changed; partial archive is unverified and retained.")
            archive.writestr(name, content)
        archive.writestr(MANIFEST, metadata)
    ensure_database_inputs_unchanged(snapshots)
    checked = verify_archive(ARCHIVE, metadata)
    with sidecar.open("x", encoding="ascii") as stream:
        stream.write(checked["zip_sha256"] + "  " + ARCHIVE.name + "\n")
    report = {**checked, "archive": ARCHIVE.relative_to(ROOT).as_posix(), "bytes": ARCHIVE.stat().st_size,
        "ui_history_runs": 25, "database_snapshots": snapshots, "actual_implementation_snapshots": implementations,
        "source_databases_read_only": True, "nested_zip_scan": zip_budget}
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key not in {"database_snapshots", "actual_implementation_snapshots"}}, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except (ExportError, OSError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile) as exc:
        # Never emit candidate text, credentials, SQL rows or exception bodies.
        print("ERROR: " + (str(exc) if isinstance(exc, ExportError) else type(exc).__name__) + "; archive is not certified; originals are unchanged.", file=sys.stderr)
        sys.exit(2)
