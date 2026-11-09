"""Run a frozen arbitrary Python task bundle with the real controller and Docker.

Every invocation creates a fresh run and evidence directory; never overwrites a
previous result or applies generated code to the source project.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.autonomous import AutonomousEngine
from agentforge.runtime import Runtime
from agentforge.storage import Store


async def main(args):
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8-sig"))
    data_dir = Path(args.data_dir).resolve()
    store = Store(data_dir / "agentforge.sqlite3")
    runtime = Runtime(ROOT, data_dir)
    engine = AutonomousEngine(store, runtime)
    budget = {key: value for key, value in vars(args).items()
              if key not in ("bundle", "data_dir") and value is not None}
    run = engine.create_run(bundle, budget)
    path = runtime._run_dir(run["id"])
    print(json.dumps({"run_id": run["id"], "evidence_dir": str(path)}, ensure_ascii=True), flush=True)
    try:
        source_files = [*sorted((ROOT / "backend/agentforge").glob("*.py")),
                        *sorted((ROOT / "sandbox/autonomous").glob("*.py")),
                        ROOT / "scripts/run_autonomous.py"]
        manifest = {}
        for source in source_files:
            relative = source.relative_to(ROOT)
            target = path / "implementation" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            manifest[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
        (path / "implementation-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        health = await runtime.health()
        store.event(run["id"], "preflight", "启动前环境检查（尚未证明模型请求成功）", data=health)
        if health["model"]["configured"] and health["sandbox"]["available"]:
            await engine.execute(run["id"])
        else:
            store.update_run(run["id"], status="blocked", summary="Model configuration or Docker unavailable")
    except asyncio.CancelledError:
        if store.run(run["id"])["status"] in ("queued", "running"):
            store.update_run(run["id"], status="blocked", summary="CLI interrupted; completion unconfirmed, no automatic replay")
        raise
    except Exception as exc:
        store.update_run(run["id"], status="failed", summary="CLI failure: " + type(exc).__name__)
    finally:
        # Ctrl+C also exports the blocked state and all known/unknown usage.
        export = engine.export(run["id"])
        (path / "evidence.json").write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    final = export["run"]
    print(json.dumps({"run_id": final["id"], "status": final["status"], "summary": final["summary"],
                      "model_metrics": final["model_metrics"], "tasks": len(final["task_graph"]),
                      "decisions": [x["action"] for x in final["controller_decisions"]],
                      "verifications": [{k: x.get(k) for k in ("status", "test_count", "classification")}
                                        for x in final["verifications"]]}, ensure_ascii=True), flush=True)
    return 0 if final["status"] == "succeeded" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", help="UTF-8 JSON task bundle; tests run only inside Docker")
    parser.add_argument("--data-dir", default=str(ROOT / ".agentforge/autonomous"))
    for name in ("max_calls", "max_tokens", "max_output_tokens", "max_steps", "max_tasks",
                 "max_parallel", "max_verifications", "wall_seconds"):
        parser.add_argument("--" + name.replace("_", "-"), type=int)
    sys.exit(asyncio.run(main(parser.parse_args())))
