"""Synthetic archive/SQLite fixtures; never packages workspace data or runs candidates."""
from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import pytest

BATCH = Path(__file__).resolve().parents[1] / "output/web-projects-20261109"
sys.path.insert(0, str(BATCH))
import archive_support as SUPPORT
import restore_reproduction as RESTORE

SPEC = importlib.util.spec_from_file_location("web_package_test", BATCH / "package_reproduction.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)


def zip_bytes(files):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return target.getvalue()


def fixture_archive(tmp_path, files=None, **metadata):
    files = files or {"README.md": b"Synthetic inert source", "candidate/app.py": b"raise AssertionError('MUST NOT EXECUTE')\n"}
    manifest = {"schema": SUPPORT.SCHEMA, "purpose": "agentforge-web-projects-reproduction-v1",
                "ui_history_runs": 25, "declared_live_ids": [], "database_snapshots": [],
                "files": {name: {"bytes": len(value), "sha256": SUPPORT.sha(value), "kind": "synthetic"}
                          for name, value in files.items()}, **metadata}
    target = tmp_path / "synthetic.zip"
    target.write_bytes(zip_bytes({**files, SUPPORT.MANIFEST: json.dumps(manifest).encode()}))
    target.with_suffix(".zip.sha256").write_text(SUPPORT.sha(target.read_bytes()) + "  " + target.name + "\n", encoding="ascii")
    return target, manifest


def test_recursive_wheel_scan_finds_configured_secret_without_extracting():
    wheel = zip_bytes({"package/core.py": b"fixture-secret-in-wheel"})
    nested = zip_bytes({"evidence/wheelhouse/package.whl": wheel})
    with pytest.raises(SUPPORT.ExportError, match="credential"):
        SUPPORT.scan_nested_zip(nested, (b"fixture-secret-in-wheel",), {"entries": 0, "bytes": 0})


def test_wheel_public_package_files_are_scanned_as_data_not_rejected_by_host_names():
    wheel = zip_bytes({"pkg/secrets.py": b"# ordinary package module, never imported\n",
                       "pkg/cacert.pem": b"public certificate fixture", "pkg/native.pyd": b"inert extension fixture"})
    budget = {"entries": 0, "bytes": 0}
    SUPPORT.scan_nested_zip(wheel, (), budget, wheel=True)
    assert budget["entries"] == 3
    with pytest.raises(SUPPORT.ExportError):
        SUPPORT.scan_nested_zip(wheel, (), {"entries": 0, "bytes": 0})


@pytest.mark.parametrize("name", ["../escape", "C:/outside", "folder\\outside", ".env", "folder/.env.local",
                                  "wsl/disk.vhdx", "pkg/cache.pyc", "db.sqlite3-wal"])
def test_wheel_or_nested_zip_never_bypasses_path_disk_dotenv_rules(name):
    # zipfile normalizes native Windows separators while writing; construct the
    # malicious central/local names explicitly without extracting either form.
    content = zip_bytes({name.replace("\\", "/"): b"inert"})
    if "\\" in name:
        content = content.replace(name.replace("\\", "/").encode(), name.encode())
    with pytest.raises(SUPPORT.ExportError):
        SUPPORT.scan_nested_zip(content, (), {"entries": 0, "bytes": 0}, wheel=True)


def test_archive_budget_and_member_hash_fail_before_extraction(tmp_path, monkeypatch):
    path, _ = fixture_archive(tmp_path)
    monkeypatch.setattr(SUPPORT, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(SUPPORT.ExportError, match="budget"):
        RESTORE.restore(path, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_read_only_verification_and_exclusive_restore_preserve_inert_candidate(tmp_path):
    path, _ = fixture_archive(tmp_path)
    original = path.read_bytes()
    assert RESTORE.restore(path)["extracted"] is False
    destination = tmp_path / "new"
    report = RESTORE.restore(path, destination)
    assert report["restored_files_verified"] and report["candidate_execution"] is False
    assert report["new_machine_runtime_verified"] is False
    assert (destination / "candidate/app.py").read_bytes().startswith(b"raise AssertionError")
    assert path.read_bytes() == original
    with pytest.raises(SUPPORT.ExportError, match="nonexistent"):
        RESTORE.restore(path, destination)


def test_restore_refuses_changed_sidecar_without_creating_directory(tmp_path):
    path, _ = fixture_archive(tmp_path)
    path.with_suffix(".zip.sha256").write_text("0" * 64 + "  " + path.name)
    with pytest.raises(SUPPORT.ExportError, match="sidecar"):
        RESTORE.restore(path, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_sqlite_snapshot_includes_committed_wal_without_modifying_source(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    output = root / "output"
    output.mkdir()
    monkeypatch.setattr(PACKAGE, "ROOT", root)
    monkeypatch.setattr(PACKAGE, "SNAPSHOTS", output / "snapshots")
    monkeypatch.setattr(PACKAGE, "LIVE_DATABASES", {})
    source = root / "live.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY,body TEXT)")
        db.execute("INSERT INTO samples VALUES(1,'synthetic committed WAL evidence')")
        db.commit()
        original = {p.name: p.read_bytes() for p in root.glob("live.sqlite3*") if not p.name.endswith("-shm")}
        selected = {}
        snapshots = PACKAGE.snapshot_databases(["live.sqlite3"], {}, selected)
        assert snapshots[0]["tables"]["samples"]["rows"] == 1
        assert original == {p.name: p.read_bytes() for p in root.glob("live.sqlite3*") if not p.name.endswith("-shm")}
        with closing(sqlite3.connect(selected["live.sqlite3"][0])) as copy:
            assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert copy.execute("SELECT body FROM samples").fetchone()[0] == "synthetic committed WAL evidence"
        PACKAGE.ensure_database_inputs_unchanged(snapshots)
        db.execute("INSERT INTO samples VALUES(2,'new write must reject packaging')")
        db.commit()
        with pytest.raises(SUPPORT.ExportError, match="changed during packaging"):
            PACKAGE.ensure_database_inputs_unchanged(snapshots)
        files = {name: path.read_bytes() for name, (path, _) in selected.items()}
    archive, _ = fixture_archive(tmp_path, files, database_snapshots=snapshots)
    result = RESTORE.restore(archive, tmp_path / "restored")
    assert result["database_roles_verified"] == {"restorable_database": 1, "historical_snapshot": 0}


@pytest.mark.parametrize("status", ["running", "queued", "unknown"])
def test_web_registry_pending_dependency_job_blocks_packaging(tmp_path, status):
    with closing(sqlite3.connect(tmp_path / "registry.sqlite3")) as db:
        db.execute("CREATE TABLE jobs(body TEXT)")
        db.execute("CREATE TABLE preview_requests(preview_id TEXT)")
        db.execute("INSERT INTO jobs VALUES(?)", (json.dumps({"status": status}),))
        with pytest.raises(SUPPORT.ExportError, match="job is still active"):
            PACKAGE.validate_database(db, ".agentforge/web/requests.sqlite3")


def test_web_registry_unknown_preview_completion_blocks_packaging(tmp_path):
    with closing(sqlite3.connect(tmp_path / "registry.sqlite3")) as db:
        db.execute("CREATE TABLE jobs(body TEXT)")
        db.execute("CREATE TABLE preview_requests(preview_id TEXT)")
        db.execute("INSERT INTO preview_requests VALUES(NULL)")
        with pytest.raises(SUPPORT.ExportError, match="preview idempotency"):
            PACKAGE.validate_database(db, ".agentforge/web/requests.sqlite3")


def test_live_inventory_is_variable_and_retains_failures_not_only_saved_publications(tmp_path, monkeypatch):
    monkeypatch.setattr(PACKAGE, "ROOT", tmp_path)
    monkeypatch.setattr(PACKAGE, "OUTDIR", tmp_path)
    rows = [{"run_id": f"{index:032x}", "data_dir": ".agentforge/autonomous", "kind": "natural"} for index in range(4)]
    (tmp_path / "live-ids.json").write_text(json.dumps({"schema": "agentforge-web-live-ids-v1", "runs": rows}))
    assert PACKAGE.load_live_ids() == rows
    rows.append(rows[0])
    (tmp_path / "live-ids.json").write_text(json.dumps({"schema": "agentforge-web-live-ids-v1", "runs": rows}))
    with pytest.raises(SUPPORT.ExportError, match="duplicate"):
        PACKAGE.load_live_ids()


@pytest.mark.parametrize("status,lease,valid", [
    ("stopped", None, True), ("ready", None, False),
    ("stopped", {"status": "released", "client_reaped": True, "pid": 123}, True),
    ("stopped", {"status": "release_failed", "client_reaped": False, "pid": 123}, False),
])
def test_all_preview_roots_require_terminal_lifecycle_and_preserve_crlf_bytes(tmp_path, monkeypatch, status, lease, valid):
    monkeypatch.setattr(PACKAGE, "ROOT", tmp_path)
    prefix = ".agentforge/web-validation-20261109/previews/" + "a" * 32
    source = "# synthetic source, never executed\r\n"
    record = {"id": "a" * 32, "status": status, "container_invoked": True, "container_removed": True,
              "source_sha256": PACKAGE.sha(PACKAGE.canonical({"app.py": source})), "runner_sha256": {}}
    if lease:
        record["wsl_lease"] = lease
    values = {prefix + "/candidate/app.py": source.encode(),
              prefix + "/events/0001.json": json.dumps({"record": record}).encode()}
    selected = {}
    for name, value in values.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        selected[name] = (path, "synthetic")
    if valid:
        assert PACKAGE.validate_web_artifacts(selected, ())["previews"][0]["source_hash_verified"] is True
    else:
        with pytest.raises(SUPPORT.ExportError):
            PACKAGE.validate_web_artifacts(selected, ())


@pytest.mark.parametrize("schema,manifest_name", [
    (None, "output/core/archive-manifest.json"),
    ("agentforge-autonomous-chat-reproduction-v1", "CHAT_REPRODUCTION_MANIFEST.json"),
    (SUPPORT.SCHEMA, SUPPORT.MANIFEST),
])
def test_sealed_previous_archives_use_their_original_schema_without_migration(tmp_path, monkeypatch, schema, manifest_name):
    monkeypatch.setattr(PACKAGE, "ROOT", tmp_path)
    monkeypatch.setattr(PACKAGE, "PREVIOUS_MANIFESTS", {"old.zip": (manifest_name, schema)})
    files = {"README.md": b"Historical inert evidence"}
    metadata = {"files": {name: {"bytes": len(value), "sha256": SUPPORT.sha(value)} for name, value in files.items()}}
    if schema is not None:
        metadata["schema"] = schema
    path = tmp_path / "old.zip"
    path.write_bytes(zip_bytes({**files, manifest_name: json.dumps(metadata).encode()}))
    original = path.read_bytes()
    path.with_suffix(".zip.sha256").write_text(SUPPORT.sha(original) + "  old.zip\n")
    result = PACKAGE.verify_previous_archive(path, ())
    assert result["verified"] and result["original_schema"] == schema
    assert path.read_bytes() == original


def test_failed_dependency_build_evidence_is_selected_but_dotenv_and_disks_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr(PACKAGE, "ROOT", tmp_path)
    monkeypatch.setattr(PACKAGE, "EVIDENCE_ROOTS", (".agentforge/web",))
    monkeypatch.setattr(PACKAGE, "LIVE_DATABASES", {})
    monkeypatch.setattr(PACKAGE, "SNAPSHOTS", tmp_path / "snapshots")
    names = [".agentforge/web/dependencies/attempts/a/build/Dockerfile",
             ".agentforge/web/dependencies/attempts/a/build/wheels/pkg.whl",
             ".agentforge/web/dependencies/attempts/a/record.json",
             ".agentforge/web/.env", ".agentforge/web/wsl/disk.vhdx"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"inert synthetic evidence")
    selected = {}
    PACKAGE.collect_evidence(selected)
    assert set(selected) == set(names[:3])


def test_idle_platform_validation_uses_its_frozen_copy_not_current_source(tmp_path, monkeypatch):
    monkeypatch.setattr(PACKAGE, "ROOT", tmp_path)
    source = b"# earlier host implementation, never executed\n"
    prefix = "output/web-projects-20261109/"
    values = {prefix + "preview-idle-implementation.py.txt": source,
              prefix + "preview-idle-attempt99.intent.json": json.dumps({"classification": "platform_preview_idle_regression",
                    "implementation_sha256": SUPPORT.sha(source)}).encode(),
              "backend/agentforge/web_preview.py": b"# later implementation, intentionally different\n"}
    selected = {}
    for name, content in values.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        selected[name] = (path, "synthetic")
    result = PACKAGE.platform_implementation_copies(selected, ())
    assert result[0]["compared_to_packaging_time_source"] is False
    selected[prefix + "preview-idle-implementation.py.txt"][0].write_bytes(b"changed")
    with pytest.raises(SUPPORT.ExportError, match="matching frozen"):
        PACKAGE.platform_implementation_copies(selected, ())
