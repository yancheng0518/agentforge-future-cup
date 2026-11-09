"""Read-only preservation check for the autonomous-chat integration.

Historical evidence files must retain their hashes. The legacy and prior
validation databases must retain exactly the original rows; the new chat API
database may append rows but may not rewrite or remove any original row.
No models, Docker, candidate imports, or historical writes are performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
APPEND_ONLY_DATABASE = ".agentforge/autonomous/agentforge.sqlite3"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Manifest path escapes project root")
    return path


def read_database(path):
    """Read a coherent SQLite snapshot, including live WAL commits."""
    result = {}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15) as database:
        database.execute("BEGIN")
        names = [row[0] for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = database.execute("SELECT * FROM " + quoted + " ORDER BY rowid")
            rows = cursor.fetchall()
            result[name] = {
                "columns": [column[0] for column in cursor.description],
                "rows": len(rows),
                "sha256": digest(json.dumps(rows, ensure_ascii=False).encode()),
                "row_hashes": Counter(digest(json.dumps(row, ensure_ascii=False).encode()) for row in rows),
            }
    return result


def check_preservation(root, manifest_path):
    root = root.resolve()
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    file_problems = []
    for relative, expected in before["files"].items():
        try:
            path = safe_path(root, relative)
            actual = digest(path.read_bytes())
            if actual != expected:
                file_problems.append({"path": relative, "reason": "sha256_changed"})
        except FileNotFoundError:
            file_problems.append({"path": relative, "reason": "missing"})
        except (OSError, ValueError):
            file_problems.append({"path": relative, "reason": "unreadable_or_outside_project"})

    reports = {}
    for relative, original in before["databases"].items():
        append_allowed = relative == APPEND_ONLY_DATABASE or original.get("allow_additions") is True
        report = {"policy": "original_rows_retained_additions_allowed" if append_allowed else "exact_original_rows",
                  "backup": original["backup"], "tables": {}, "passed": True}
        try:
            backup = read_database(safe_path(root, original["backup"]))
            current = read_database(safe_path(root, relative))
            if set(backup) != set(original["tables"]):
                raise ValueError("Backup tables differ from initial manifest")
            report["added_tables"] = sorted(set(current) - set(backup))
            report["missing_tables"] = sorted(set(backup) - set(current))
            if report["missing_tables"] or (report["added_tables"] and not append_allowed):
                report["passed"] = False
            for name, previous in backup.items():
                expected = original["tables"][name]
                backup_verified = (previous["rows"] == expected["rows"] and previous["sha256"] == expected["sha256"]
                                   and previous["row_hashes"] == Counter(expected["records_sha256"]))
                present = current.get(name)
                if present is None:
                    table_report = {"passed": False, "reason": "table_missing", "backup_verified": backup_verified}
                else:
                    missing_rows = sum((previous["row_hashes"] - present["row_hashes"]).values())
                    added_rows = sum((present["row_hashes"] - previous["row_hashes"]).values())
                    columns_equal = previous["columns"] == present["columns"]
                    exact = previous["sha256"] == present["sha256"]
                    passed = backup_verified and columns_equal and missing_rows == 0 and (append_allowed or exact)
                    table_report = {"passed": passed, "backup_verified": backup_verified, "columns_unchanged": columns_equal,
                                    "original_rows": previous["rows"], "current_rows": present["rows"],
                                    "missing_or_changed_original_rows": missing_rows, "additional_rows": added_rows,
                                    "exact_original_rows": exact}
                report["tables"][name] = table_report
                report["passed"] = report["passed"] and table_report["passed"]
        except (OSError, ValueError, sqlite3.Error, KeyError):
            # Fixed diagnostic only: never emit row contents or exception bodies.
            report.update(passed=False, error="Database or frozen backup could not be validated")
        reports[relative] = report

    return {"checked_at": datetime.now(timezone.utc).isoformat(),
            "manifest": manifest_path.relative_to(root).as_posix() if manifest_path.is_relative_to(root) else manifest_path.name,
            "checked_files": len(before["files"]), "file_problems": file_problems,
            "databases": reports, "read_only_inputs": True, "model_calls": 0, "docker_calls": 0,
            "passed": not file_problems and all(report["passed"] for report in reports.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(ROOT / "output/autonomous-chat-20261108/preservation-before.json"))
    parser.add_argument("--output", default=str(ROOT / "output/autonomous-chat-20261108/preservation-after.json"))
    args = parser.parse_args(argv)
    manifest = Path(args.manifest).resolve()
    output = Path(args.output).resolve()
    if not output.is_relative_to(ROOT / "output") or output == manifest or output.suffix != ".json":
        parser.error("Output must be a separate report under the project output directory")
    protected = json.loads(manifest.read_text(encoding="utf-8"))
    protected_paths = set(protected["files"]) | set(protected["databases"])
    protected_paths.update(database["backup"] for database in protected["databases"].values())
    if output.relative_to(ROOT).as_posix() in protected_paths:
        parser.error("Output would overwrite preserved historical evidence")
    if output.is_symlink() or output.with_name(output.name + ".tmp").is_symlink():
        parser.error("Report paths must not be symbolic links")
    result = check_preservation(ROOT, manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Only this derived report is refreshed; all historical inputs stay read-only.
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"passed": result["passed"], "checked_files": result["checked_files"],
                      "file_problems": len(result["file_problems"]),
                      "database_passed": {path: report["passed"] for path, report in result["databases"].items()},
                      "output": str(output)}, ensure_ascii=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
