"""Read-only audit of every autonomous run and its reproducibility evidence.

Does not call models, start Docker, import candidates, instantiate Store, or modify
the input database. The sole write is a new explicitly named JSON audit report.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.runtime import Runtime


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _contained(base, relative):
    path = base / relative
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Evidence path leaves its run directory")
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Evidence path uses a link or junction")
        if item == base:
            break
    return path


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_database(data_dir):
    database = data_dir / "agentforge.sqlite3"
    if not database.is_file():
        raise ValueError("Input data directory has no agentforge.sqlite3")
    # A read transaction gives one consistent SQLite/WAL snapshot without writes.
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        runs = [_read_row(r[0]) for r in connection.execute("SELECT body FROM runs ORDER BY rowid")]
        result = {"runs": runs, "events": {}, "steps": {}, "snapshots": {}}
        for table in ("events", "steps", "snapshots"):
            for run_id, body in connection.execute(f"SELECT run_id,body FROM {table} ORDER BY rowid"):
                result[table].setdefault(run_id, []).append(_read_row(body))
        return result
    finally:
        connection.close()


def _read_row(body):
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Database evidence row is not an object")
    return value


def _acceptance_parts(source, depth=0):
    """Read the frozen suite inventory as AST data; never execute/import it."""
    if not source:
        return []
    if not isinstance(source, str) or depth > 8 or len(source.encode()) > 65536:
        raise ValueError("Invalid or deeply nested acceptance inventory")
    tree = ast.parse(source)
    manifests = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "_AGENTFORGE_SUITE_PARTS" for t in n.targets)]
    if manifests:
        if len(manifests) != 1:
            raise ValueError("Ambiguous acceptance manifest")
        parts = ast.literal_eval(manifests[0].value)
        if not isinstance(parts, list) or not 1 <= len(parts) <= 64:
            raise ValueError("Invalid acceptance parts")
        result = []
        for part in parts:
            if not isinstance(part, dict) or set(part) != {"source", "count"}:
                raise ValueError("Invalid acceptance part")
            nested = _acceptance_parts(part["source"], depth + 1)
            if sum(p["count"] for p in nested) != part["count"]:
                raise ValueError("Acceptance part count differs from source")
            result.extend(nested)
        return result
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if len({c.name for c in classes}) != len(classes):
        raise ValueError("Duplicate acceptance class names")
    count = 0
    for cls in classes:
        if any(isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name)
               and b.value.id == "unittest" and b.attr == "TestCase" for b in cls.bases):
            names = [n.name for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate acceptance method names")
            count += len(names)
    if not count:
        raise ValueError("No statically inventoried acceptance tests")
    return [{"source": source, "count": count}]


def _request_context(record):
    return json.loads(record["request"]["messages"][1]["content"])["context"]


def _web_runtime(run, bundle, records, check):
    """Inspect a frozen runtime as data; no dependency/preview code is imported."""
    project = run.get("project_context") or {}
    declared = project.get("web") if isinstance(project, dict) else None
    bundled = bundle.get("provenance", {}).get("web")
    if declared is not None and bundle:
        check(declared == bundled, "Web project runtime differs from frozen bundle provenance")
    web = declared if declared is not None else bundled
    if web is None:
        return None
    fields = {"schema", "entrypoint", "dependency_id", "image_id", "lock_sha256", "requirements"}
    valid = isinstance(web, dict) and set(web) == fields and web.get("schema") == "python-web-v1"
    check(valid, "Web runtime has an unsupported or incomplete schema")
    if not valid:
        return None
    valid_entry = (isinstance(web["entrypoint"], str) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_/]*\.py", web["entrypoint"]))
                   and all(part not in ("", ".", "..") for part in web["entrypoint"].split("/")))
    check(valid_entry, "Web entrypoint is not a bounded Python project path")
    check(all(isinstance(web[k], str) and bool(re.fullmatch(r"[a-f0-9]{64}", web[k])) for k in ("dependency_id", "lock_sha256")),
          "Web dependency or lock digest is malformed")
    check(isinstance(web["image_id"], str) and bool(re.fullmatch(r"sha256:[a-f0-9]{64}", web["image_id"])), "Web runtime has no pinned Docker image")
    requirements = web["requirements"]
    check(isinstance(requirements, list) and 1 <= len(requirements) <= 16 and all(
        isinstance(item, str) and len(item) <= 160 and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+-]*", item))
        for item in requirements), "Web direct dependencies are not exact bounded package pins")
    files = (bundle or project).get("files", {})
    check(valid_entry and web["entrypoint"] in files, "Frozen Web entrypoint is absent from the imported source")
    expected_hash = digest(canonical(web))
    for record in records:
        try:
            check(_request_context(record).get("web") == web, "Web model request did not receive the frozen runtime")
        except (KeyError, ValueError, TypeError, IndexError):
            check(False, "Web model request has no readable runtime context")
    for review in run.get("quality_reviews", []):
        check(review.get("web_runtime_sha256") == expected_hash, "Web semantic review used a different dependency/runtime context")
    if run.get("verified_hash"):
        check(run.get("verified_runtime_sha256") == expected_hash, "Web verified source is not bound to the frozen runtime")
    return web


def _project_contract(run, output, check):
    """Compare a project bundle with its imported revision plus accepted additions."""
    revision, bundle = run["project_context"], run["bundle"]
    original, added = revision["files"], output["files"]
    check(not ({p.casefold() for p in original} & {p.casefold() for p in added}),
          "Project intake overwrote an imported source path")
    check(bundle.get("files") == {**original, **added}, "Project bundle differs from imported source plus accepted new scaffolds")
    old_parts = _acceptance_parts(revision.get("acceptance", ""))
    new_parts = _acceptance_parts(output["acceptance"])
    actual_parts = _acceptance_parts(bundle["acceptance"])
    check(actual_parts == old_parts + new_parts, "Project acceptance lost or changed regression/new suite source")
    manifest = {"regression_test_count": sum(p["count"] for p in old_parts),
                "new_test_count": sum(p["count"] for p in new_parts),
                "total_test_count": sum(p["count"] for p in actual_parts),
                "parts": [{"sha256": digest(p["source"]), "count": p["count"],
                           "origin": "regression" if i < len(old_parts) else "new_requirement"}
                          for i, p in enumerate(old_parts + new_parts)]}
    check(3 <= manifest["new_test_count"] <= 12, "Project intake does not contain 3 to 12 new tests")
    check(run.get("acceptance_manifest") == manifest, "Project acceptance manifest differs from independently inventoried suites")
    check(run.get("intake", {}).get("regression_manifest") == manifest, "Project intake regression manifest differs from frozen acceptance")


def _project_configuration(run, records, events, check):
    revision = run["project_context"]
    check(digest(canonical(revision["files"])) == revision.get("source_sha256"), "Imported project source hash mismatch")
    check(run.get("project_id") == revision.get("project_id") and run.get("base_revision_id") == revision.get("id"),
          "Run project identity differs from frozen revision")
    profiles, documents = run.get("profile_snapshot", []), run.get("knowledge_snapshot", [])
    check(digest(canonical({"profiles": profiles, "knowledge": documents})) == run.get("configuration_sha256"), "Frozen project configuration hash mismatch")
    profile_by_id, doc_by_id = {p["id"]: p for p in profiles}, {d["id"]: d for d in documents}
    check(len(profile_by_id) == len(profiles) and len(doc_by_id) == len(documents), "Duplicate frozen configuration identities")
    for profile in profiles:
        business = {k: profile[k] for k in ("name", "description", "prompt", "capabilities", "enabled")}
        check(digest(canonical(business)) == profile.get("sha256"), "Frozen profile version content hash mismatch")
    for doc in documents:
        check(digest(canonical({k: doc[k] for k in ("title", "content")})) == doc.get("sha256"), "Frozen knowledge version content hash mismatch")
    frozen = [e.get("data", {}) for e in events if e.get("type") == "project_revision_frozen"]
    expected = {"project_id": revision["project_id"], "revision_id": revision["id"], "number": revision["number"],
                "source_sha256": revision["source_sha256"], "configuration_sha256": run.get("configuration_sha256"),
                "profiles": [{k: p[k] for k in ("id", "version", "sha256", "enabled")} for p in profiles],
                "knowledge": [{k: d[k] for k in ("id", "version", "sha256")} for d in documents]}
    if revision.get("web"):
        expected.update(web_runtime_sha256=digest(canonical(revision["web"])), dependency_id=revision["web"]["dependency_id"])
    check(frozen == [expected], "Frozen project/configuration event differs from run snapshot")
    tasks = {t["id"]: t for t in run.get("task_graph", [])}
    for record in records:
        context = _request_context(record)
        configuration = context["configuration"]
        check(record.get("configuration_sha256") == run.get("configuration_sha256"), "Request configuration hash differs from frozen project configuration")
        check(record.get("request", {}).get("model") == run.get("model_config", {}).get("model"), "Project request changed the frozen model")
        if record.get("role") == "intake":
            check(context.get("project") == revision["files"], "Project intake request did not receive imported source")
        expected_catalog = [{k: p[k] for k in ("id", "version", "name", "description", "capabilities", "sha256", "enabled")} for p in profiles]
        check(configuration.get("profiles") == expected_catalog, "Model request profile catalog differs from frozen configuration")
        selected = configuration.get("selected_profile")
        assigned = tasks.get(record.get("task_id"), {}) if record.get("role") == "worker" else {}
        expected_profile = profile_by_id.get(assigned.get("profile_id"))
        check(selected == expected_profile, "Worker request did not receive its assigned frozen profile")
        if expected_profile:
            check(expected_profile.get("enabled") is True, "Worker used a disabled frozen profile")
            check(assigned.get("profile_version") == expected_profile["version"] and assigned.get("profile_sha256") == expected_profile["sha256"], "Task profile identity differs from frozen version")
        metadata = {k: selected[k] for k in ("id", "version", "sha256")} if selected else None
        check(record.get("profile") == metadata and record.get("profile_model_policy") == "inherits_run_model", "Request profile evidence differs from actual payload")
        matches = configuration.get("knowledge", [])
        check(len(matches) <= 4, "Knowledge retrieval exceeds bounded context count")
        for match in matches:
            doc = doc_by_id.get(match.get("id"), {})
            check(all(match.get(k) == doc.get(k) for k in ("id", "version", "title", "sha256")), "Retrieved knowledge identity differs from frozen version")
            start, excerpt = match.get("excerpt_start"), match.get("excerpt")
            check(type(start) is int and start >= 0 and isinstance(excerpt, str) and len(excerpt) <= 2400
                  and doc.get("content", "")[start:start + len(excerpt)] == excerpt,
                  "Retrieved knowledge excerpt differs from frozen content")
        references = [{k: m[k] for k in ("id", "version", "sha256", "citation")} for m in matches]
        check(record.get("knowledge_references") == references, "Request knowledge references differ from actual payload")
        check(any(e.get("type") == "knowledge_retrieved" and e.get("data", {}).get("role") == record.get("role")
                  and e.get("node_id") == record.get("task_id") and e.get("data", {}).get("matches") == matches for e in events),
              "Request knowledge excerpts lack a matching retrieval event")


def _project_actions(run, records, events, base, counts, check, warning):
    """Replay evidence boundaries, not Python code or model decisions."""
    current = deepcopy_json((run.get("bundle") or {}).get("files", run["project_context"]["files"]))
    current_hash, verified = digest(canonical(current)), None
    active, pending, interrupted = None, None, False
    origin, source_history_complete = "imported_baseline", True
    outcomes = set()
    workers = {r.get("task_id"): r.get("output", {}) for r in records if r.get("role") == "worker" and r.get("status") == "accepted"}
    web = run["project_context"].get("web")
    allowed = {"read_file", "search_project", "http_request" if web else "run_application"}
    seeded = (run.get("bundle") or {}).get("provenance", {}).get("kind") == "seeded_defect"

    def applied(patch, proposal, label):
        nonlocal current, current_hash
        snapshot = _read_json(_contained(base, patch["snapshot"]))
        expected = {**current, **proposal}
        expected_hash = digest(canonical(expected))
        changed = sorted(path for path in proposal if proposal[path] != current.get(path))
        check(snapshot.get("action") == "applied", label + " is not backed by an applied source snapshot")
        check(snapshot.get("files") == expected, label + " source snapshot differs from accepted file proposal")
        check(snapshot.get("before_sha256") == current_hash, label + " snapshot before hash breaks source continuity")
        check(snapshot.get("candidate_sha256") == patch.get("candidate_sha256") == expected_hash, label + " snapshot after hash mismatch")
        check(snapshot.get("changed_paths") == changed, label + " snapshot changed paths mismatch")
        current, current_hash = expected, expected_hash
        return changed

    for event in events:
        kind, data = event.get("type"), event.get("data", {})
        if kind == "controller_decision":
            if interrupted:
                check(False, "Controller continued after a timed out or cancelled project tool")
            if active and active.get("action") in {"edit", "tool"}:
                check(active.get("index") in outcomes, "Project action advanced without a result or explicit rejection")
            active = data
        elif kind == "decision_rejected":
            rejected = data.get("decision", {})
            outcomes.add(rejected.get("index"))
        elif kind == "main_edit":
            counts["main_edits"] += 1
            check(active is not None and active.get("action") == "edit", "Main edit lacks a corresponding controller edit decision")
            if not active or active.get("action") != "edit":
                continue
            check(active.get("index") not in outcomes, "One edit decision produced duplicate main_edit outcomes")
            before = current_hash
            check(data.get("before_sha256") == before, "Main edit before hash differs from current candidate")
            changed = applied(data["patch"], active["files"], "Main edit")
            check(data.get("changed_paths") == changed and data.get("after_sha256") == current_hash,
                  "Main edit event differs from its source snapshot")
            if before != current_hash:
                verified = None
                origin = "natural_candidate"
            outcomes.add(active.get("index"))
        elif kind == "task_result" and data.get("status") == "succeeded":
            task_id = data.get("id") or event.get("node_id")
            proposal = workers.get(task_id, {}).get("files", {})
            patch = data.get("result", {}).get("patch")
            if patch:
                applied(patch, proposal, "Worker edit")
            if proposal:
                verified = None
                origin = "natural_candidate"
        elif kind == "verification":
            check(data.get("candidate_hash") == current_hash, "Verification is not bound to the candidate reconstructed from edits")
            expected_class = "imported_baseline" if origin == "imported_baseline" else "seeded_experiment_candidate" if seeded else "natural_candidate"
            check(data.get("candidate_origin") == origin and data.get("classification") == expected_class,
                  "Project verification origin differs from committed source-edit history")
            verified = current_hash if data.get("status") == "passed" else None
        elif kind == "tool_started":
            counts["project_tools_started"] += 1
            check(pending is None, "Project tool started before the previous tool had a result")
            check(active is not None and active.get("action") == "tool", "Project tool start lacks a controller tool decision")
            check(data.get("tool") in allowed, "Unknown project tool was executed")
            check(data.get("candidate_sha256") == current_hash and data.get("verification_evidence") is False,
                  "Project tool start has a stale source hash or claims acceptance authority")
            if active:
                check(data.get("tool") == active.get("tool") and data.get("arguments") == active.get("arguments"),
                      "Project tool call differs from accepted controller arguments")
            pending = (active, data)
        elif kind in {"tool_result", "tool_interrupted"}:
            check(pending is not None, "Project tool result or interruption lacks a matching start")
            if pending is None:
                continue
            decision, start = pending
            check(all(data.get(k) == start.get(k) for k in ("tool", "arguments", "candidate_sha256")),
                  "Project tool result scope differs from its start")
            check(data.get("candidate_sha256") == current_hash and data.get("verification_evidence") is False,
                  "Project tool result has a stale source hash or claims acceptance authority")
            if kind == "tool_interrupted":
                counts["project_tools_interrupted"] += 1
                check(data.get("status") == "blocked" and data.get("completion_unknown") is True,
                      "Interrupted project tool lacks an explicit unknown-completion state")
                verified, interrupted = None, True
            else:
                counts["project_tools_completed"] += 1
                counts["project_tools_failed"] += int(data.get("status") in {"failed", "blocked"})
                result = data.get("result")
                if result is not None:
                    check(isinstance(result, dict), "Project tool result is not an object")
                    if isinstance(result, dict):
                        check("candidate_sha256" not in result or result["candidate_sha256"] == current_hash,
                              "Project tool receipt belongs to another candidate")
                        check("source_sha256" not in result or result["source_sha256"] == current_hash,
                              "Application execution source hash differs from its tool invocation")
                        check(not result.get("timed_out") and not result.get("cancelled")
                              and result.get("status") not in {"timeout", "timed_out", "cancelled"},
                              "Tool timeout or cancellation was disguised as an ordinary result")
                        if start.get("tool") == "run_application" and result.get("exit_code") == 0 and result.get("status") == "succeeded":
                            counts["successful_application_tools"] += 1
                            check(result.get("isolated") is True and result.get("container_invoked") is True,
                                  "Successful application tool lacks actual isolated execution evidence")
                        if start.get("tool") == "http_request":
                            counts["http_tools_completed"] += 1
                            check(bool(web) and result.get("web_runtime_sha256") == digest(canonical(web))
                                  and result.get("dependency_id") == web.get("dependency_id")
                                  and result.get("lock_sha256") == web.get("lock_sha256")
                                  and result.get("image_id") == web.get("image_id"),
                                  "HTTP tool receipt used a different frozen runtime")
                            check(result.get("test_claim") is False, "HTTP tool receipt claims independent acceptance authority")
                            if result.get("ok") is True:
                                counts["http_responses_received"] += 1
                                status = result.get("status")
                                check(result.get("isolated") is True and type(status) is int and 100 <= status <= 599,
                                      "HTTP response lacks actual isolated HTTP evidence")
                                counts["http_error_responses"] += int(type(status) is int and status >= 400)
                            else:
                                counts["http_transport_failures"] += 1
                                check(result.get("ok") is False and bool(result.get("error")), "HTTP transport failure lacks its failure evidence")
                else:
                    check(data.get("status") == "failed" and bool(data.get("error")), "Project tool result lacks output or an explicit error")
            if decision:
                outcomes.add(decision.get("index"))
            pending = None
        elif kind == "integrity_failure":
            verified, interrupted, source_history_complete = None, True, False
            check(run.get("status") != "succeeded", "Project succeeded despite an unresolved integrity failure")
            warning("Project source integrity failed; last committed edits do not establish subsequent source state")
        elif kind == "terminal":
            check(data.get("status") == run.get("status") and data.get("summary") == run.get("summary"),
                  "Terminal project event differs from final run state")
            check(data.get("publication") == run.get("publication"), "Terminal event omitted or changed project publication result")
    if pending:
        counts["project_tools_unknown"] += 1
        if run.get("status") in {"running", "queued", "blocked", "failed", "needs_human"}:
            warning("Project tool has no committed result; completion remains unknown")
        else:
            check(False, "Successful project run has an uncompleted tool invocation")
    if active and active.get("action") in {"edit", "tool"} and active.get("index") not in outcomes:
        if run.get("status") == "succeeded":
            check(False, "Successful project run ended with an uncommitted edit/tool action")
        else:
            warning("Last project action has no committed outcome; do not infer it completed")
            if active.get("action") == "edit":
                # The host invalidates before applying. Cancellation can occur
                # between that save and the applied snapshot / main_edit event.
                source_history_complete = False
                if run.get("verified_hash") is None:
                    verified = None
    check(run.get("verified_hash") == verified, "Project retained stale verification or treated a tool result as acceptance")
    publication = run.get("publication") or {}
    if publication.get("status") == "saved":
        check(publication.get("source_sha256") == current_hash, "Published project source hash differs from final candidate")
        if web:
            check(publication.get("web_runtime_sha256") == digest(canonical(web))
                  and publication.get("dependency_id") == web.get("dependency_id"),
                  "Published Web version is not bound to the verified dependency/runtime")
    if publication.get("status") == "failed":
        check(any(e.get("type") == "publication_failed" and e.get("data") == publication for e in events),
              "Failed publication lacks its retained failure event")
    return {"replayed_candidate_sha256": current_hash, "source_history_complete": source_history_complete,
            "last_independent_verified_hash": verified,
            "publication": publication, "tool_results_are_acceptance": False}


def deepcopy_json(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def audit(data_dir, secret=""):
    data_dir = Path(data_dir).resolve()
    database = _read_database(data_dir)
    issues, reports = [], []
    aggregate = Counter()
    aggregate_keys = ("runs", "succeeded_runs", "non_successful_runs", "model_calls", "known_tokens",
                      "unknown_usage_calls", "format_retries", "protocol_failed_responses", "transport_failed_requests",
                      "cancelled_requests", "interrupted_requests", "controller_decisions", "decision_rejections",
                      "tasks", "failed_tasks", "verifications", "passed_verifications", "blocked_verifications",
                      "natural_candidate_failures", "input_baseline_failures", "seeded_baseline_failures",
                      "seeded_candidate_failures", "natural_repair_runs", "seeded_repair_runs",
                      "generated_acceptance_candidate_failures", "generated_acceptance_repair_runs",
                      "posthoc_rejected_runs", "quality_rejected_runs",
                      "imported_baseline_verifications", "imported_baseline_failures", "main_edits",
                      "project_tools_started", "project_tools_completed", "project_tools_failed",
                      "project_tools_interrupted", "project_tools_unknown", "successful_application_tools",
                      "web_runs", "http_tools_completed", "http_responses_received", "http_error_responses", "http_transport_failures",
                      "unlinked_validation_results")
    aggregate.update({key: 0 for key in aggregate_keys})

    def issue(run_id, check, severity="error"):
        # Only fixed diagnostic text/path labels are emitted, never secret content.
        issues.append({"run_id": run_id, "check": check, "severity": severity})

    def check(run_id, condition, label):
        if not condition:
            issue(run_id, label)

    # Scan loaded DB JSON even if a credential were split across SQLite pages.
    secret_hits = []
    if secret and secret in json.dumps(database, ensure_ascii=False):
        secret_hits.append("database_rows")

    for run in database["runs"]:
        run_id = run.get("id")
        aggregate["runs"] += 1
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            issue(None, "Invalid run identifier")
            continue
        base = _contained(data_dir, "runs/" + run_id)
        events = database["events"].get(run_id, [])
        steps = database["steps"].get(run_id, [])
        snapshots = database["snapshots"].get(run_id, [])
        project = run.get("project_context") if isinstance(run.get("project_context"), dict) else None
        project_evidence = None
        run_issue_start = len(issues)
        check(run_id, run.get("engine") == "model-directed-v1", "Unexpected orchestration engine revision")
        check(run_id, run.get("mode") == "live", "Run is not labelled live")
        if run.get("status") in {"running", "queued"}:
            issue(run_id, "Run is still active; audit is provisional", "warning")
        counts = Counter({key: 0 for key in aggregate_keys if key not in {"runs", "succeeded_runs", "non_successful_runs"}})
        records, record_files = [], []
        request_dir = _contained(base, "model_requests")
        for path in sorted(request_dir.glob("*.json")):
            try:
                record = _read_json(_contained(base, path.relative_to(base)))
                records.append(record)
                record_files.append({"path": path.relative_to(base).as_posix(), "sha256": digest(path.read_bytes())})
            except (ValueError, OSError, TypeError):
                issue(run_id, "Unreadable model request evidence: " + path.name)
        records.sort(key=lambda value: value.get("sequence", -1))
        check(run_id, [r.get("sequence") for r in records] == list(range(1, len(records) + 1)), "Request sequences are missing or duplicated")
        check(run_id, all(r.get("run_id") == run_id for r in records), "Request evidence belongs to a different run")
        counts["model_calls"] = len(records)
        completed_unknown, pending_count, pending_reserve, charged = 0, 0, 0, 0
        for record in records:
            usage = record.get("usage", {}).get("total_tokens")
            known = record.get("usage_known") is True
            check(run_id, known == (type(usage) is int and usage >= 0), "Request usage_known disagrees with provider token evidence")
            if known:
                counts["known_tokens"] += usage
            if record.get("status") == "in_flight":
                pending_count += 1
                pending_reserve += record.get("reserved_budget_tokens", 0)
                counts["interrupted_requests"] += 1
            else:
                completed_unknown += int(not known)
                charged += record.get("budget_charge_tokens", 0)
                check(run_id, record.get("budget_charge_tokens") == (usage if known else record.get("reserved_budget_tokens")), "Request budget charge is inconsistent")
            counts["format_retries"] += int(record.get("format_retry") is True)
            counts["protocol_failed_responses"] += int(record.get("status") == "protocol_failed")
            counts["transport_failed_requests"] += int(record.get("status") == "transport_failed")
            counts["cancelled_requests"] += int(record.get("status") == "cancelled")
            check(run_id, "reasoning_content" not in record, "Provider reasoning unexpectedly retained as a request field")
            if record.get("status") == "accepted":
                try:
                    check(run_id, json.loads(record["content"]) == record.get("output"), "Accepted model output differs from retained final content")
                except (ValueError, KeyError, TypeError):
                    issue(run_id, "Accepted model request lacks valid JSON final content")
        recovered = any(e.get("type") == "interrupted_recovery" for e in events)
        expected_unknown = completed_unknown + (pending_count if recovered else 0)
        expected_inflight = 0 if recovered else pending_count
        expected_reserved = 0 if recovered else pending_reserve
        expected_charged = charged + (pending_reserve if recovered else 0)
        counts["unknown_usage_calls"] = completed_unknown + pending_count
        metrics = run.get("model_metrics", {})
        expected_metrics = {"model_calls": counts["model_calls"], "tokens": counts["known_tokens"],
                            "contract_retries": counts["format_retries"], "unknown_usage_calls": expected_unknown,
                            "in_flight": expected_inflight, "reserved_tokens": expected_reserved,
                            "budget_tokens": expected_charged,
                            "metrics_complete": expected_unknown == 0 and expected_inflight == 0}
        for key, value in expected_metrics.items():
            default = True if key == "metrics_complete" else 0
            check(run_id, metrics.get(key, default) == value, "Persisted model metric mismatch: " + key)
        starts = [e for e in events if e.get("type") == "model_request"]
        check(run_id, sorted(e.get("data", {}).get("sequence", -1) for e in starts) == list(range(1, len(records) + 1)), "Model request events do not cover every attempted request")

        decisions = run.get("controller_decisions", [])
        decision_events = [e.get("data") for e in events if e.get("type") == "controller_decision"]
        check(run_id, decisions == decision_events, "Controller decisions disagree with append-only events")
        check(run_id, [d.get("index") for d in decisions] == list(range(1, len(decisions) + 1)), "Controller decision sequence is incomplete")
        accepted_main = [r.get("output") for r in records if r.get("role") == "main" and r.get("status") == "accepted"]
        decision_outputs = [{k: v for k, v in d.items() if k not in {"index", "timestamp"}} for d in decisions]
        check(run_id, accepted_main[:len(decisions)] == decision_outputs, "Controller decisions are not backed by accepted main-Agent requests")
        if len(accepted_main) > len(decisions) and run.get("status") == "blocked":
            issue(run_id, "Accepted main response was interrupted before decision commit", "warning")
        else:
            check(run_id, len(accepted_main) == len(decisions), "Accepted main response count differs from committed decisions")
        counts["controller_decisions"] = len(decisions)
        counts["decision_rejections"] = sum(e.get("type") == "decision_rejected" for e in events)
        graph = run.get("task_graph", [])
        by_id = {t.get("id"): t for t in graph}
        check(run_id, len(by_id) == len(graph), "Task graph contains duplicate IDs")
        step_by_id = {s.get("id"): s for s in steps}
        expected_step_ids = {t["id"] for t in graph if t.get("status") != "pending"}
        check(run_id, set(step_by_id) == expected_step_ids, "Persisted steps do not cover exactly all started/cancelled tasks")
        for task in graph:
            if task.get("status") != "pending":
                check(run_id, step_by_id.get(task["id"]) == task, "Task and step evidence differ: " + task["id"])
            if task.get("status") == "succeeded":
                check(run_id, any(r.get("task_id") == task["id"] and r.get("role") == "worker" and r.get("status") == "accepted" for r in records), "Successful task lacks an accepted worker request: " + task["id"])
        counts["tasks"] = len(graph)
        counts["failed_tasks"] = sum(t.get("status") == "failed" for t in graph)

        hashes = {}
        manifest = None
        bundle = run.get("bundle") if isinstance(run.get("bundle"), dict) else {}
        web = _web_runtime(run, bundle, records, lambda condition, label: check(run_id, condition, label))
        counts["web_runs"] = int(web is not None)
        intake = run.get("intake") if isinstance(run.get("intake"), dict) else None
        unprepared_intake = (intake is not None and not bundle and not graph and not decisions
                             and not run.get("verifications"))
        intake_records = [record for record in records if record.get("role") == "intake"
                          and record.get("status") == "accepted"]
        if intake and intake.get("status") in ("completed", "needs_input"):
            check(run_id, len(intake_records) >= 1, "Completed intake lacks an accepted intake response")
            if intake_records:
                output = intake_records[-1].get("output", {})
                # A ready contract may later need input if its scaffold already
                # passes the generated suite; retain the original ready response.
                contract_rejected = (run.get("contract_review") or {}).get("passed") is False
                expected_status = "ready" if bundle or contract_rejected else "needs_input"
                check(run_id, output.get("status") == expected_status, "Intake status differs from accepted model response")
                if bundle:
                    if project is not None:
                        try:
                            _project_contract(run, output, lambda condition, label: check(run_id, condition, label))
                        except (ValueError, KeyError, TypeError, SyntaxError, AttributeError):
                            issue(run_id, "Frozen project intake/regression contract cannot be independently inventoried")
                    else:
                        check(run_id, bundle.get("files") == output.get("files") and bundle.get("acceptance") == output.get("acceptance"),
                              "Frozen bundle differs from accepted intake scaffold or tests")
                    check(run_id, intake.get("specification") == output.get("specification"), "Frozen specification differs from intake response")
                    check(run_id, intake.get("acceptance_origin") == "model_generated"
                          and bundle.get("provenance", {}).get("acceptance_origin") == "model_generated",
                          "Generated acceptance provenance is missing")
        if "intake_proposals" in run:
            proposed_outputs = [{k: v for k, v in proposal.items() if k not in {"index", "timestamp"}}
                                for proposal in run["intake_proposals"]]
            check(run_id, proposed_outputs == [record.get("output") for record in intake_records],
                  "Intake proposal history differs from accepted model responses")
        for plural, singular, role in (("contract_reviews", "contract_review", "contract_review"),
                                       ("quality_reviews", "quality_review", "final_review")):
            if plural not in run:
                continue  # Historical adapter versions had no semantic review.
            reviews = run[plural]
            review_records = [record for record in records if record.get("role") == role and record.get("status") == "accepted"]
            check(run_id, reviews == [event.get("data") for event in events if event.get("type") == singular],
                  "Review history disagrees with append-only events: " + singular)
            check(run_id, len(reviews) == len(review_records), "Review history lacks corresponding model responses: " + singular)
            for review, record in zip(reviews, review_records):
                check(run_id, {key: review.get(key) for key in ("passed", "issues", "summary")} == record.get("output"),
                      "Review verdict differs from accepted model output: " + singular)
                try:
                    context = json.loads(record["request"]["messages"][1]["content"])["context"]
                    check(run_id, context.get("original_requirement") == run.get("requirement"),
                          "Review did not receive the original requirement: " + singular)
                    if role == "contract_review":
                        proposed = context["proposed_contract"]
                        check(run_id, digest(json.dumps(proposed, ensure_ascii=False, sort_keys=True)) == review.get("proposal_sha256"),
                              "Contract review was bound to a different proposal")
                        proposal_index = review.get("index")
                        check(run_id, type(proposal_index) is int and 1 <= proposal_index <= len(intake_records)
                              and proposed == intake_records[proposal_index - 1].get("output"),
                              "Contract review proposal differs from its accepted intake proposal")
                    else:
                        check(run_id, digest(canonical(context["project"])) == review.get("candidate_hash"),
                              "Semantic review was bound to a different candidate")
                except (KeyError, TypeError, ValueError, IndexError):
                    issue(run_id, "Review request context cannot establish its binding: " + singular)
            if run.get(singular) is not None:
                check(run_id, run[singular] in reviews, "Current review is absent from retained history: " + singular)
        if bundle and "contract_reviews" in run:
            check(run_id, (run.get("contract_review") or {}).get("passed") is True,
                  "Generated contract was frozen without a passing consistency review")
        if run.get("status") == "succeeded" and "quality_reviews" in run:
            review = run.get("quality_review") or {}
            check(run_id, review.get("passed") is True and review.get("candidate_hash") == run.get("verified_hash"),
                  "Successful chat run lacks a passing semantic review of the verified candidate")
        if project is not None:
            try:
                _project_configuration(run, records, events, lambda condition, label: check(run_id, condition, label))
            except (ValueError, KeyError, TypeError, AttributeError):
                issue(run_id, "Project configuration or request context evidence is malformed or incomplete")
        try:
            if not unprepared_intake:
                expected_run_bundle = digest(json.dumps(run["bundle"], ensure_ascii=False, sort_keys=True))
                hashes["run_bundle_sha256"] = expected_run_bundle
                check(run_id, run.get("bundle_sha256") == expected_run_bundle, "Run bundle hash differs from frozen DB bundle")
            if (base / "autonomous_manifest.json").is_file():
                manifest = _read_json(_contained(base, "autonomous_manifest.json"))
                frozen = _read_json(_contained(base, "bundle.json"))
                check(run_id, frozen == run["bundle"], "Disk task bundle differs from frozen DB bundle")
                check(run_id, digest(canonical(frozen)) == manifest["bundle_sha256"], "Sandbox bundle hash mismatch")
                suite = _contained(base, "trusted/test_acceptance.py").read_text(encoding="utf-8")
                check(run_id, digest(suite) == manifest["acceptance_sha256"], "Frozen acceptance suite hash mismatch")
                check(run_id, suite == frozen["acceptance"], "Acceptance suite differs from frozen task bundle")
                check(run_id, digest(canonical(frozen["files"])) == manifest["baseline_sha256"], "Baseline source hash mismatch")
                if web:
                    check(run_id, manifest.get("schema") == "autonomous-python-web-v1" and manifest.get("web") == web
                          and manifest.get("web_runtime_sha256") == digest(canonical(web)), "Web sandbox manifest runtime binding differs from the run")
                    check(run_id, set(manifest.get("runner_sha256", {})) == {"runner.py", "web_bridge.py", "serve.py"},
                          "Web sandbox manifest omits its trusted HTTP runner/bridge/server launcher")
                    hashes.update(web_runtime_sha256=digest(canonical(web)), dependency_id=web["dependency_id"], lock_sha256=web["lock_sha256"])
                else:
                    check(run_id, not manifest.get("web") and not manifest.get("web_runtime_sha256"), "Non-Web run acquired an unexplained Web runtime")
                for filename, expected in manifest["runner_sha256"].items():
                    runner = _contained(base, "runner/" + filename)
                    check(run_id, digest(runner.read_text(encoding="utf-8")) == expected, "Frozen runner hash mismatch: " + filename)
                candidate = {}
                for path in sorted(_contained(base, "candidate").rglob("*")):
                    safe = _contained(base, path.relative_to(base))
                    if safe.is_file():
                        check(run_id, safe.stat().st_nlink == 1, "Candidate file is a hard link")
                        candidate[safe.relative_to(base / "candidate").as_posix()] = safe.read_text(encoding="utf-8")
                hashes.update(candidate_sha256=digest(canonical(candidate)), acceptance_sha256=digest(suite),
                              sandbox_bundle_sha256=manifest["bundle_sha256"], runner_sha256=manifest["runner_sha256"])
                if run.get("status") == "succeeded":
                    check(run_id, run.get("verified_hash") == hashes["candidate_sha256"], "Successful run candidate differs from independently verified hash")
            elif unprepared_intake and run.get("status") != "succeeded":
                issue(run_id, "Intake ended before any candidate was prepared; request evidence retained", "warning")
            elif records or decisions or run.get("status") == "succeeded":
                issue(run_id, "Executed run is missing its frozen sandbox manifest")
            else:
                issue(run_id, "Run has no prepared candidate; preserved as an unexecuted run", "warning")
            implementation_path = _contained(base, "implementation-manifest.json")
            if implementation_path.exists():
                implementation = _read_json(implementation_path)
                for filename, expected in implementation.items():
                    check(run_id, digest(_contained(base, "implementation/" + filename).read_bytes()) == expected,
                          "Frozen implementation copy hash mismatch: " + filename)
                hashes["implementation_manifest_sha256"] = digest(implementation_path.read_bytes())
                hashes["implementation_files"] = len(implementation)
                if web:
                    required_web_sources = {"sandbox/web/runner.py", "sandbox/web/web_bridge.py", "sandbox/web/serve.py",
                                            "sandbox/web/http_probe.py", "sandbox/web/download_wheels.py", "sandbox/web/Dockerfile",
                                            "backend/agentforge/web_dependencies.py", "backend/agentforge/web_preview.py", "backend/agentforge/web_config.py"}
                    check(run_id, required_web_sources <= set(implementation), "Web implementation snapshot omits its runtime/dependency execution sources")
            else:
                issue(run_id, "No implementation snapshot; API runs require separate source archiving", "error" if web else "warning")
        except (ValueError, OSError, KeyError, TypeError):
            issue(run_id, "Frozen source/runner/manifest evidence is missing or unreadable")

        verifications = run.get("verifications", [])
        check(run_id, verifications == [e.get("data") for e in events if e.get("type") == "verification"], "Verification records disagree with append-only events")
        linked_results = set()
        seeded = bundle.get("provenance", {}).get("kind") == "seeded_defect"
        generated_acceptance = bundle.get("provenance", {}).get("acceptance_origin") == "model_generated"
        counts["posthoc_rejected_runs"] = int((run.get("posthoc_review") or {}).get("passed") is False)
        counts["quality_rejected_runs"] = int((run.get("quality_review") or {}).get("passed") is False)
        for verification in verifications:
            counts["verifications"] += 1
            classification = verification.get("classification")
            origin = verification.get("candidate_origin")
            expected_class = ("imported_baseline" if project is not None and origin == "imported_baseline" else
                              ("seeded_defect_baseline" if seeded else "input_baseline") if origin == "baseline" else
                              ("seeded_experiment_candidate" if seeded else "natural_candidate"))
            check(run_id, classification == expected_class, "Verification failure provenance is inconsistent")
            counts["imported_baseline_verifications"] += int(classification == "imported_baseline")
            status = verification.get("status")
            counts["passed_verifications"] += int(status == "passed")
            counts["blocked_verifications"] += int(status == "blocked")
            if status == "failed":
                category = {"natural_candidate": "natural_candidate_failures", "input_baseline": "input_baseline_failures",
                            "imported_baseline": "imported_baseline_failures",
                            "seeded_defect_baseline": "seeded_baseline_failures", "seeded_experiment_candidate": "seeded_candidate_failures"}.get(classification)
                if category:
                    # AI-generated tests can themselves be wrong. Test failure
                    # alone does not establish a natural implementation defect.
                    counts["generated_acceptance_candidate_failures" if generated_acceptance and category == "natural_candidate_failures" else category] += 1
            if status == "passed":
                check(run_id, verification.get("isolated") is True and verification.get("exit_code") == 0 and type(verification.get("test_count")) is int and verification["test_count"] > 0, "Passed verification lacks successful nonempty isolated test evidence")
                check(run_id, verification.get("container_invoked") is True and bool(re.fullmatch(r"sha256:[a-f0-9]{64}", verification.get("image") or "")), "Passed verification lacks pinned Docker image evidence")
                attestation = verification.get("attestation", {})
                check(run_id, attestation.get("passed") is True and attestation.get("tests") == attestation.get("discovered") == verification.get("test_count"), "Passed verification lacks a complete test attestation")
            if project is not None:
                expected_tests = (run.get("acceptance_manifest") or {}).get("total_test_count")
                check(run_id, verification.get("expected_test_count") == expected_tests, "Project verification expected-test count differs from frozen suite inventory")
                if status == "passed":
                    check(run_id, verification.get("test_count") == expected_tests and not verification.get("discovery_mismatch"),
                          "Project passed verification omitted frozen regression or new tests")
                if classification == "imported_baseline":
                    check(run_id, verification.get("candidate_hash") == digest(canonical(bundle.get("files", {}))),
                          "Imported baseline verification used edited source")
            if manifest:
                check(run_id, verification.get("acceptance_sha256") == manifest["acceptance_sha256"], "Verification used a different acceptance suite")
                check(run_id, verification.get("runner_sha256") == manifest["runner_sha256"], "Verification used a different runner")
            if web:
                check(run_id, verification.get("web_runtime_sha256") == digest(canonical(web))
                      and verification.get("dependency_id") == web["dependency_id"]
                      and verification.get("lock_sha256") == web["lock_sha256"]
                      and verification.get("entrypoint") == web["entrypoint"]
                      and verification.get("verification_kind") == "http", "Web verification used a different frozen runtime")
                if verification.get("container_invoked") or verification.get("image"):
                    check(run_id, verification.get("image") == web["image_id"], "Web verification ran outside its pinned dependency image")
                if status == "failed":
                    receipt = verification.get("attestation", {})
                    check(run_id, verification.get("isolated") is True and verification.get("container_invoked") is True
                          and type(verification.get("test_count")) is int and verification["test_count"] > 0
                          and receipt.get("passed") is False
                          and receipt.get("tests") == receipt.get("discovered") == verification["test_count"],
                          "Failed Web verification lacks actual isolated nonempty test evidence")
                if status == "passed":
                    receipt = verification.get("attestation", {})
                    check(run_id, receipt.get("verification_kind") == "http" and type(receipt.get("http_requests")) is int
                          and 0 < receipt["http_requests"] <= 64 and "startup_error" in receipt
                          and "server_exit_code" in receipt and receipt["startup_error"] is None
                          and receipt["server_exit_code"] is None, "Passed Web verification has no healthy real-HTTP server attestation")
            try:
                relative = verification["result_record"]
                stored_result = _read_json(_contained(base, relative))
                linked_results.add(relative)
                check(run_id, all(verification.get(k) == v for k, v in stored_result.items()), "Validation result file differs from DB verification")
                snapshot = _read_json(_contained(base, verification["snapshot"]))
                snapshot_hash = digest(canonical(snapshot["files"]))
                check(run_id, snapshot_hash == verification.get("candidate_hash") == verification.get("candidate_sha256") == snapshot.get("candidate_sha256"), "Validation-start source snapshot hash mismatch")
            except (ValueError, OSError, KeyError, TypeError):
                issue(run_id, "Verification lacks a complete result/source snapshot record")
        result_files = {p.relative_to(base).as_posix() for p in (base / "evidence").glob("*-validation-result-*.json")}
        unlinked = sorted(result_files - linked_results)
        counts["unlinked_validation_results"] = len(unlinked)
        if unlinked:
            issue(run_id, "Uncommitted validation results retained; inspect interruption evidence", "warning")
        if run.get("status") == "succeeded":
            check(run_id, any(v.get("status") == "passed" and v.get("candidate_hash") == run.get("verified_hash") for v in verifications), "Successful run has no matching passed verification record")
            check(run_id, not any(t.get("status") in {"pending", "running"} for t in graph), "Successful run has unfinished tasks")
            check(run_id, bool(decisions) and decisions[-1].get("action") == "finish", "Successful run lacks a final main-Agent finish decision")
            if web and seeded:
                baseline = verifications[0] if verifications else {}
                check(run_id, baseline.get("status") == "failed" and baseline.get("classification") == "seeded_defect_baseline"
                      and any(v.get("status") == "passed" and v.get("classification") == "seeded_experiment_candidate"
                              and v.get("candidate_hash") != baseline.get("candidate_hash") for v in verifications[1:]),
                      "Successful seeded Web control lacks a failed baseline followed by an edited passing candidate")
        for earlier_index, earlier in enumerate(verifications):
            if earlier.get("status") != "failed":
                continue
            later_success = any(v.get("status") == "passed" and v.get("candidate_hash") != earlier.get("candidate_hash") for v in verifications[earlier_index + 1:])
            if later_success and earlier.get("classification") == "natural_candidate":
                counts["generated_acceptance_repair_runs" if generated_acceptance else "natural_repair_runs"] = 1
            if later_success and earlier.get("classification") in {"seeded_defect_baseline", "seeded_experiment_candidate"}:
                counts["seeded_repair_runs"] = 1

        if project is not None:
            try:
                project_evidence = _project_actions(run, records, events, base, counts,
                    lambda condition, label: check(run_id, condition, label),
                    lambda label: issue(run_id, label, "warning"))
                if manifest and run.get("status") == "succeeded":
                    check(run_id, project_evidence["replayed_candidate_sha256"] == hashes.get("candidate_sha256"),
                          "Project candidate on disk differs from replayed source edits")
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                issue(run_id, "Project edit/tool evidence is missing, malformed, or outside the run directory")

        if secret:
            for path in base.rglob("*"):
                try:
                    safe = _contained(base, path.relative_to(base))
                    if safe.is_file() and secret.encode("utf-8") in safe.read_bytes():
                        secret_hits.append(run_id + "/" + safe.relative_to(base).as_posix())
                except (OSError, ValueError):
                    issue(run_id, "Could not safely scan an evidence file for credential leakage")
        export_path = _contained(base, "evidence.json")
        if export_path.exists():
            try:
                exported = _read_json(export_path)
                check(run_id, exported.get("run") == run, "Final JSON export differs from SQLite run")
                check(run_id, [{k: v for k, v in e.items() if k != "id"} for e in exported.get("events", [])] == events, "Final JSON export events differ from SQLite")
                check(run_id, exported.get("steps") == steps, "Final JSON export steps differ from SQLite")
                check(run_id, [{k: v for k, v in s.items() if k != "id"} for s in exported.get("snapshots", [])] == snapshots, "Final JSON export snapshots differ from SQLite")
                hashes["export_sha256"] = digest(export_path.read_bytes())
            except (OSError, ValueError):
                issue(run_id, "Final JSON export cannot be read")
        else:
            issue(run_id, "No final JSON export; SQLite and request records remain primary evidence",
                  "error" if project is not None and any(e.get("type") == "terminal" for e in events) else "warning")

        aggregate.update(counts)
        aggregate["succeeded_runs" if run.get("status") == "succeeded" else "non_successful_runs"] += 1
        reports.append({"run_id": run_id, "status": run.get("status"), "provenance": bundle.get("provenance"),
                        "intake_status": intake.get("status") if intake else None,
                        "posthoc_review": run.get("posthoc_review"), "quality_review": run.get("quality_review"),
                        "model_config": run.get("model_config"), "budget": run.get("budget"), "model_metrics": metrics,
                        "project_id": run.get("project_id"), "base_revision_id": run.get("base_revision_id"),
                        "project_evidence": project_evidence,
                        "web_runtime": web,
                        "recomputed_metrics": expected_metrics, "counts": dict(counts), "hashes": hashes,
                        "controller_actions": [d.get("action") for d in decisions],
                        "tasks": [{key: t.get(key) for key in ("id", "title", "agent_id", "status", "depends_on", "write_paths")} for t in graph],
                        "verification_results": [{key: v.get(key) for key in ("index", "status", "test_count", "classification", "candidate_hash", "image", "verification_kind", "web_runtime_sha256", "dependency_id", "lock_sha256")} for v in verifications],
                        "request_records": record_files, "unlinked_validation_results": unlinked,
                        "events": len(events), "steps": len(steps), "snapshots": len(snapshots),
                        "passed": not any(i["severity"] == "error" for i in issues[run_issue_start:])})
    if secret_hits:
        issue(None, "Local configured credential detected in evidence; report does not disclose its value")
    report = {"schema": "agentforge-autonomous-audit-v1", "audit_revision": "web-runtime-v3", "data_dir": str(data_dir),
            "read_only_inputs": True, "models_invoked": False, "docker_invoked": False,
            "secret_scan": {"configured_key_available": bool(secret), "passed": not secret_hits,
                            "locations": secret_hits},
            "passed": not any(i["severity"] == "error" for i in issues), "aggregate": dict(aggregate),
            "runs": reports, "issues": issues,
            "limitations": ["Hashes establish internal evidence consistency, not resistance to coordinated malicious evidence rewriting.",
                            "Unknown provider usage remains unknown; budget charge is a conservative estimate, not actual billed tokens.",
                            "No model or candidate execution occurs during this audit; live evidence is inspected, not rerun."]}
    if secret:
        # The report itself must remain safe even when inspecting a leaky archive.
        report = json.loads(json.dumps(report, ensure_ascii=False).replace(secret, "[REDACTED]"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True, help="New JSON file; existing files are never overwritten")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        parser.error("Output already exists; choose a new audit filename")
    if output.is_relative_to(data_dir):
        parser.error("Audit output must be outside the read-only input data directory")
    # Settings are read locally solely for an exact credential-leak check.
    secret = Runtime(ROOT, data_dir).settings.get("api_key", "")
    report = audit(data_dir, secret=secret)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": report["passed"], "output": str(output), "aggregate": report["aggregate"],
                      "issues": len(report["issues"])}, ensure_ascii=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
