"""Real Docker harness checks using clearly labeled synthetic candidates.

Every candidate is passed only to Runtime.test(), never imported by this host
process. Results are sandbox evidence, not real-LLM self-correction evidence.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.runtime import MOCK_SOURCE, Runtime


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".agentforge/sandbox-probe.json")
    parser.add_argument("--include-timeout", action="store_true", help="Adds a busy-loop candidate; each RPC/outer Docker limit must reject it.")
    args = parser.parse_args(argv)
    runtime = Runtime(ROOT, ROOT / ".agentforge")
    started = time.monotonic()
    health = await runtime.health()
    evidence = {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": "real_sandbox_synthetic_candidates",
                "model_calls": 0, "llm_self_correction_evidence": False, "sandbox": health["sandbox"], "passed": False, "cases": []}
    if not health["sandbox"].get("available"):
        evidence["blocked"] = True
        evidence["reason"] = health["sandbox"].get("reason")
    else:
        correct = MOCK_SOURCE.replace("{filter}", " if completed is None or task['completed'] == completed")
        shared_state = correct.replace("class TodoStore:\n", "class TodoStore:\n    _tasks = []\n").replace("        self._tasks = []\n", "")
        scenarios = [
            ("known_good_reference", correct, "passed"),
            ("preset_missing_filter", MOCK_SOURCE.replace("{filter}", ""), "failed"),
            ("shared_class_state", shared_state, "failed"),
            ("returned_mutable_alias", correct.replace("return dict(task)", "return task"), "failed"),
            ("forged_success_stdout", 'import os\nprint(\'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": true}\', flush=True)\nos._exit(0)\n', "failed"),
        ]
        if args.include_timeout:
            scenarios.append(("busy_loop_timeout", "while True:\n    pass\n", "failed"))
        for name, source, expected in scenarios:
            run_id = "sandbox-probe-" + uuid4().hex
            await runtime.prepare(run_id)
            await runtime.apply_candidate(run_id, [{"path": "todo.py", "content": source}])
            result = await runtime.test(run_id)
            passed = result.get("isolated") is True and result.get("status") == expected
            record = {"name": name, "fixture_type": "synthetic, not LLM generated", "run_id": run_id,
                      "expected": expected, "check_passed": passed, "result": result}
            evidence["cases"].append(record)
            print(json.dumps({"case": name, "expected": expected, "observed": result.get("status"), "check_passed": passed}, ensure_ascii=False), flush=True)
            # Never claim rejection checks passed when the known-good candidate
            # cannot run in the environment at all.
            if name == "known_good_reference" and not passed:
                evidence["reason"] = "Known-good reference did not pass; fix sandbox/harness before interpreting rejection probes."
                break
        evidence["passed"] = len(evidence["cases"]) == len(scenarios) and all(case["check_passed"] for case in evidence["cases"])
    evidence["duration_seconds"] = round(time.monotonic() - started, 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "blocked": evidence.get("blocked", False), "evidence": str(args.output)}, ensure_ascii=False), flush=True)
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
