"""Docker-only input-baseline checks. No model requests; no host candidate imports."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.autonomous_sandbox import AutonomousSandbox
from agentforge.runtime import Runtime


async def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data_dir = ROOT / ".agentforge" / "autonomous-fixture-baselines" / stamp
    runtime = Runtime(ROOT, data_dir)
    sandbox = AutonomousSandbox(runtime)
    data_dir.mkdir(parents=True, exist_ok=False)
    summary = {"model_mode": "none", "model_calls": 0, "purpose": "input baseline verification only", "runs": []}
    for name in ("sales_report", "interval_library", "shipping_seeded"):
        bundle = json.loads((Path(__file__).parent / (name + ".json")).read_text(encoding="utf-8"))
        run_id = "baseline_" + name
        await sandbox.prepare(run_id, bundle)
        result = await sandbox.test(run_id)
        record = {"id": run_id, "provenance": bundle["provenance"], "result": result}
        summary["runs"].append(record)
        (data_dir / "evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"id": run_id, "status": result["status"], "test_count": result.get("test_count"), "isolated": result.get("isolated")}, ensure_ascii=True), flush=True)
    print(json.dumps({"evidence": str(data_dir / "evidence.json")}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
