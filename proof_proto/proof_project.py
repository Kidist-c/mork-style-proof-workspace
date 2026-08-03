from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProofProject:
    """A lightweight proof workspace inspired by the paper's core ideas.

    The goal is not to implement full formal verification. It is to make the
    persistence, branching, and evidence model tangible in a small, fast prototype.
    """

    def __init__(self, root: str | Path, theorem_kernel: str):
        self.root = Path(root).resolve()
        self.proof_dir = self.root / "proof_store"
        self.artifact_dir = self.root / "artifacts"
        self.snapshot_dir = self.root / "snapshots"
        self.journal_path = self.root / "journal.jsonl"
        self.state_path = self.root / "project_state.json"

        self.proof_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        self.theorem_kernel = theorem_kernel
        self.states: Dict[str, Dict[str, Any]] = {}
        self.claims: Dict[str, Dict[str, Any]] = {}
        self.attempts: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []

        if self.state_path.exists():
            self._load()
            return

        self._append_event(
            "project_init",
            {
                "theorem_kernel": theorem_kernel,
                "created_at": self._now(),
            },
        )
        self._persist_state()

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _persist_state(self) -> None:
        payload = {
            "theorem_kernel": self.theorem_kernel,
            "states": self.states,
            "claims": self.claims,
            "attempts": self.attempts,
            "events": self.events,
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.theorem_kernel = payload.get("theorem_kernel", self.theorem_kernel)
        self.states = payload.get("states", {})
        self.claims = payload.get("claims", {})
        self.attempts = payload.get("attempts", {})
        self.events = payload.get("events", [])

    def _append_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        event = {
            "id": f"evt-{len(self.events) + 1:03d}",
            "type": event_type,
            "timestamp": self._now(),
            "payload": payload,
        }
        self.events.append(event)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        self._persist_state()

    def add_state(self, state_id: str, description: str, parent: Optional[str] = None) -> Dict[str, Any]:
        state = {
            "id": state_id,
            "description": description,
            "parent": parent,
            "status": "open",
        }
        self.states[state_id] = state
        self._append_event("state_added", {"state": state})
        return state

    def add_claim(self, claim_id: str, statement: str, status: str = "conjectural") -> Dict[str, Any]:
        claim = {
            "id": claim_id,
            "statement": statement,
            "status": status,
        }
        self.claims[claim_id] = claim
        self._append_event("claim_added", {"claim": claim})
        return claim

    def record_attempt(
        self,
        attempt_id: str,
        state_id: str,
        move_summary: str,
        worker: str = "explorer",
        note: str = "",
    ) -> Dict[str, Any]:
        attempt = {
            "id": attempt_id,
            "state_id": state_id,
            "move_summary": move_summary,
            "worker": worker,
            "note": note,
            "status": "pending",
        }
        self.attempts[attempt_id] = attempt
        self._append_event("attempt_recorded", {"attempt": attempt})
        return attempt

    def mark_attempt(self, attempt_id: str, status: str, evidence: str = "") -> Dict[str, Any]:
        attempt = self.attempts[attempt_id]
        attempt["status"] = status
        attempt["evidence"] = evidence
        self.attempts[attempt_id] = attempt
        self._append_event("attempt_updated", {"attempt": attempt})
        return attempt

    def write_artifact(self, name: str, content: str, kind: str = "note") -> Dict[str, Any]:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        artifact_path = self.artifact_dir / f"{digest}_{name.replace(' ', '_')}.txt"
        artifact_path.write_text(content, encoding="utf-8")
        artifact = {
            "path": str(artifact_path),
            "kind": kind,
            "sha256": digest,
            "name": name,
        }
        self._append_event("artifact_written", {"artifact": artifact})
        return artifact

    def export_snapshot(self, path: Optional[str | Path] = None) -> Path:
        target = Path(path) if path is not None else self.snapshot_dir / f"snapshot_{len(self.events):03d}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "theorem_kernel": self.theorem_kernel,
            "states": self.states,
            "claims": self.claims,
            "attempts": self.attempts,
            "events": self.events,
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target

    def context_for(self, state_id: str) -> Dict[str, Any]:
        related_attempts = [a for a in self.attempts.values() if a.get("state_id") == state_id]
        return {
            "state": self.states.get(state_id),
            "attempts": related_attempts,
            "claims": list(self.claims.values()),
        }
