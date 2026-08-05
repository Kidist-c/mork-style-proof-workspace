from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from proof_proto.neo4j_adapter import Neo4jAdapter


class ProofProject:
    """A lightweight proof workspace inspired by the paper's core ideas.

    Persistence layer:
      - journal.jsonl         append-only event log — the durability authority
      - project_state.json    lightweight metadata (theorem, event count)
      - Neo4j                 graph authority for states, claims, attempts
                              can be wiped and rebuilt from the journal at any time
    """

    def __init__(
        self,
        root: str | Path,
        theorem_kernel: str,
        neo4j_uri: str = "bolt://localhost:7687",
        neo4j_user: str = "neo4j",
        neo4j_password: str = "proofagent123",
    ):
        self.root = Path(root).resolve()
        self.proof_dir = self.root / "proof_store"
        self.artifact_dir = self.root / "artifacts"
        self.snapshot_dir = self.root / "snapshots"
        self.journal_path = self.root / "journal.jsonl"
        self.state_path = self.root / "project_state.json"

        self.proof_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        # proof_id is the folder name — unique namespace in Neo4j per project
        self.proof_id = self.root.name
        self.theorem_kernel = theorem_kernel
        self.events: List[Dict[str, Any]] = []

        self.graph = Neo4jAdapter(neo4j_uri, neo4j_user, neo4j_password)

        if self.state_path.exists():
            self._load()
            return

        self.graph.init_proof(self.proof_id, theorem_kernel)
        self._append_event(
            "project_init",
            {
                "theorem_kernel": theorem_kernel,
                "created_at": self._now(),
            },
        )
        self._persist_state()

    def close(self) -> None:
        self.graph.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _persist_state(self) -> None:
        # Only journal metadata lives here now — graph data lives in Neo4j
        payload = {
            "theorem_kernel": self.theorem_kernel,
            "proof_id": self.proof_id,
            "events": self.events,
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.theorem_kernel = payload.get("theorem_kernel", self.theorem_kernel)
        self.events = payload.get("events", [])
        # Rebuild Neo4j from the journal — this is the key architectural guarantee:
        # Neo4j can always be reconstructed from the append-only journal
        self.graph.wipe_and_rebuild(self.proof_id, self.events)

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
        state = {"id": state_id, "description": description, "parent": parent, "status": "open"}
        self.graph.add_state(self.proof_id, state_id, description, parent)
        self._append_event("state_added", {"state": state})
        return state

    def add_claim(self, claim_id: str, statement: str, status: str = "conjectural") -> Dict[str, Any]:
        claim = {"id": claim_id, "statement": statement, "status": status}
        self.graph.add_claim(self.proof_id, claim_id, statement, status)
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
        move_summary = move_summary if isinstance(move_summary, str) else json.dumps(move_summary)
        note = note if isinstance(note, str) else json.dumps(note)
        attempt = {"id": attempt_id, "state_id": state_id, "move_summary": move_summary,
                   "worker": worker, "note": note, "status": "pending"}
        self.graph.add_attempt(self.proof_id, attempt_id, state_id, move_summary, worker, note)
        self._append_event("attempt_recorded", {"attempt": attempt})
        return attempt

    def mark_attempt(self, attempt_id: str, status: str, evidence: str = "") -> Dict[str, Any]:
        self.graph.update_attempt(attempt_id, status, evidence)
        attempt = {"id": attempt_id, "status": status, "evidence": evidence}
        self._append_event("attempt_updated", {"attempt": attempt})
        return attempt

    def close_state(self, state_id: str, reason: str = "") -> None:
        self.graph.close_state(state_id, self.proof_id, reason)
        self._append_event("state_closed", {"state_id": state_id, "reason": reason})

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
        # Pull current graph state from Neo4j for the snapshot
        payload = {
            "theorem_kernel": self.theorem_kernel,
            "proof_id": self.proof_id,
            "claims": self.graph.get_all_claims(self.proof_id),
            "events": self.events,
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target

    def context_for(self, state_id: str) -> Dict[str, Any]:
        # Now a real graph traversal instead of a list filter
        return self.graph.context_for(self.proof_id, state_id)
