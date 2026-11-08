"""Replay a failed inspect against the preregistered frozen adapter, read-only.

This is a new diagnostic invocation, not a replacement for original results.
No candidate is executed, written, applied, or submitted to the main Engine.
Responses omit reasoning_content and redact the configured credential.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def strip_reasoning(value):
    if isinstance(value, dict):
        return {k: strip_reasoning(v) for k, v in value.items() if k != "reasoning_content"}
    if isinstance(value, list):
        return [strip_reasoning(v) for v in value]
    return value


def inspect_content(response):
    try:
        choice = response["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        return {"problem": "choices/message missing"}
    content = message.get("content")
    observed = {"finish_reason": choice.get("finish_reason"), "content_type": type(content).__name__,
                "content_chars": len(content) if isinstance(content, str) else None,
                "tool_calls": len(message.get("tool_calls") or []), "usage": response.get("usage")}
    if message.get("tool_calls"):
        observed["stage"] = "tool_request"
        return observed
    if not isinstance(content, str):
        observed["problem"] = "content is not a string"
        return observed
    normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", content) if content.startswith("```") else content
    try:
        value = json.loads(normalized)
        observed.update(json_valid=True, json_type=type(value).__name__)
        if isinstance(value, dict):
            observed.update(keys=list(value), reviewer_contract_valid=(type(value.get("passed")) is bool and isinstance(value.get("issues"), list)))
    except json.JSONDecodeError as exc:
        observed.update(json_valid=False, json_error=exc.msg, json_error_position=exc.pos,
                        json_error_line=exc.lineno, json_error_column=exc.colno)
    return observed


async def replay(args):
    batch = args.batch.resolve()
    frozen = batch / "frozen"
    if not (frozen / "backend/agentforge/runtime.py").is_file():
        raise ValueError("A frozen batch adapter is required")
    sys.path.insert(0, str(frozen / "backend"))
    from agentforge.engine import Engine, candidate_fingerprint
    from agentforge.runtime import Runtime

    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    run = evidence["run"]
    failed = [e for e in run["events"] if e.get("node_id") == "round-0/verify/inspect"
              and e.get("type") == "agent" and e.get("data", {}).get("status") == "failed"]
    if len(failed) != 1 or run.get("mode") != "live" or run.get("fault_injection") is not False:
        raise ValueError("Expected one failed natural round-zero inspect invocation")
    with sqlite3.connect((ROOT / ".agentforge/agentforge.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        values = [json.loads(r[0]) for r in db.execute("SELECT body FROM snapshots WHERE run_id=? ORDER BY id", (run["id"],))]
    snapshot = next(s for s in values if s.get("node_id") == "develop")
    context = deepcopy(snapshot["context"])
    Engine._begin_validation(context, "round-0/verify", candidate_fingerprint(context["diff"]))
    task = {"mode": "live", "requirement": run["requirement"], "kind": "inspect", "workflow_id": run["workflow_id"]}
    agent = next(a for a in run["agent_versions"] if a["id"] == failed[0]["data"]["agent_id"] and a["version"] == failed[0]["data"]["agent_version"])
    directory = ROOT / ".agentforge/protocol-probes" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    runtime = Runtime(ROOT, ROOT / ".agentforge")
    runtime.root = frozen
    secret = runtime.settings["api_key"]
    state = {"schema": "agentforge-frozen-model-contract-probe-v1", "started_at": now(),
             "original_run_id": run["id"], "original_failed_event_id": failed[0]["id"],
             "original_evidence_sha256": digest(args.evidence.read_bytes()), "batch": str(batch),
             "adapter_path": str(frozen / "backend/agentforge/runtime.py"),
             "adapter_sha256": digest((frozen / "backend/agentforge/runtime.py").read_bytes()),
             "model_config": runtime.model_config(), "max_requests": 4, "requests": [],
             "original_result_unchanged": True, "new_diagnostic_invocation": True,
             "candidate_execution": False, "candidate_applied": False,
             "reconstruction": "SQLite read-only develop snapshot; Engine._begin_validation adds round-0 scope. Inspection starts before parallel Docker result. Frozen agent version and knowledge files. Original wire payload/response were not retained, so this is reconstructed input, not byte-identical request replay.",
             "input": {"agent": agent, "task": task, "context": context}}

    def write():
        text = json.dumps(strip_reasoning(state), ensure_ascii=False, indent=2)
        if secret:
            text = text.replace(secret, "[REDACTED_API_KEY]")
        (directory / "evidence.json").write_text(text, encoding="utf-8")

    original_post = runtime._post

    def capture(payload):
        if len(state["requests"]) >= 4:
            raise RuntimeError("Diagnostic request cap reached")
        record = {"request": len(state["requests"]) + 1, "started_at": now(), "payload": deepcopy(payload)}
        state["requests"].append(record)
        write()
        try:
            response = original_post(payload)
            record.update(response=strip_reasoning(response), observation=inspect_content(response))
            return response
        except Exception as exc:
            record.update(error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            record["finished_at"] = now()
            write()

    runtime._post = capture
    (directory / "probe_model_contract.py.txt").write_bytes(Path(__file__).read_bytes())
    write()
    print("Diagnostic evidence: " + str(directory), flush=True)
    try:
        result = await runtime.invoke(agent, task, context)
        state.update(status="succeeded", result=result)
    except Exception as exc:
        state.update(status="failed", error_type=type(exc).__name__, error=str(exc),
                     model_calls=getattr(exc, "model_calls", None), tokens=getattr(exc, "tokens", None),
                     metrics_complete=getattr(exc, "metrics_complete", None))
    finally:
        state["finished_at"] = now()
        write()
    print(json.dumps({"status": state["status"], "requests": len(state["requests"]),
                      "observations": [r.get("observation") for r in state["requests"]]}, ensure_ascii=False), flush=True)
    return 0 if state["status"] == "succeeded" else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(replay(args))


if __name__ == "__main__":
    sys.exit(main())
