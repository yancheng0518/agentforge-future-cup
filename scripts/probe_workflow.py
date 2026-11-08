"""Real Docker + explicitly mock-model workflow validation; never calls an LLM.

First checks observable container boundaries, then exercises an actual Engine
failure/feedback/repair flow in a separate evidence database. Never applies code.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.engine import Engine
from agentforge.runtime import MOCK_SOURCE, Runtime
from agentforge.storage import Store

BOUNDARY_CHECKS = '''import errno
import os
from pathlib import Path
assert os.geteuid() == 65534, "candidate must be non-root UID 65534"
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
assert int(status["CapEff"].strip(), 16) == 0, "effective capabilities must be empty"
assert status["NoNewPrivs"].strip() == "1", "no-new-privileges must be enabled"
assert status["Seccomp"].strip() == "2", "seccomp filter must be enabled"
assert set(os.listdir("/sys/class/net")) == {"lo"}, "network namespace must contain loopback only"
for destination in ("/candidate/forbidden.txt", "/acceptance/forbidden.txt", "/rootfs-forbidden.txt"):
    try:
        with open(destination, "w") as handle:
            handle.write("write-should-not-succeed")
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES), "unexpected write failure"
    else:
        raise AssertionError("read-only write boundary failed")
'''


async def main():
    started = time.monotonic()
    directory = ROOT / ".agentforge" / "verification" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
    runtime = Runtime(ROOT, directory)
    health = await runtime.health()
    if not health.get("sandbox", {}).get("available"):
        print("BLOCKED: Docker prerequisites unavailable; no candidate executed.")
        return 2
    boundary_id = "boundary-" + uuid4().hex
    await runtime.prepare(boundary_id)
    source = BOUNDARY_CHECKS + MOCK_SOURCE.replace("{filter}", " if completed is None or task['completed'] == completed")
    await runtime.apply_candidate(boundary_id, [{"path": "todo.py", "content": source}])
    boundary = await runtime.test(boundary_id)
    boundary_ok = boundary.get("status") == "passed" and boundary.get("isolated") is True
    evidence = {"captured_at": datetime.now(timezone.utc).isoformat(), "mode": "mock model + real Docker",
                "llm_self_correction_evidence": False, "boundary_probe": {"passed": boundary_ok, "run_id": boundary_id, "result": boundary}}
    print(json.dumps({"container_boundaries_passed": boundary_ok}, ensure_ascii=False), flush=True)
    if boundary_ok:
        store = Store(directory / "verification.sqlite3")
        engine = Engine(store, runtime)
        run = engine.create_run("增加任务完成状态，并支持按照完成状态筛选任务。", mode="mock")
        await engine.execute(run["id"])
        saved = store.run(run["id"])
        events = store.events(run["id"])
        gates = [event["data"] for event in events if event["type"] == "validation" and "passed" in event["data"]]
        loop_ok = (saved["status"] == "succeeded" and saved["attempt"] == 1 and saved["model_calls"] == 0
                   and not saved["applied"] and len(gates) == 2
                   and gates[0]["test"].get("status") == "failed" and gates[0]["test"].get("isolated") is True
                   and gates[1]["test"].get("status") == "passed" and gates[1]["test"].get("isolated") is True
                   and any(event["type"] == "feedback" for event in events))
        evidence.update(workflow_passed=loop_ok, run=saved, events=events, steps=store.steps(run["id"]), diff=await runtime.diff(run["id"]))
    else:
        evidence["workflow_passed"] = False
    evidence["duration_seconds"] = round(time.monotonic() - started, 2)
    output = directory / "evidence.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"container_boundaries_passed": boundary_ok, "mock_model_real_docker_loop_passed": evidence["workflow_passed"],
               "evidence": str(output), "duration_seconds": evidence["duration_seconds"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if boundary_ok and evidence["workflow_passed"] else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
