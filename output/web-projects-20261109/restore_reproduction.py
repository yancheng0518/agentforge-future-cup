"""Verify this batch; optionally extract once into a NONEXISTENT directory.

No model, Docker, installation, application, candidate or archive Python module
is executed. Historical ZIPs and wheels remain sealed files. Partial restores
are retained on failure and can never be silently overwritten by another run.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_reproduction import ExportError, MANIFEST, checked_path, configured_secrets, linked
from archive_support import verify_archive, sha


def database_metadata(path):
    tables = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExportError("A restored database failed integrity checking.")
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            cursor = db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')
            rows = cursor.fetchall()
            tables[name] = {"columns": [c[0] for c in cursor.description], "rows": len(rows),
                            "sha256": sha(json.dumps(rows, ensure_ascii=False).encode())}
    return tables


def verify_database_aliases(root, manifest):
    counts = {"restorable_database": 0, "historical_snapshot": 0}
    for item in manifest.get("database_snapshots", []):
        if item.get("source_read_only") is not True or item.get("role") not in counts:
            raise ExportError("A restored database has an unsupported or unsafe snapshot role.")
        for key in ("archive_member", "backup_member"):
            path = checked_path(root, root / item[key])
            if sha(path.read_bytes()) != item["backup_sha256"] or database_metadata(path) != item["tables"]:
                raise ExportError("A restored database alias differs from its backup bytes or table inventory.")
        counts[item["role"]] += 1
    return counts


def restore(archive_path, destination=None, *, secrets=(), require_sidecar=True):
    archive_path = Path(archive_path).absolute()
    if any(linked(p) for p in (archive_path, *archive_path.parents)):
        raise ExportError("The input archive must not use links or junctions.")
    checked = verify_archive(archive_path, secrets=secrets)
    sidecar = archive_path.with_suffix(".zip.sha256")
    if require_sidecar:
        if not sidecar.is_file() or linked(sidecar) or sidecar.stat().st_size > 1024:
            raise ExportError("The bounded archive SHA-256 sidecar is missing or unsafe.")
        fields = sidecar.read_text(encoding="ascii").split()
        if fields != [checked["zip_sha256"], archive_path.name]:
            raise ExportError("The archive differs from its SHA-256 sidecar.")
    with zipfile.ZipFile(archive_path) as archive:
        metadata = archive.read(MANIFEST)
        if sha(metadata) != checked["manifest_sha256"]:
            raise ExportError("The archive manifest changed during verification.")
        manifest = json.loads(metadata)
        if manifest.get("purpose") != "agentforge-web-projects-reproduction-v1":
            raise ExportError("Only the new Web batch may be restored by this helper; keep old archives sealed.")
        report = {**checked, "extracted": False, "candidate_execution": False, "model_calls": 0,
                  "docker_calls": 0, "installations": 0, "historical_archives_extracted": False,
                  "ui_history_runs": manifest.get("ui_history_runs"), "new_runs": manifest.get("declared_live_ids", [])}
        if destination is None:
            return report
        root = Path(destination).absolute()
        if root.exists() or any(linked(p) for p in (root, *root.parents)) or not root.parent.is_dir():
            raise ExportError("Restoration requires a nonexistent directory under an existing ordinary parent.")
        root.mkdir(exist_ok=False)
        for name in archive.namelist():
            content = archive.read(name)
            expected = manifest["files"].get(name) if name != MANIFEST else {"sha256": sha(metadata), "bytes": len(metadata)}
            if not expected or sha(content) != expected["sha256"] or len(content) != expected["bytes"]:
                raise ExportError("An archive member changed before extraction; the partial restore is retained.")
            target = checked_path(root, root / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            checked_path(root, target)
            with target.open("xb") as stream:
                stream.write(content)
        with archive_path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checked["zip_sha256"]:
                raise ExportError("The input ZIP changed during restoration; the partial restore is retained.")
        tables = verify_database_aliases(root, manifest)
        return {**report, "extracted": True, "destination": str(root), "database_roles_verified": tables,
                "restored_files_verified": True, "new_machine_runtime_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--extract-to", help="A nonexistent destination. Omit to verify without extracting.")
    parser.add_argument("--report", help="Optional new JSON report path; never overwritten.")
    args = parser.parse_args(argv)
    report_path = Path(args.report).absolute() if args.report else None
    if report_path and (report_path.exists() or not report_path.parent.is_dir()
                        or any(linked(p) for p in (report_path, *report_path.parents))):
        raise ExportError("A restore report already exists or uses links; it cannot be overwritten.")
    result = restore(args.archive, args.extract_to, secrets=configured_secrets(ROOT))
    if report_path:
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExportError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print("ERROR: " + (str(exc) if isinstance(exc, ExportError) else type(exc).__name__)
              + "; input unchanged; partial output retained and unverified.", file=sys.stderr)
        raise SystemExit(2)
