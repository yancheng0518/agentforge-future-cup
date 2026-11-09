"""CLI lifecycle simulations only. No model or Docker calls are performed."""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("phase", ["preflight", "execution"])
def test_cli_interruption_exports_known_state(tmp_path, monkeypatch, phase):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("autonomous_cli_simulation", root / "scripts/run_autonomous.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    bundle_file = tmp_path / "input.json"
    bundle_file.write_text(json.dumps({"requirement": "unit simulation"}), encoding="utf-8")
    called = {"execute": 0}

    class FakeRuntime:
        def __init__(self, root, data_dir):
            self.data_dir = data_dir

        def _run_dir(self, run_id):
            target = self.data_dir / "runs" / run_id
            target.mkdir(parents=True, exist_ok=True)
            return target

        async def health(self):
            if phase == "preflight":
                raise asyncio.CancelledError("unit simulated preflight interruption")
            return {"model": {"configured": True}, "sandbox": {"available": True}}

    class FakeEngine:
        def __init__(self, store, runtime):
            self.store = store
            self.runtime = runtime

        def _snapshot_implementation(self, run_id):
            # Explicit fake snapshot for this lifecycle-only simulation.
            import hashlib
            path = self.runtime._run_dir(run_id)
            source = path / "implementation/backend/agentforge/fake.py"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"# simulated implementation, never executed\n")
            (path / "implementation-manifest.json").write_text(json.dumps({
                "backend/agentforge/fake.py": hashlib.sha256(source.read_bytes()).hexdigest()}))

        def create_run(self, bundle, budget):
            run = {"id": "cli_interrupt_unit", "status": "queued", "summary": "unit simulation",
                   "model_metrics": {"model_calls": 0}, "task_graph": [],
                   "controller_decisions": [], "verifications": []}
            self.store.create_run(run)
            return run

        async def execute(self, run_id):
            called["execute"] += 1
            self.store.update_run(run_id, status="blocked", summary="unit simulated execution interruption")
            raise asyncio.CancelledError("unit simulated execution interruption")

        def export(self, run_id):
            return {"run": self.store.run(run_id), "events": self.store.events(run_id), "steps": [], "snapshots": []}

    monkeypatch.setattr(cli, "Runtime", FakeRuntime)
    monkeypatch.setattr(cli, "AutonomousEngine", FakeEngine)
    args = argparse.Namespace(bundle=str(bundle_file), data_dir=str(tmp_path / "evidence"))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli.main(args))
    evidence_path = tmp_path / "evidence/runs/cli_interrupt_unit/evidence.json"
    assert evidence_path.is_file(), "Cancellation after run creation must not skip the final evidence export"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["run"]["status"] == "blocked"
    assert evidence["run"]["model_metrics"]["model_calls"] == 0
    assert called["execute"] == (phase == "execution")
