"""A frozen, best-effort finishing allowance inside each new run's total budget.

It is a scheduling policy, not an increase in provider quota or a guarantee that
an arbitrary final-review prompt fits. Old records without the policy keep their
original accounting. The model cannot enter the host-owned finishing phase.
"""
from .runtime import RuntimeErrorSafe


REVISION = "finishing-budget-v1"


class FinishingBudgetReached(RuntimeErrorSafe):
    """No development request was sent; its allowance belongs to final checks."""


def freeze_policy(budget):
    return {
        "revision": REVISION,
        "reserved_calls": min(2, budget["max_calls"] // 4),
        "reserved_tokens": min(65536, budget["max_tokens"] // 5),
        "reserved_verifications": int(budget["max_verifications"] >= 2),
        "reserved_steps": int(budget["max_steps"] >= 4),
        "reserved_seconds": min(60, budget["wall_seconds"] // 5),
    }


def policy_for(run):
    policy = run.get("finishing_budget")
    return policy if isinstance(policy, dict) and policy.get("revision") == REVISION else None


def remaining_budget(run, remaining_seconds=None):
    budget, metrics = run["budget"], run.get("model_metrics", {})
    return {
        "calls": budget["max_calls"] - metrics.get("model_calls", 0),
        "tokens": budget["max_tokens"] - metrics.get("budget_tokens", metrics.get("tokens", 0))
                  - metrics.get("reserved_tokens", 0),
        "verifications": budget["max_verifications"] - len(run.get("verifications", [])),
        "steps": budget["max_steps"] - len(run.get("controller_decisions", [])),
        "seconds": remaining_seconds,
    }


def finishing_reason(run, remaining_seconds=None):
    policy = policy_for(run)
    if policy is None:
        return None
    if run.get("finishing_state", {}).get("phase") == "finalizing":
        return "already_finalizing"
    remaining = remaining_budget(run, remaining_seconds)
    for key in ("calls", "tokens", "verifications", "steps", "seconds"):
        reserve = policy["reserved_" + key]
        if reserve and remaining[key] is not None and remaining[key] <= reserve:
            return key + "_reserve"
    return None


def request_block_reason(run, role, metrics, reservation):
    """Called under the adapter's accounting lock, including for format retries."""
    policy = policy_for(run)
    if policy is None:
        return None
    if run.get("finishing_state", {}).get("phase") == "finalizing" and role != "final_review":
        return "development_closed"
    if role == "final_review":
        return None  # The original total request/token gates still apply.
    budget = run["budget"]
    if metrics["model_calls"] + 1 > budget["max_calls"] - policy["reserved_calls"]:
        return "calls_reserve"
    if metrics["budget_tokens"] + metrics["reserved_tokens"] + reservation > budget["max_tokens"] - policy["reserved_tokens"]:
        return "tokens_reserve"
    return None
