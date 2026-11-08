"""Explicit new-phase amendment: submit only never-submitted parent slots.

No parent record is rewritten and no submitted run is retried. A claim outside
the parent directory prevents two live continuation batches for the same plan.
"""
from copy import deepcopy
import json
import os
from pathlib import Path

from agentforge.runtime import RuntimeErrorSafe
from evaluate_tasks import canonical, digest, now, verify_frozen

IDENTITY = ("trial", "task_id", "arm", "repetition", "block", "arm_position")
SAME_CONDITIONS = ("schema", "task_order", "task_catalog", "arms", "repetitions", "model_config",
                   "model_settings", "docker_image_id", "limits", "candidate_origin", "fault_injection")
OBSERVER_CHANGES = {"scripts/evaluate_tasks.py", "scripts/evaluate_comparison.py",
                    "scripts/comparison_continuation.py", "scripts/merge_comparison.py"}


def load_parent(root, directory):
    directory = Path(directory).resolve()
    if not directory.is_relative_to((root / ".agentforge/comparisons").resolve()):
        raise RuntimeErrorSafe("Continuation parent must be inside comparisons.")
    raw_plan = (directory / "preregistration.json").read_bytes()
    raw_state = (directory / "state.json").read_bytes()
    plan, state = json.loads(raw_plan), json.loads(raw_state)
    if state.get("status") not in {"blocked", "interrupted"}:
        raise RuntimeErrorSafe("Only a stopped parent is eligible for an explicit new phase.")
    if state.get("preregistration_sha256") != digest(raw_plan):
        raise RuntimeErrorSafe("Parent preregistration hash mismatch.")
    if digest(canonical(plan["source_manifest"])) != plan["source_manifest_sha256"]:
        raise RuntimeErrorSafe("Parent manifest hash mismatch.")
    verify_frozen(root, directory, plan["source_manifest"], compare_current=False)
    intended, observed = plan["trials"], state["trials"]
    if len(observed) != len(intended) or len({t["trial"] for t in observed}) != len(intended):
        raise RuntimeErrorSafe("Parent trial inventory is incomplete or duplicated.")
    selected, retained = [], []
    for expected, actual in zip(intended, observed):
        if any(expected[k] != actual[k] for k in IDENTITY):
            raise RuntimeErrorSafe("Parent trial identity or ordering changed.")
        if actual["state"] == "not_submitted" and actual.get("run_id") is None:
            selected.append({**{k: expected[k] for k in IDENTITY}, "state": "pending", "run_id": None})
        elif actual.get("run_id") and actual["state"] in {"complete", "interrupted"}:
            retained.append(actual["trial"])
        else:
            raise RuntimeErrorSafe("Ambiguous parent submission; cannot continue.")
    if not selected:
        raise RuntimeErrorSafe("No never-submitted slots remain.")
    submitted_ids = [t["run_id"] for t in observed if t.get("run_id")]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise RuntimeErrorSafe("Duplicate parent run IDs; cannot continue.")
    return {"directory": directory, "plan": plan, "state": state, "trials": selected,
            "reference": {"parent_batch": directory.name,
                          "parent_preregistration_sha256": digest(raw_plan),
                          "parent_state_sha256": digest(raw_state),
                          "retained_parent_trials": retained,
                          "selected_trial_ids": [t["trial"] for t in selected]}}


def validate_conditions(parent, plan):
    previous = parent["plan"]
    for key in SAME_CONDITIONS:
        if plan.get(key) != previous.get(key):
            raise RuntimeErrorSafe(f"Continuation changes frozen condition: {key}.")
    if plan["trials"] != parent["trials"]:
        raise RuntimeErrorSafe("Continuation must include exactly the original unsubmitted slots.")
    old, new = previous["source_manifest"], plan["source_manifest"]
    changes = {key: {"before": old.get(key), "after": new.get(key)}
               for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)}
    if set(changes) - OBSERVER_CHANGES:
        raise RuntimeErrorSafe("Continuation changed model, engine, task or other frozen source.")
    return {**deepcopy(parent["reference"]), "source_changes": changes,
            "amendment": "New phase after local observation failure. Only previously unsubmitted slots; submitted failures remain in the original denominator. Observer-only source changes disclosed; temporal phase break is a limitation."}


def verify_parent(parent):
    for filename, field in (("preregistration.json", "parent_preregistration_sha256"),
                            ("state.json", "parent_state_sha256")):
        if digest((parent["directory"] / filename).read_bytes()) != parent["reference"][field]:
            raise RuntimeErrorSafe("Parent evidence changed after continuation registration.")


def claim_continuation(root, parent, directory, preregistration_sha256):
    """Claim is permanent: uncertain interrupted submissions must never repeat."""
    verify_parent(parent)
    folder = root / ".agentforge/comparison-continuation-claims"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (parent["reference"]["parent_preregistration_sha256"] + ".json")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeErrorSafe("This parent already has a claimed continuation; do not resubmit its slots.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"created_at": now(), "parent": parent["reference"], "child_batch": directory.name,
                   "child_preregistration_sha256": preregistration_sha256}, handle, indent=2)
    (directory / "continuation-claim.json").write_bytes(path.read_bytes())
    return {"path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())}
