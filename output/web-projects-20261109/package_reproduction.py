"""Package the completed Web batch into a NEW archive, only after quiescence.

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
import importlib.util
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_reproduction import (collect_files, checked_bytes, checked_path,
    configured_secrets, reject_secret, linked, ExportError,
    SCHEMA, MANIFEST, EXCLUDED_DIRS)
from archive_support import (verify_archive, scan_nested_zip, MAX_ENTRIES,
    MAX_TOTAL_BYTES, MAX_FILE_BYTES, MAX_MANIFEST_BYTES, LEGACY_ARCHIVE_MEMBERS, forbidden_member)

OUTDIR = ROOT / "output/web-projects-20261109"
ARCHIVE = ROOT / "output/agentforge-web-projects-20261109.zip"
SNAPSHOTS = OUTDIR / "package-database-snapshots"
RESTORE_GUIDE = "docs/web-projects-restore.md"
EVIDENCE_ROOTS = (
    ".agentforge/runs", ".agentforge/autonomous/runs",
    ".agentforge/autonomous-chat-posthoc-20261108",
    ".agentforge/collaboration-graph-checks-20261108",
    ".agentforge/projects", "output/project-workspaces-20261109",
    ".agentforge/web", ".agentforge/web-validation-20261109",
    ".agentforge/web-preview-platform-20261109", "output/web-preview-platform-20261109",
    "output/web-projects-20261109",
)
LIVE_DATABASES = {
    ".agentforge/agentforge.sqlite3": {"runs": 17},
    ".agentforge/autonomous/agentforge.sqlite3": {},
    ".agentforge/projects/projects.sqlite3": {},
    ".agentforge/projects/requests.sqlite3": {},
    ".agentforge/web/requests.sqlite3": {},
}
PREVIOUS_ARCHIVES = tuple("output/agentforge-" + name + "-20261108.zip"
                          for name in ("autonomous-core", "autonomous-chat", "collaboration-graph")) + (
                              "output/agentforge-project-workspaces-20261109.zip",)
PREVIOUS_MANIFESTS = {
    PREVIOUS_ARCHIVES[0]: ("output/autonomous-core-20261108/archive-manifest.json", None),
    PREVIOUS_ARCHIVES[1]: ("CHAT_REPRODUCTION_MANIFEST.json", "agentforge-autonomous-chat-reproduction-v1"),
    PREVIOUS_ARCHIVES[2]: ("REPRODUCTION_MANIFEST.json", "agentforge-local-reproduction-v1"),
    PREVIOUS_ARCHIVES[3]: ("REPRODUCTION_MANIFEST.json", "agentforge-local-reproduction-v1"),
}
DATABASE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
PRIVATE_NAME = re.compile(r"^(?:credentials?|secrets?|tokens?)(?:[._-]|$)|\.(?:pem|p12|pfx|key)$", re.I)
SQLITE_HEADER = b"SQLite format 3\x00"
ORIGINAL_CHECKED_BYTES = checked_bytes


def checked_bytes(root, path, secrets):
    return ORIGINAL_CHECKED_BYTES(root, path, secrets, MAX_FILE_BYTES)


def collect_source_files():
    selected, skipped, roots = collect_files(ROOT, [])
    attributes = checked_path(ROOT, ROOT / ".gitattributes")
    if not attributes.is_file():
        raise ExportError("The Web frozen-byte Git attributes file is missing.")
    selected[".gitattributes"] = (attributes, "source")
    return selected, skipped, roots


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
    if lowered.endswith((".vhd", ".vhdx", ".vmdk", ".iso", ".msi")):
        return "disk_or_installer"
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
                # Dependency build contexts are evidence, including frozen
                # Dockerfile, lock and wheels. They are never executed here.
                dependency_build = (name == "build" and parent.parent.name == "attempts"
                                    and parent.parent.parent.name == "dependencies")
                if (name.casefold() in EXCLUDED_DIRS | {"logs", "log", "wsl", ".wsl"} and not dependency_build) or path == SNAPSHOTS:
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


def verify_previous_archive(path, secrets):
    """Verify each sealed archive's ACTUAL historical manifest, not a new schema.

The core batch has an unversioned inventory inside output/, the chat batch has
its own named manifest and schema; graph and project-workspaces use the current
exporter's manifest convention. All four retain their complete original inventory.
"""
    path = checked_path(ROOT, path)
    relative = path.relative_to(ROOT).as_posix()
    if relative not in PREVIOUS_MANIFESTS:
        raise ExportError("No reviewed historical manifest contract exists for this archive.")
    manifest_name, expected_schema = PREVIOUS_MANIFESTS[relative]
    content = checked_bytes(ROOT, path, secrets)
    digest_file = checked_path(ROOT, Path(str(path) + ".sha256"))
    recorded = checked_bytes(ROOT, digest_file, secrets).decode("ascii").split()
    if len(recorded) != 2 or recorded[0] != sha(content) or recorded[1] != path.name:
        raise ExportError("A prior archive does not match its historical SHA-256 sidecar.")
    nested_scan = {"entries": 0, "bytes": 0}
    scan_nested_zip(content, secrets, nested_scan)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if manifest_name not in names or archive.getinfo(manifest_name).file_size > 2 * 1024 * 1024:
            raise ExportError("The declared historical member manifest is missing or oversized.")
        metadata = archive.read(manifest_name)
        manifest = json.loads(metadata)
        if not isinstance(manifest, dict) or manifest.get("schema") != expected_schema:
            raise ExportError("A historical manifest's original schema changed.")
        records = manifest.get("files")
        if not isinstance(records, dict) or set(names) != set(records) | {manifest_name}:
            raise ExportError("A historical archive differs from its complete original inventory.")
        for name, record in records.items():
            if not isinstance(record, dict):
                raise ExportError("A historical member hash record is malformed.")
            data = archive.read(name)
            if len(data) != record.get("bytes") or sha(data) != record.get("sha256"):
                raise ExportError("A historical member failed its original byte-count or SHA-256 check.")
    return {"verified": True, "verification_method": "original_member_manifest_and_sha256_sidecar",
        "original_manifest": manifest_name, "original_schema": expected_schema,
        "original_schema_note": "unversioned original inventory" if expected_schema is None else "original declared schema",
        "files": len(records), "zip_sha256": sha(content), "manifest_sha256": sha(metadata),
        "nested_zip_scan": nested_scan}


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


def load_live_ids():
    value = json.loads(checked_bytes(ROOT, OUTDIR / "live-ids.json", ()).decode("utf-8"))
    rows = value.get("runs")
    if value.get("schema") != "agentforge-web-live-ids-v1" or not isinstance(rows, list) or not rows:
        raise ExportError("The completed batch needs its explicit nonempty live-ids inventory.")
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"run_id", "data_dir", "kind"}
                or not re.fullmatch(r"[a-f0-9]{32}", row.get("run_id", ""))
                or row.get("data_dir") not in {".agentforge/autonomous", ".agentforge/web-validation-20261109"}
                or row.get("kind") not in {"natural", "seeded_defect"}
                or (row["data_dir"], row["run_id"]) in seen):
            raise ExportError("The live-ids inventory has an invalid, duplicate or unsupported run reference.")
        seen.add((row["data_dir"], row["run_id"]))
    return rows


def frozen_backup_references():
    result = {}
    for folder in (ROOT / "output/project-workspaces-20261109", OUTDIR):
        manifest = json.loads(checked_bytes(ROOT, folder / "preservation-before.json", ()).decode("utf-8"))
        for original, info in manifest["databases"].items():
            relative = info["backup"]
            path = checked_path(ROOT, ROOT / relative)
            if not path.is_relative_to(folder) or not path.is_file():
                raise ExportError("A historical database reference is missing or outside its evidence batch.")
            result[relative] = {"original_database": original, "expected_tables": info["tables"]}
    return result


def validate_preserved_files(secrets):
    manifest = json.loads(checked_bytes(ROOT, OUTDIR / "preservation-before.json", secrets))
    for name, expected in manifest["files"].items():
        if sha(checked_bytes(ROOT, checked_path(ROOT, ROOT / name), secrets)) != expected:
            raise ExportError("A pre-Web historical evidence file changed; the original preservation contract failed.")
    return len(manifest["files"])


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
    if relative == ".agentforge/projects/requests.sqlite3":
        if source.execute("SELECT COUNT(*) FROM requests WHERE completed=0").fetchone()[0]:
            raise ExportError("A project request/publication is still pending.")
        if source.execute("SELECT COUNT(*) FROM execution_requests WHERE response IS NULL").fetchone()[0]:
            raise ExportError("An application execution receipt is still pending.")
    if relative == ".agentforge/web/requests.sqlite3":
        for (body,) in source.execute("SELECT body FROM jobs"):
            if json.loads(body).get("status") not in {"succeeded", "failed", "interrupted"}:
                raise ExportError("A Web dependency job is still active or has an unknown outcome.")
        if source.execute("SELECT COUNT(*) FROM preview_requests WHERE preview_id IS NULL").fetchone()[0]:
            raise ExportError("A Web preview idempotency receipt is still pending.")
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
            role = "historical_snapshot" if relative in historical or relative.startswith("output/") else "restorable_database"
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


def implementation_sources(selected, secrets, live_ids):
    result = []
    for identity in live_ids:
        run_id = identity["run_id"]
        folder = f"{identity['data_dir']}/runs/{run_id}"
        manifest_member = folder + "/implementation-manifest.json"
        path, _ = selected[manifest_member]
        values = json.loads(checked_bytes(ROOT, path, secrets))
        for relative, expected in values.items():
            member = folder + "/implementation/" + relative
            source, _ = selected[member]
            if sha(checked_bytes(ROOT, source, secrets)) != expected:
                raise ExportError("A real-run implementation snapshot does not match its frozen manifest.")
        required = {"sandbox/web/runner.py", "sandbox/web/web_bridge.py", "sandbox/web/serve.py",
                    "sandbox/web/http_probe.py", "sandbox/web/download_wheels.py", "sandbox/web/Dockerfile"}
        if not required <= set(values):
            raise ExportError("A Web run implementation snapshot omitted its trusted sandbox/Web sources.")
        result.append({**identity, "manifest": manifest_member,
            "manifest_sha256": sha(checked_bytes(ROOT, path, secrets)),
            "implementation_root": folder + "/implementation", "files": len(values),
            "meaning": "Host implementation frozen for this actual run; candidate and acceptance are separate evidence members."})
    return result


def read_selected(selected, name, secrets=()):
    if name not in selected:
        raise ExportError("A required referenced evidence member was not selected.")
    return checked_bytes(ROOT, selected[name][0], secrets)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_web_artifacts(selected, secrets):
    """Validate frozen Web data and byte hashes, without importing its code."""
    dependencies, attempts, previews, sandboxes = {}, [], [], []
    dependency_root = ".agentforge/web/dependencies/"
    for name in sorted(selected):
        if name.startswith(dependency_root + "attempts/") and name.endswith("/record.json"):
            record = json.loads(read_selected(selected, name, secrets))
            if record.get("status") not in {"succeeded", "failed", "cancelled"} or not record.get("finished_at"):
                raise ExportError("An unfinished dependency attempt remains.")
            if record.get("downloader_sha256"):
                source = name.removesuffix("record.json") + "trusted/download_wheels.py"
                if sha(read_selected(selected, source, secrets)) != record["downloader_sha256"]:
                    raise ExportError("A dependency attempt downloader snapshot changed.")
            attempts.append({"attempt_id": record["attempt_id"], "status": record["status"], "record": name})
        if name.startswith(dependency_root + "bundles/") and name.endswith("/manifest.json"):
            record = json.loads(read_selected(selected, name, secrets))
            prefix = name.removesuffix("manifest.json")
            identity = sha(canonical({k: record[k] for k in ("schema", "requirements", "base_image_id")}))
            if (record.get("schema") != "agentforge-web-dependencies-v1" or record.get("dependency_id") != identity
                    or prefix.split("/")[-2] != identity
                    or any(not re.fullmatch(r"sha256:[a-f0-9]{64}", record.get(k, "")) for k in ("image_id", "base_image_id"))
                    or record.get("offline_install") is not True or record.get("host_installation") is not False
                    or record.get("candidate_source_mounted") is not False
                    or not set(record.get("observed_hosts", [])) <= {"pypi.org", "files.pythonhosted.org"}):
                raise ExportError("A dependency bundle lost its pinned official-source/runtime identity.")
            wheels = record["wheels"]
            if not isinstance(wheels, list) or not 1 <= len(wheels) <= 48:
                raise ExportError("A dependency bundle has no bounded wheel inventory.")
            for wheel in wheels:
                if not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", wheel["file"]):
                    raise ExportError("A dependency wheel filename is unsafe.")
                content = read_selected(selected, prefix + "wheels/" + wheel["file"], secrets)
                if sha(content) != wheel["sha256"] or len(content) != wheel["bytes"]:
                    raise ExportError("A dependency wheel does not match its frozen hash/size.")
            if len({w["file"] for w in wheels}) != len(wheels):
                raise ExportError("Duplicate wheel inventory entries.")
            lock = read_selected(selected, prefix + "requirements.lock", secrets)
            expected_lock = ("\n".join(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}" for w in wheels) + "\n").encode()
            if lock != expected_lock or sha(lock) != record["lock_sha256"]:
                raise ExportError("The dependency lock differs from its complete frozen wheel inventory.")
            attempt = dependency_root + "attempts/" + record["prepared_by_attempt"] + "/"
            for relative, key in (("build/Dockerfile", "dockerfile_sha256"), ("trusted/download_wheels.py", "downloader_sha256")):
                if sha(read_selected(selected, attempt + relative, secrets)) != record[key]:
                    raise ExportError("A prepared dependency runtime lost its actual builder/downloader snapshot.")
            dependencies[identity] = record
    attempt_roots = {str(PurePosixPath(name).parent) for name in selected
                     if name.startswith(dependency_root + "attempts/") and PurePosixPath(name).parent.parent.name == "attempts"}
    if any(prefix + "/record.json" not in selected for prefix in attempt_roots):
        raise ExportError("A dependency attempt has evidence but no final attempt record.")

    def files_under(prefix):
        return {name[len(prefix):]: read_selected(selected, name, secrets).decode("utf-8")
                for name in sorted(selected) if name.startswith(prefix)}

    for name in sorted(selected):
        if name.endswith("/autonomous_manifest.json"):
            manifest = json.loads(read_selected(selected, name, secrets))
            prefix = name.removesuffix("autonomous_manifest.json")
            bundle = json.loads(read_selected(selected, prefix + "bundle.json", secrets))
            if (sha(canonical(bundle)) != manifest["bundle_sha256"]
                    or sha(canonical(bundle["files"])) != manifest["baseline_sha256"]
                    or sha(read_selected(selected, prefix + "trusted/test_acceptance.py", secrets).decode("utf-8").replace("\r\n", "\n").encode()) != manifest["acceptance_sha256"]):
                raise ExportError("A frozen sandbox source/acceptance/bundle hash changed.")
            for filename, expected in manifest["runner_sha256"].items():
                text = read_selected(selected, prefix + "runner/" + filename, secrets).decode("utf-8").replace("\r\n", "\n")
                if sha(text.encode()) != expected:
                    raise ExportError("A frozen acceptance runner source hash changed.")
            web = bundle.get("provenance", {}).get("web")
            if web:
                validate_web_binding(web, dependencies)
                if manifest.get("web") != web or manifest.get("web_runtime_sha256") != sha(canonical(web)):
                    raise ExportError("A Web sandbox lost its frozen runtime binding.")
                sandboxes.append({"manifest": name, "web_runtime_sha256": sha(canonical(web))})
    preview_roots = {name.split("/events/", 1)[0] for name in selected if "/previews/" in name and "/events/" in name}
    for prefix in sorted(preview_roots):
        names = sorted(name for name in selected if name.startswith(prefix + "/events/") and name.endswith(".json"))
        final = json.loads(read_selected(selected, names[-1], secrets))["record"]
        if final.get("status") not in {"stopped", "expired", "failed", "interrupted"}:
            raise ExportError("An active preview remains; stop it before freezing evidence.")
        if final.get("container_invoked") is True and final.get("container_removed") is not True:
            raise ExportError("A launched preview lacks its completed cleanup receipt.")
        lease = final.get("wsl_lease")
        if lease and (lease.get("pid") or lease.get("status") != "start_failed") and (
                lease.get("status") != "released" or lease.get("client_reaped") is not True):
            raise ExportError("A preview WSL lease lacks its completed release/reap receipt.")
        files = files_under(prefix + "/candidate/")
        source_matches = sha(canonical(files)) == final.get("source_sha256")
        if final.get("container_invoked") is True and not source_matches:
            raise ExportError("A launched preview source differs from its frozen source hash.")
        for filename, expected in final.get("runner_sha256", {}).items():
            if sha(read_selected(selected, prefix + "/runner/" + filename, secrets)) != expected:
                raise ExportError("A preview's actual runner source changed.")
        previews.append({"id": final["id"], "last_event": names[-1], "status": final["status"],
                         "bindings": {k: final.get(k) for k in ("source_sha256", "image_id", "entrypoint", "owner")},
                         "source_hash_verified": source_matches, "container_removed": final.get("container_removed"),
                         "startup_failure_before_source_freeze": not source_matches and not final.get("container_invoked")})
    return {"dependencies": dependencies, "attempts": attempts, "previews": previews, "web_sandboxes": sandboxes}


def validate_web_binding(web, dependencies):
    required = {"schema", "entrypoint", "dependency_id", "image_id", "lock_sha256", "requirements"}
    dependency = dependencies.get(web.get("dependency_id"), {})
    if (set(web) != required or web.get("schema") != "python-web-v1"
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_/]*\.py", web.get("entrypoint", ""))
            or not dependency or any(web[k] != dependency[k] for k in ("dependency_id", "image_id", "lock_sha256", "requirements"))):
        raise ExportError("A project/run Web runtime differs from its actual dependency bundle.")


def validate_batch_runs(snapshots, live_ids, artifacts):
    by_source = {item["source_database"]: item for item in snapshots}
    before = json.loads(checked_bytes(ROOT, OUTDIR / "preservation-before.json", ()))
    def rows(relative):
        snapshot = by_source[relative]
        with closing(sqlite3.connect((ROOT / snapshot["backup_member"]).as_uri() + "?mode=ro", uri=True)) as db:
            columns = [r[1] for r in db.execute('PRAGMA table_info("runs")')]
            if "body" in columns:
                return {v["id"]: v for v in (json.loads(r[0]) for r in db.execute("SELECT body FROM runs"))}
            db.row_factory = sqlite3.Row
            return {r["id"]: dict(r) for r in db.execute("SELECT * FROM runs")}
    old_ui, current_ui, by_data = {}, {}, {}
    for relative in (".agentforge/agentforge.sqlite3", ".agentforge/autonomous/agentforge.sqlite3"):
        baseline = rows(before["databases"][relative]["backup"])
        current = rows(relative)
        if any(current.get(key) != value for key, value in baseline.items()):
            raise ExportError("An old UI run was removed or changed.")
        old_ui.update(baseline)
        current_ui.update(current)
        by_data[str(PurePosixPath(relative).parent)] = current
    if len(old_ui) != 25:
        raise ExportError("The preserved pre-Web UI inventory no longer contains its original 25 runs.")
    declared = {r["run_id"] for r in live_ids if r["data_dir"] == ".agentforge/autonomous"}
    if set(current_ui) - set(old_ui) != declared:
        raise ExportError("The live-ids inventory omits or invents a newly recorded UI run.")
    additional = ".agentforge/web-validation-20261109"
    if additional + "/agentforge.sqlite3" in by_source:
        by_data[additional] = rows(additional + "/agentforge.sqlite3")
        if set(by_data[additional]) != {r["run_id"] for r in live_ids if r["data_dir"] == additional}:
            raise ExportError("The live-ids inventory omitted a standalone Web Store run, including a failed attempt.")
    results = []
    for identity in live_ids:
        data_dir = identity["data_dir"]
        if data_dir not in by_data:
            by_data[data_dir] = rows(data_dir + "/agentforge.sqlite3")
        run = by_data[data_dir].get(identity["run_id"])
        if not run or run.get("status") in {"running", "queued"}:
            raise ExportError("A declared Web run is missing or not terminal.")
        provenance = (run.get("bundle") or {}).get("provenance", {})
        if (provenance.get("kind") == "seeded_defect") != (identity["kind"] == "seeded_defect"):
            raise ExportError("A declared run's natural/seeded provenance differs from retained evidence.")
        web = (run.get("project_context") or {}).get("web") or provenance.get("web")
        if not web:
            raise ExportError("A declared real Web run has no frozen Web runtime.")
        validate_web_binding(web, artifacts["dependencies"])
        results.append({**identity, "status": run["status"], "web_runtime_sha256": sha(canonical(web)),
                        "publication": run.get("publication"), "model_metrics": run.get("model_metrics")})
    # Import only the maintained audit script, never archived/candidate modules.
    spec = importlib.util.spec_from_file_location("web_package_audit", ROOT / "scripts/audit_autonomous.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    audits = []
    for data_dir in sorted({r["data_dir"] for r in live_ids}):
        report = audit.audit(ROOT / data_dir)
        if not report["passed"]:
            raise ExportError("Read-only source/runtime/implementation audit failed; retain and inspect its separate evidence.")
        audits.append({"data_dir": data_dir, "passed": True, "aggregate": report["aggregate"], "issues": report["issues"]})
    return {"preserved_ui_runs": len(old_ui), "ui_history_runs": len(current_ui),
            "legacy_runs": len(by_data[".agentforge"]), "dynamic_runs": len(by_data[".agentforge/autonomous"]),
            "new_runs": results, "read_only_audits": audits}


def validate_web_registry(snapshots, selected, artifacts, secrets):
    by_source = {item["source_database"]: item for item in snapshots}
    def connection(relative):
        return sqlite3.connect((ROOT / by_source[relative]["backup_member"]).as_uri() + "?mode=ro", uri=True)
    with closing(connection(".agentforge/projects/projects.sqlite3")) as db:
        revisions = {v["id"]: v for v in (json.loads(r[0]) for r in db.execute("SELECT body FROM revisions"))}
    for revision in revisions.values():
        if revision.get("web"):
            validate_web_binding(revision["web"], artifacts["dependencies"])
    previews = {item["id"]: item for item in artifacts["previews"]}
    unknown = []
    with closing(connection(".agentforge/web/requests.sqlite3")) as db:
        if {r[0] for r in db.execute("SELECT key FROM preview_requests")} != {r[0] for r in db.execute("SELECT key FROM preview_outcomes")}:
            raise ExportError("Preview requests and immutable outcomes have an incomplete or orphaned inventory.")
        for key, payload, identity in db.execute("SELECT key,payload,preview_id FROM preview_requests"):
            row = db.execute("SELECT identity,body FROM preview_outcomes WHERE key=?", (key,)).fetchone()
            if not row or row[0] != identity:
                raise ExportError("A preview idempotency request lost its immutable outcome.")
            record = json.loads(row[1])
            request = json.loads(payload)
            revision = revisions.get(request.get("revision_id"), {})
            if revision.get("project_id") != request.get("project_id"):
                raise ExportError("A preview request does not identify its frozen project revision.")
            owner = record.get("owner", {})
            if (record.get("id") != identity or owner.get("kind") != "project"
                    or any(owner.get(k) != request[k] for k in ("project_id", "revision_id"))):
                raise ExportError("A preview outcome owner/identity differs from its idempotency request.")
            name = ".agentforge/web/preview-request-evidence/" + sha(key.encode())[:32] + "/outcome.json"
            if json.loads(read_selected(selected, name, secrets)) != record:
                raise ExportError("A preview registry outcome differs from its immutable evidence file.")
            if record.get("source_sha256") and record["source_sha256"] != revision["source_sha256"]:
                raise ExportError("A preview outcome is bound to another source revision.")
            if record.get("image_id") and record["image_id"] != revision["web"]["image_id"]:
                raise ExportError("A preview outcome is bound to another dependency image.")
            if record.get("entrypoint") and record["entrypoint"] != revision["web"]["entrypoint"]:
                raise ExportError("A preview outcome is bound to another entrypoint.")
            if record.get("container_invoked") is True or record.get("status") == "ready":
                if owner.get("web_runtime_sha256") != sha(canonical(revision["web"])) or owner.get("dependency_id") != revision["web"]["dependency_id"]:
                    raise ExportError("A launched preview lost its complete frozen runtime binding.")
            if identity in previews:
                if any(previews[identity]["bindings"].get(k) != record.get(k) for k in ("source_sha256", "image_id", "entrypoint", "owner")):
                    raise ExportError("A preview lifecycle differs from its immutable startup outcome bindings.")
            else:
                if record.get("status") not in {"failed", "interrupted"}:
                    raise ExportError("A once-ready preview lost its manager lifecycle evidence.")
                if record.get("container_invoked") is not False:
                    unknown.append(identity)
        for identity, body in db.execute("SELECT id,body FROM jobs"):
            record = json.loads(body)
            if record.get("id") != identity:
                raise ExportError("A dependency job body has another registry identity.")
            name = f".agentforge/web/job-evidence/{identity}/{record['status']}.json"
            if json.loads(read_selected(selected, name, secrets)) != record:
                raise ExportError("A dependency job registry differs from its terminal evidence file.")
            if record["status"] == "succeeded" and artifacts["dependencies"].get(record.get("result", {}).get("dependency_id")) != record.get("result"):
                raise ExportError("A successful dependency job differs from its frozen bundle.")
    return {"frozen_web_revisions": True, "dependency_job_receipts": True, "preview_outcomes": True,
            "unknown_prelaunch_completion_retained": unknown, "new_container_inspection_performed": False}


def platform_implementation_copies(selected, secrets):
    copies = {name: sha(read_selected(selected, name, secrets)) for name in selected
              if name.startswith("output/web-projects-20261109/") and "implementation" in PurePosixPath(name).name
              and name.endswith(".py.txt")}
    result = []
    for name in sorted(selected):
        if not name.startswith("output/web-projects-20261109/") or not name.endswith(".intent.json"):
            continue
        declaration = json.loads(read_selected(selected, name, secrets))
        if declaration.get("classification") != "platform_preview_idle_regression":
            continue
        expected = declaration.get("implementation_sha256")
        matching = [member for member, fingerprint in copies.items() if fingerprint == expected]
        if not matching:
            raise ExportError("An idle-preview validation has no matching frozen host implementation copy.")
        result.append({"intent": name, "implementation_sha256": expected, "frozen_copies": matching,
                       "compared_to_packaging_time_source": False})
    return result


def ensure_inventory_unchanged(initial_names, initial_databases):
    current, _, _ = collect_source_files()
    databases, _ = collect_evidence(current)
    if set(current) != initial_names or set(databases) != initial_databases:
        raise ExportError("Workspace evidence inventory changed during packaging; partial outputs remain unverified.")


def main():
    report_path = OUTDIR / "archive-manifest.json"
    sidecar = ARCHIVE.with_suffix(".zip.sha256")
    for path in (ARCHIVE, sidecar, report_path, SNAPSHOTS):
        checked_path(ROOT, path)
        if path.exists() or linked(path):
            raise ExportError("An output already exists; this script never overwrites or deletes prior outputs.")
    secrets = configured_secrets(ROOT)
    live_ids = load_live_ids()
    preserved_files = validate_preserved_files(secrets)
    selected, skipped_source, _ = collect_source_files()
    databases, skipped_evidence = collect_evidence(selected)
    initial_names, initial_databases = set(selected), set(databases)
    historical = frozen_backup_references()
    if not set(historical) <= set(databases):
        raise ExportError("A referenced pre-change backup was omitted from the inventory.")
    if RESTORE_GUIDE not in selected:
        raise ExportError("Restoration instructions are missing.")
    previous = []
    for relative in PREVIOUS_ARCHIVES:
        path = checked_path(ROOT, ROOT / relative)
        content = checked_bytes(ROOT, path, secrets)
        verified = verify_previous_archive(path, secrets)
        digest_file = checked_path(ROOT, Path(str(path) + ".sha256"))
        if checked_bytes(ROOT, digest_file, secrets).decode("ascii").split()[0] != sha(content):
            raise ExportError("A prior archive does not match its historical SHA-256 sidecar.")
        selected[relative] = (path, "unchanged_prior_archive")
        selected[relative + ".sha256"] = (digest_file, "unchanged_prior_archive_hash")
        previous.append({"archive": relative, **verified, "bytes": len(content), "unchanged": True})
    implementations = implementation_sources(selected, secrets, live_ids)
    platform_implementations = platform_implementation_copies(selected, secrets)
    artifacts = validate_web_artifacts(selected, secrets)
    snapshots = snapshot_databases(databases, historical, selected)
    reference_checks = validate_snapshot_references(snapshots, selected)
    batch = validate_batch_runs(snapshots, live_ids, artifacts)
    web_registry = validate_web_registry(snapshots, selected, artifacts, secrets)
    project_counts = next(item["row_counts"] for item in snapshots if item["source_database"] == ".agentforge/projects/projects.sqlite3")
    if len(selected) + 1 > MAX_ENTRIES or len({name.casefold() for name in selected}) != len(selected):
        raise ExportError("Member count or case-insensitive path collision limit exceeded.")
    if MANIFEST in selected:
        raise ExportError("Generated manifest conflicts with a selected member.")
    records, total, zip_budget = {}, 0, {"entries": 0, "bytes": 0}
    for name, (path, kind) in sorted(selected.items()):
        reject_secret(name.encode("utf-8"), secrets)
        if forbidden_member(PurePosixPath(name).parts):
            raise ExportError("A selected member has an excluded credential/cache/disk path.")
        content = checked_bytes(ROOT, path, secrets)
        if name.casefold().endswith((".zip", ".whl")) or zipfile.is_zipfile(io.BytesIO(content)):
            scan_nested_zip(content, secrets, zip_budget, wheel=name.casefold().endswith(".whl"))
        records[name] = {"sha256": sha(content), "bytes": len(content), "kind": kind}
        total += len(content)
    manifest = {"schema": SCHEMA, "purpose": "agentforge-web-projects-reproduction-v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "files": records,
        "selected_evidence_roots": list(EVIDENCE_ROOTS), "database_snapshots": snapshots,
        **batch, "declared_live_ids": live_ids, "web_artifacts": artifacts,
        "pre_web_files_verified_unchanged": preserved_files,
        "projects": project_counts["projects"], "revisions": project_counts["revisions"], "previous_archives": previous,
        "actual_implementation_snapshots": implementations,
        "platform_implementation_snapshots": platform_implementations,
        "cross_database_reference_checks": reference_checks, "web_registry_checks": web_registry,
        "current_source": "Source members are packaging-time working tree bytes; per-run implementation manifests preserve the code used by each real run.",
        "excluded_counts": {"source": skipped_source, "evidence": skipped_evidence},
        "nested_zip_scan": zip_budget, "model_calls": 0, "docker_calls": 0,
        "sealed_legacy_archive_exceptions": LEGACY_ARCHIVE_MEMBERS,
        "candidate_execution": False, "live_databases_modified": False, "uploaded": False,
        "secret_scan": "Configured UTF-8/UTF-16 credential bytes rejected in all members and recursively decompressed ZIPs/wheels; unknown/encoded secrets are not claimed detected.",
        "restoration": "Extract only into a new empty directory. Restorable aliases and historical-only snapshots have separate roles.",
        "instructions": RESTORE_GUIDE}
    metadata = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    reject_secret(metadata, secrets)
    if len(metadata) > MAX_MANIFEST_BYTES or total + len(metadata) > MAX_TOTAL_BYTES:
        raise ExportError("Generated archive or manifest exceeds its bounded size limit.")
    ensure_database_inputs_unchanged(snapshots)
    ensure_inventory_unchanged(initial_names, initial_databases)
    validate_preserved_files(secrets)
    with zipfile.ZipFile(ARCHIVE, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (path, _) in sorted(selected.items()):
            content = checked_bytes(ROOT, path, secrets)
            if sha(content) != records[name]["sha256"] or len(content) != records[name]["bytes"]:
                raise ExportError("A selected file changed; partial archive is unverified and retained.")
            archive.writestr(name, content)
        archive.writestr(MANIFEST, metadata)
    ensure_database_inputs_unchanged(snapshots)
    ensure_inventory_unchanged(initial_names, initial_databases)
    for name, (path, _) in selected.items():
        if sha(checked_bytes(ROOT, path, secrets)) != records[name]["sha256"]:
            raise ExportError("A source/evidence member changed during packaging; the partial output is retained.")
    checked = verify_archive(ARCHIVE, metadata, secrets=secrets)
    with sidecar.open("x", encoding="ascii") as stream:
        stream.write(checked["zip_sha256"] + "  " + ARCHIVE.name + "\n")
    report = {**checked, "archive": ARCHIVE.relative_to(ROOT).as_posix(), "bytes": ARCHIVE.stat().st_size,
        "ui_history_runs": batch["ui_history_runs"], "preserved_ui_runs": batch["preserved_ui_runs"],
        "new_runs": batch["new_runs"], "database_snapshots": snapshots, "actual_implementation_snapshots": implementations,
        "source_databases_read_only": True, "nested_zip_scan": zip_budget}
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key not in {"database_snapshots", "actual_implementation_snapshots"}}, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except (ExportError, OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, zipfile.BadZipFile) as exc:
        # Never emit candidate text, credentials, SQL rows or exception bodies.
        print("ERROR: " + (str(exc) if isinstance(exc, ExportError) else type(exc).__name__) + "; archive is not certified; originals are unchanged.", file=sys.stderr)
        sys.exit(2)
