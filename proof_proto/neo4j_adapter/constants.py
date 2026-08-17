from __future__ import annotations

from typing import Set

# Protocol status enums. The commit gate validates against these.

STATE_STATUSES = {"open", "closed", "tainted", "reopened"}
MOVE_STATUSES = {"queued", "open", "leased", "refuted", "dominated", "exhausted", "closed"}
CLAIM_STATUSES = {
    "conjectural", "empirical", "provisional", "critic_accepted",
    "lean_verified", "refuted", "retracted", "stale",
}
ATTEMPT_STATUSES = {"pending", "supported", "critic_accepted", "refuted", "retracted"}


def _check(value: str, allowed: Set[str], label: str) -> None:
    if value not in allowed:
        raise ValueError(
            f"invalid {label} {value!r}; expected one of {sorted(allowed)}"
        )


def _edge_id(event_id: str, kind: str) -> str:
    return f"{event_id}-{kind}" if kind else event_id
