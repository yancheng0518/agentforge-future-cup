"""Run a real workflow through the local API and export redacted evidence.

This script never generates candidate code, fabricates test results, applies a
patch, or forces a repair. Start the backend first. Missing isolation/model
configuration stops before a model request is submitted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.runtime import Runtime
from agentforge.engine import candidate_fingerprint


def api_request(base, path, body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=payload, headers={"Content-Type": "application/json"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # The local API returns safe validation messages. Never dump headers or
        # arbitrary proxy/server response HTML to the console.
        raise RuntimeError(f"Local API HTTP {exc.code} at {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("Local API unavailable or timed out; start scripts/backend.ps1 first.") from None


def redact(value, secret):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in {"api_key", "authorization", "access_token"} else redact(v, secret)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    return value


def extract_outcome(detail):
    events = detail.get("events", [])
    # API events are ordered by SQLite ID. Do not silently reorder a broken
    # export into a convincing sequence of failure, feedback, and repair.
    ids = [e.get("id") for e in events]
    ordered = all(type(i) is int for i in ids) and all(a < b for a, b in zip(ids, ids[1:]))
    steps = {step["id"]: step for step in detail.get("steps", [])}

    def live_output(event):
        data = event.get("data", {})
        return (event.get("type") == "agent" and data.get("model_mode") == "live"
                and data.get("status") != "failed" and data.get("model_calls", 0) > 0
                and isinstance(data.get("output"), dict) and bool(data["output"]))

    def test_passed(test):
        return test.get("isolated") is True and test.get("status") == "passed" and type(test.get("exit_code")) is int and test["exit_code"] == 0

    def test_failed(test):
        return (test.get("isolated") is True and test.get("status") == "failed"
                and type(test.get("exit_code")) is int and test["exit_code"] == 1
                and 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": false}' in test.get("stdout", ""))

    def valid_gate(index, event):
        data = event.get("data", {})
        attempt = data.get("attempt")
        if type(attempt) is not int or event.get("node_id") != f"round-{attempt}/gate" or type(data.get("passed")) is not bool:
            return False
        verify_id = f"round-{attempt}/verify"
        test, review = data.get("test", {}), data.get("review", {})
        scope = test.get("evidence_scope", {})
        if (not scope.get("candidate_hash") or scope.get("id") != verify_id or scope.get("attempt") != attempt
                or review.get("evidence_scope") != scope):
            return False
        # An invalidation retires old evidence even if its gate said passed.
        if any(e.get("type") == "validation" and e.get("node_id") == verify_id for e in events[index + 1:]):
            return False
        verify_step = steps.get(verify_id, {})
        test_step = steps.get(verify_id + "/test", {})
        if (verify_step.get("status") != "succeeded" or test_step.get("status") != "succeeded"
                or verify_step.get("output", {}).get("validation_scope") != scope
                or verify_step.get("output", {}).get("test") != test
                or verify_step.get("output", {}).get("review") != review
                or test_step.get("output", {}).get("test") != test):
            return False
        raw_test = {k: v for k, v in test.items() if k != "evidence_scope"}
        return any(e.get("type") == "tool" and e.get("node_id") == verify_id + "/test"
                   and e.get("data", {}).get("tool") == "run_acceptance"
                   and e["data"].get("source") == "runtime" and e["data"].get("output") == raw_test
                   for e in events[:index])

    gates = [(i, e) for i, e in enumerate(events) if ordered and e.get("type") == "validation" and valid_gate(i, e)]
    final_gate = gates[-1][1]["data"] if gates else {}
    final_test = final_gate.get("test", {})
    final_hash = final_test.get("evidence_scope", {}).get("candidate_hash")
    current_hash = candidate_fingerprint(detail.get("diff", {}))
    final_current = bool(final_hash and final_hash == current_hash and final_gate.get("attempt") == detail.get("attempt"))
    repairs = []
    for failed_index, failed_event in gates:
        failure = failed_event["data"]
        if failure["passed"] is not False:
            continue
        next_round = failure["attempt"] + 1
        fix_id = f"round-{next_round}/fix"
        fix_step = steps.get(fix_id, {})
        if fix_step.get("status") != "succeeded":
            continue
        for feedback_index in range(failed_index + 1, len(events)):
            feedback = events[feedback_index]
            if not (feedback.get("type") == "feedback" and feedback.get("node_id") == failed_event["node_id"]
                    and feedback.get("data", {}).get("next_attempt") == next_round
                    and feedback["data"].get("test") == failure.get("test")):
                continue
            for fix_index in range(feedback_index + 1, len(events)):
                fix_event = events[fix_index]
                if not (fix_event.get("node_id") == fix_id and live_output(fix_event)
                        and fix_event["data"]["output"] == fix_step.get("output")
                        and fix_event["data"]["output"].get("files")):
                    continue
                generated_files = fix_event["data"]["output"]["files"]
                if not all(isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("content"), str) for f in generated_files):
                    continue
                generated_hash = candidate_fingerprint({"files": [{"path": f["path"], "after": f["content"]} for f in generated_files]})
                for next_index, next_event in gates:
                    next_gate = next_event["data"]
                    if next_index <= fix_index or next_gate["attempt"] != next_round:
                        continue
                    repaired_hash = next_gate["test"]["evidence_scope"]["candidate_hash"]
                    written = any(e.get("type") == "tool" and e.get("node_id") == fix_id
                                  and e.get("data", {}).get("tool") == "candidate_diff"
                                  and e["data"].get("source") == "runtime" and e["data"].get("status") == "completed"
                                  and candidate_fingerprint(e["data"].get("output", {})) == repaired_hash
                                  for e in events[fix_index + 1:next_index])
                    if written and generated_hash == repaired_hash and repaired_hash != failure["test"]["evidence_scope"]["candidate_hash"]:
                        repairs.append({"round": next_round, "after_test_failure": test_failed(failure["test"])})
    if detail.get("mode") != "live":
        repairs = []
    live_observed = detail.get("mode") == "live" and ordered and any(live_output(e) for e in events)
    succeeded = (detail.get("mode") == "live" and detail.get("status") == "succeeded" and live_observed
                 and final_current and final_gate.get("passed") is True and test_passed(final_test)
                 and final_gate.get("review", {}).get("passed") is True and detail.get("verified_hash") == final_hash)
    return {
        "live_model_output_observed": live_observed,
        "real_isolated_test_passed": final_current and test_passed(final_test),
        "repair_observed": bool(repairs),
        "real_isolated_test_failure_observed": any(test_failed(e["data"]["test"]) for _, e in gates),
        "repair_after_test_failure_observed": any(item["after_test_failure"] for item in repairs),
        "workflow_succeeded": bool(succeeded),
        "server_reported_success": detail.get("status") == "succeeded",
        "repair_note": "Ordered evidence links failed validation, feedback, a live fix, candidate change and retest." if repairs else "No complete live repair sequence observed; no self-correction claim is made.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000/api")
    parser.add_argument("--workflow", choices=("development", "review"), default="development")
    parser.add_argument("--requirement", default="增加任务完成状态，并支持按照完成状态筛选任务。")
    parser.add_argument("--resume", help="Existing failed/blocked live run ID; no mock run can be resumed by this script.")
    parser.add_argument("--timeout", type=int, default=900, help="Observation timeout in seconds; does not cancel server execution.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-repair", action="store_true", help="Exit 3 if successful but no actual validation/repair cycle occurred; never inject a failure.")
    args = parser.parse_args(argv)
    parsed = urllib.parse.urlsplit(args.api_base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Only a local loopback HTTP API is accepted.")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")
    if args.resume and not re.fullmatch(r"[a-fA-F0-9]{32}", args.resume):
        parser.error("--resume must be a 32-character run ID")
    base = args.api_base.rstrip("/")
    started = time.monotonic()
    try:
        health = api_request(base, "/health")
        if not health.get("model", {}).get("configured"):
            print("BLOCKED: real model API key/model ID is not configured. No run was submitted.")
            return 2
        if not health.get("sandbox", {}).get("available"):
            print("BLOCKED: verified Docker prerequisites are unavailable. No run was submitted.")
            return 2
        if args.resume:
            existing = api_request(base, "/runs/" + args.resume)
            if existing.get("mode") != "live":
                print("BLOCKED: this script cannot relabel or resume a mock run as live.")
                return 2
            if existing.get("status") not in {"failed", "blocked"}:
                print("BLOCKED: only a failed/blocked live run can be resumed.")
                return 2
            run = api_request(base, "/runs/" + args.resume + "/resume", {})
        else:
            run = api_request(base, "/runs", {"requirement": args.requirement, "workflow_id": args.workflow, "mode": "live"})
        run_id = run["id"]
        print(f"Submitted live run {run_id}. Candidate changes will NOT be applied.")
        last_marker, timed_out = None, False
        while True:
            detail = api_request(base, "/runs/" + run_id)
            marker = (detail.get("status"), detail.get("attempt"), len(detail.get("steps", [])))
            if marker != last_marker:
                print(f"status={marker[0]} repair_round={marker[1]} steps={marker[2]} model_requests={detail.get('model_calls', 0)}")
                last_marker = marker
            if detail.get("status") not in {"queued", "running"}:
                break
            if time.monotonic() - started >= args.timeout:
                timed_out = True
                break
            time.sleep(1)
        output = args.output or ROOT / ".agentforge" / "evidence" / f"live-{run_id}.json"
        settings = Runtime(ROOT, ROOT / ".agentforge").settings
        outcome = extract_outcome(detail)
        evidence = {"captured_at": datetime.now(timezone.utc).isoformat(), "source": "actual local API/SQLite, live mode",
                    "observation_timed_out": timed_out, "elapsed_seconds": round(time.monotonic() - started, 2),
                    "outcome": outcome, "health_at_start": health, "run": detail}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(redact(evidence, settings.get("api_key", "")), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"run_id": run_id, "status": detail.get("status"), "evidence": str(output), **outcome}, ensure_ascii=False, indent=2))
        if timed_out:
            print("Observation timed out; the backend may still be executing. Do not start a duplicate run.")
            return 4
        if not outcome["workflow_succeeded"] or not outcome["real_isolated_test_passed"]:
            return 2
        if args.require_repair and not outcome["repair_observed"]:
            return 3
        return 0
    except (RuntimeError, KeyError, ValueError) as exc:
        print("ERROR: " + str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
