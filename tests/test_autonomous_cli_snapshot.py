"""CLI source-evidence regression tests; no candidate, Docker or model execution."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def environment(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts/run_autonomous.py"
    spec = importlib.util.spec_from_file_location("autonomous_cli_snapshot_test", source)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    from agentforge.autonomous import AutonomousEngine

    root, base = tmp_path / "repo", tmp_path / "evidence/run"
    sources = {
        "backend/agentforge/autonomous.py": b"# frozen controller v1\n",
        "backend/agentforge/finishing_budget.py": b"# frozen finishing policy v1\n",
        "backend/agentforge/web_preview.py": b"# frozen preview v1\n",
        "sandbox/autonomous/runner.py": b"# standard runner v1\n",
        "sandbox/web/runner.py": b"# HTTP acceptance runner v1\n",
        "sandbox/web/web_bridge.py": b"# HTTP bridge v1\n",
        "sandbox/web/serve.py": b"# Web launcher v1\n",
        "sandbox/web/http_probe.py": b"# bounded HTTP probe v1\n",
        "sandbox/web/download_wheels.py": b"# trusted dependency preparation v1\n",
        "sandbox/web/Dockerfile": b"# offline dependency image recipe v1\n",
        "scripts/run_autonomous.py": b"# CLI receipt fixture, never executed\n",
    }
    for name, content in sources.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    base.mkdir(parents=True)
    runtime = SimpleNamespace(root=root, _run_dir=lambda run_id: base)
    engine = SimpleNamespace(runtime=runtime)
    engine._snapshot_implementation = lambda run_id: AutonomousEngine._snapshot_implementation(engine, run_id)
    monkeypatch.setattr(cli, "ROOT", root)
    return cli, engine, root, base, sources


def read_manifest(base):
    return json.loads((base / "implementation-manifest.json").read_text(encoding="utf-8"))


def test_web_cli_reuses_frozen_backend_and_all_web_sources_after_live_edits(environment):
    cli, engine, root, base, sources = environment
    engine._snapshot_implementation("run")
    original = read_manifest(base)
    for name in original:
        (root / name).write_bytes(b"# changed after engine created the run\n")
    cli.snapshot_cli_implementation(engine, "run")
    manifest = read_manifest(base)
    assert set(manifest) == set(sources)
    assert {name: manifest[name] for name in original} == original
    for name, content in sources.items():
        assert (base / "implementation" / name).read_bytes() == content
        assert manifest[name] == hashlib.sha256(content).hexdigest()
    assert not (base / "implementation-manifest.cli.pending.json").exists()


def test_non_web_cli_uses_same_complete_engine_inventory(environment):
    cli, engine, _, base, sources = environment
    assert not (base / "implementation-manifest.json").exists()
    cli.snapshot_cli_implementation(engine, "run")
    assert set(read_manifest(base)) == set(sources)


def test_existing_cli_snapshot_is_not_replaced_by_a_later_cli_version(environment):
    cli, engine, root, base, _ = environment
    cli.snapshot_cli_implementation(engine, "run")
    before = (base / "implementation-manifest.json").read_bytes()
    frozen_cli = (base / "implementation/scripts/run_autonomous.py").read_bytes()
    (root / "scripts/run_autonomous.py").write_bytes(b"# different working-tree CLI\n")
    cli.snapshot_cli_implementation(engine, "run")
    assert (base / "implementation-manifest.json").read_bytes() == before
    assert (base / "implementation/scripts/run_autonomous.py").read_bytes() == frozen_cli


def test_changed_frozen_copy_fails_without_repairing_or_overwriting_evidence(environment):
    cli, engine, _, base, _ = environment
    engine._snapshot_implementation("run")
    manifest = (base / "implementation-manifest.json").read_bytes()
    target = base / "implementation/sandbox/web/serve.py"
    target.write_bytes(b"# corrupted frozen evidence\n")
    with pytest.raises(RuntimeError, match="copy changed"):
        cli.snapshot_cli_implementation(engine, "run")
    assert target.read_bytes() == b"# corrupted frozen evidence\n"
    assert (base / "implementation-manifest.json").read_bytes() == manifest
    assert not (base / "implementation/scripts/run_autonomous.py").exists()


@pytest.mark.parametrize("invalid", [[], {}, None, {"../outside.py": "a" * 64}])
def test_invalid_existing_manifest_is_not_silently_regenerated(environment, invalid):
    cli, engine, _, base, _ = environment
    original = json.dumps(invalid).encode()
    (base / "implementation-manifest.json").write_bytes(original)
    with pytest.raises(RuntimeError, match="invalid"):
        cli.snapshot_cli_implementation(engine, "run")
    assert (base / "implementation-manifest.json").read_bytes() == original
    assert not (base / "implementation").exists()


def test_partial_cli_append_remains_visible_and_is_never_overwritten(environment):
    cli, engine, _, base, _ = environment
    engine._snapshot_implementation("run")
    original = (base / "implementation-manifest.json").read_bytes()
    target = base / "implementation/scripts/run_autonomous.py"
    target.parent.mkdir()
    target.write_bytes(b"# preexisting interrupted append\n")
    with pytest.raises(FileExistsError):
        cli.snapshot_cli_implementation(engine, "run")
    assert (base / "implementation-manifest.json").read_bytes() == original
    assert target.read_bytes() == b"# preexisting interrupted append\n"
