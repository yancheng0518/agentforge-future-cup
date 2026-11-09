"""Build a new collaboration-graph reproduction archive; never overwrite history.

Run only after documentation and final evidence are ready. No model, Docker or
candidate execution occurs. Live databases are opened read-only and backed up
coherently; only the ZIP receives restorable database aliases.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from export_reproduction import (collect_files, checked_bytes, checked_path, configured_secrets,
                                 reject_secret, linked, verify_archive, SCHEMA, MANIFEST,
                                 EXCLUDED_DIRS, MAX_ENTRIES, MAX_TOTAL_BYTES)
from check_chat_preservation import read_database

OUTDIR = ROOT / "output/collaboration-graph-20261108"
ARCHIVE = ROOT / "output/agentforge-collaboration-graph-20261108.zip"
RESTORE_README = "COLLABORATION_GRAPH_RESTORE.md"
DATABASES = (
    (".agentforge/agentforge.sqlite3", "legacy-final.sqlite3", 17),
    (".agentforge/autonomous/agentforge.sqlite3", "autonomous-api-final.sqlite3", 6),
)
EVIDENCE_ROOTS = (
    ".agentforge/runs",
    ".agentforge/autonomous/runs",
    ".agentforge/autonomous-chat-posthoc-20261108",
    ".agentforge/collaboration-graph-checks-20261108",
    "output/collaboration-graph-20261108",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot_database(relative, filename, expected_runs):
    """Backup a coherent SQLite/WAL state; never checkpoint/write the source."""
    source_path = checked_path(ROOT, ROOT / relative)
    destination = checked_path(ROOT, OUTDIR / filename)
    if destination.exists():
        raise RuntimeError("Database snapshot already exists; it will not be overwritten")
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=15) as source:
        source.execute("BEGIN")
        runs = [json.loads(row[0]) for row in source.execute("SELECT body FROM runs ORDER BY rowid")]
        if len(runs) != expected_runs:
            raise RuntimeError("UI history changed; review the expected run count before packaging")
        if any(run.get("status") in ("queued", "running") for run in runs):
            raise RuntimeError("Wait for active runs to finish before taking a reproduction snapshot")
        with sqlite3.connect(destination) as target:
            source.backup(target)
    tables = read_database(destination)
    return destination, {
        "source_database": relative,
        "backup": destination.relative_to(ROOT).as_posix(),
        "restored_zip_member": relative,
        "backup_sha256": sha(destination.read_bytes()),
        "run_count": len(runs),
        "run_ids": [run["id"] for run in runs],
        "tables": {name: {"columns": table["columns"], "rows": table["rows"],
                           "sha256": table["sha256"], "records_sha256": dict(table["row_hashes"])}
                   for name, table in tables.items()},
    }


def collect_evidence(selected):
    skipped = Counter()
    for relative in EVIDENCE_ROOTS:
        folder = checked_path(ROOT, ROOT / relative)
        if not folder.is_dir():
            raise RuntimeError("A required evidence directory is missing")
        for directory, directories, filenames in os.walk(folder, followlinks=False):
            parent = Path(directory)
            for name in list(directories):
                path = parent / name
                if linked(path):
                    raise RuntimeError("Linked evidence directories cannot be archived")
                if name.lower() in EXCLUDED_DIRS:
                    directories.remove(name)
                    skipped["cache_directory"] += 1
            for name in sorted(filenames):
                path = checked_path(ROOT, parent / name)
                lowered = name.lower()
                if lowered == ".env" or (lowered.startswith(".env.") and lowered != ".env.example"):
                    skipped["dotenv"] += 1
                    continue
                if lowered.endswith(("-wal", "-shm", "-journal", ".pyc", ".pyo", ".pyd")):
                    skipped["cache_or_database_sidecar"] += 1
                    continue
                if not path.is_file():
                    raise RuntimeError("Evidence must be an ordinary file")
                selected[path.relative_to(ROOT).as_posix()] = (path, "evidence")
    return dict(skipped)


def restoration_readme():
    return """# 协作图复现包：只在全新目录恢复

本包包含当前源码、17 项旧工作台历史、6 项动态运行（含所有失败与否定结论）、
本轮真实执行及预声明独立 Docker 验收证据。不得将本包直接解压覆盖 D:\\未来杯，
也不要把包内数据库复制回正在使用的项目目录。

1. 创建一个从未用于 AgentForge 的全新空目录，将整个 ZIP 解压到该目录。
2. 包内 .agentforge/agentforge.sqlite3 和 .agentforge/autonomous/agentforge.sqlite3
   已是打包时通过 SQLite backup API 获得的一致性快照，分别包含 17 和 6 项历史；
   无需执行复制、覆盖、数据库清空或恢复脚本。原始备份另存于
   output/collaboration-graph-20261108/legacy-final.sqlite3 和 autonomous-api-final.sqlite3。
3. 在新目录按 README.md 使用锁定依赖重建虚拟环境和前端依赖，再从新目录启动服务。
   新目录内不含 .env、凭据、依赖缓存或 WSL 镜像。仅浏览历史不会发送新模型请求；
   如需重新运行，另按 .env.example 在新目录配置自己的凭据与 Docker。
4. 旧项目服务占用端口时，不要同时启动新目录服务到同一端口。阅读和核对 JSON/ZIP
   证据无需启动任何服务。不得在宿主机直接运行候选代码，执行验证只使用 Docker。

包内 REPRODUCTION_MANIFEST.json 记录每个成员的 SHA-256 和大小，以及两个数据库
快照的逐表/逐行摘要。可在不解压、不执行候选的情况下核验：

    python scripts/export_reproduction.py --verify <本ZIP的路径>

归档工具不发模型请求、不启动 Docker，不改当前数据库或历史文件。
历史“运行成功”并不自动代表业务正确，请同时查看 quality_review/posthoc_review：
原测试通过后被后验检查否定的记录仍完整保留。换机从零启动尚未在本轮实测。
""".encode("utf-8")


def main():
    report_path = OUTDIR / "archive-manifest.json"
    sidecar = ARCHIVE.with_suffix(".zip.sha256")
    checked_path(ROOT, OUTDIR)
    if any(path.exists() or linked(path) for path in (ARCHIVE, sidecar, report_path)):
        raise RuntimeError("An archive/report already exists; existing outputs are never overwritten")
    if any((OUTDIR / filename).exists() for _, filename, _ in DATABASES):
        raise RuntimeError("Final snapshots already exist; they will not be overwritten")
    selected, skipped_source, _ = collect_files(ROOT, [])
    snapshots = []
    for relative, filename, expected_runs in DATABASES:
        snapshot, details = snapshot_database(relative, filename, expected_runs)
        snapshots.append(details)
        # Only an archive alias, never a write to the current .agentforge database.
        selected[relative] = (snapshot, "restorable_database_snapshot")
    skipped_evidence = collect_evidence(selected)
    if len(selected) + 2 > MAX_ENTRIES:
        raise RuntimeError("Archive entry-count limit exceeded")
    if len({name.casefold() for name in selected}) != len(selected):
        raise RuntimeError("Archive paths collide on a case-insensitive filesystem")
    if MANIFEST in selected or RESTORE_README in selected:
        raise RuntimeError("Generated archive metadata path conflicts with a selected file")
    secrets = configured_secrets(ROOT)
    records = {}
    total = 0
    for name, (path, kind) in sorted(selected.items()):
        reject_secret(name.encode("utf-8"), secrets)
        data = checked_bytes(ROOT, path, secrets)
        records[name] = {"sha256": sha(data), "bytes": len(data), "kind": kind}
        total += len(data)
    readme = restoration_readme()
    reject_secret(readme, secrets)
    records[RESTORE_README] = {"sha256": sha(readme), "bytes": len(readme), "kind": "restoration_instructions"}
    if total + len(readme) > MAX_TOTAL_BYTES - 3 * 1024 * 1024:
        raise RuntimeError("Uncompressed archive size limit exceeded")
    manifest = {
        "schema": SCHEMA, "purpose": "agentforge-collaboration-graph-reproduction-v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "files": records,
        "selected_evidence_roots": list(EVIDENCE_ROOTS), "database_snapshots": snapshots,
        "ui_history_runs": sum(item["run_count"] for item in snapshots),
        "excluded_counts": {"source": skipped_source, "evidence": skipped_evidence},
        "model_calls": 0, "docker_calls": 0, "candidate_execution": False,
        "live_databases_modified": False,
        "secret_scan": "Exact configured credential bytes absent in UTF-8/UTF-16; dotenv excluded",
        "restoration": "Only extract into a new empty directory. Database aliases are coherent backups, not live file copies.",
        "instructions": RESTORE_README,
    }
    metadata = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    reject_secret(metadata, secrets)
    with zipfile.ZipFile(ARCHIVE, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (path, _) in sorted(selected.items()):
            data = checked_bytes(ROOT, path, secrets)
            if sha(data) != records[name]["sha256"]:
                raise RuntimeError("Selected content changed during packaging; partial archive retained")
            archive.writestr(name, data)
        archive.writestr(RESTORE_README, readme)
        archive.writestr(MANIFEST, metadata)
    checked = verify_archive(ARCHIVE, metadata)
    with sidecar.open("x", encoding="ascii") as stream:
        stream.write(checked["zip_sha256"] + "  " + ARCHIVE.name + "\n")
    report = {**checked, "bytes": ARCHIVE.stat().st_size,
              "archive": ARCHIVE.relative_to(ROOT).as_posix(), "ui_history_runs": 23,
              "database_snapshots": snapshots, "source_databases_read_only": True}
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "database_snapshots"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
