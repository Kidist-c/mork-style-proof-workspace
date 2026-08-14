from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from proof_proto.neo4j_adapter import Neo4jAdapter


class ProofProject:
    """A lightweight proof workspace inspired by the paper's core ideas.

    Persistence layers:
      - journal.jsonl         append-only event log — the durability authority
      - project_state.json    lightweight metadata (theorem, event count)
      - Neo4j                 graph authority (search/justification/provenance
                              metagraph) — a derived projection that can be
                              wiped and rebuilt from the journal at any time

    Every adapter write is stamped with the event id that caused it, so any
    unauthorized mutation is detectable by diffing the graph against the journal.
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

        # A fresh journal means no committed graph exists yet — wipe any stale
        # Neo4j nodes left under this proof_id so reads never see ghost data.
        self.graph.wipe_and_rebuild(self.proof_id, [])
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
        # Rebuild Neo4j from the journal — the key architectural guarantee:
        # Neo4j can always be reconstructed from the append-only journal
        self.graph.wipe_and_rebuild(self.proof_id, self.events)

    def _append_event(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
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
        return event

    def _next_id(self, prefix: str) -> str:
        # Monotonic, journal-unique id — event count is the shared sequence
        return f"{prefix}-{len(self.events) + 1}"

    # ------------------------------------------------------------------
    # Search DAG — states, moves, subgoals
    # ------------------------------------------------------------------

    def add_state(self, state_id: str, description: str, parent: Optional[str] = None,
                  kind: str = "or", assumptions: str = "") -> Dict[str, Any]:
        state = {"id": state_id, "description": description, "parent": parent,
                 "kind": kind, "status": "open", "assumptions": assumptions}
        event = self._append_event("state_added", {"state": state})
        self.graph.add_state(self.proof_id, state_id, description, parent,
                             kind=kind, assumptions=assumptions, event_id=event["id"])
        return state

    def add_claim(self, claim_id: str, statement: str, status: str = "conjectural") -> Dict[str, Any]:
        claim = {"id": claim_id, "statement": statement, "status": status}
        event = self._append_event("claim_added", {"claim": claim})
        self.graph.add_claim(self.proof_id, claim_id, statement, status, event_id=event["id"])
        return claim

    def add_claim_dependency(self, dependent_claim_id: str, depends_on_claim_id: str) -> None:
        event = self._append_event(
            "claim_dependency_added",
            {"dependent_claim_id": dependent_claim_id, "depends_on_claim_id": depends_on_claim_id},
        )
        self.graph.add_claim_dependency(dependent_claim_id, depends_on_claim_id, self.proof_id, event["id"])

    def update_claim_status(self, claim_id: str, status: str, reason: str = "") -> Dict[str, Any]:
        event = self._append_event(
            "claim_updated",
            {"claim_id": claim_id, "status": status, "reason": reason},
        )
        self.graph.update_claim_status(claim_id, status, self.proof_id, event["id"], reason)
        return {"id": claim_id, "status": status, "reason": reason}

    def propagate_taint(self, claim_id: str, reason: str = "") -> Dict[str, Any]:
        """Journal a refutation + taint cascade and project it into the graph."""
        event = self._append_event(
            "taint_propagated", {"claim_id": claim_id, "reason": reason},
        )
        return self.graph.propagate_taint(self.proof_id, claim_id, event["id"], reason)

    def add_move(
        self,
        state_id: str,
        move_summary: str,
        kind: str = "reduction",
        note: str = "",
        move_id: Optional[str] = None,
        status: str = "open",
    ) -> Dict[str, Any]:
        move_summary = move_summary if isinstance(move_summary, str) else json.dumps(move_summary)
        note = note if isinstance(note, str) else json.dumps(note)
        if move_id is None:
            move_id = self._next_id("move")
        move = {
            "id": move_id,
            "state_id": state_id,
            "move_summary": move_summary,
            "kind": kind,
            "note": note,
            "status": status,
        }
        event = self._append_event("move_added", {"move": move})
        self.graph.add_move(self.proof_id, move_id, state_id, move_summary, kind, note,
                            event_id=event["id"], status=status)
        return move

    def add_subgoal(
        self,
        move_id: str,
        description: str,
        parent_state_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        description = description if isinstance(description, str) else json.dumps(description)
        subgoal_id = self._next_id("sub")
        subgoal = {"id": subgoal_id, "description": description, "parent": parent_state_id}
        event = self._append_event("subgoal_added", {"move_id": move_id, "subgoal": subgoal})
        self.graph.add_required_subgoal(self.proof_id, move_id, subgoal_id, description,
                                        parent_state_id, event_id=event["id"])
        return subgoal

    def update_move_status(self, move_id: str, status: str) -> Dict[str, Any]:
        move = {"id": move_id, "status": status}
        event = self._append_event("move_updated", {"move": move})
        self.graph.update_move_status(move_id, status, self.proof_id, event["id"])
        return move

    # ------------------------------------------------------------------
    # Provenance DAG — attempts, routes, artifacts, contexts
    # ------------------------------------------------------------------

    def record_attempt(
        self,
        attempt_id: str,
        state_id: str,
        move_summary: str,
        worker: str = "explorer",
        note: str = "",
        move_id: Optional[str] = None,
        route_id: Optional[str] = None,
        model_persona: str = "",
        disposition: str = "",
        result_relation: str = "",
    ) -> Dict[str, Any]:
        move_summary = move_summary if isinstance(move_summary, str) else json.dumps(move_summary)
        note = note if isinstance(note, str) else json.dumps(note)
        attempt = {
            "id": attempt_id, "state_id": state_id, "move_summary": move_summary,
            "worker": worker, "note": note, "status": "pending",
            "route_id": route_id, "model_persona": model_persona,
            "disposition": disposition, "result_relation": result_relation,
        }
        if move_id:
            attempt["move_id"] = move_id
        event = self._append_event("attempt_recorded", {"attempt": attempt})
        self.graph.add_attempt(
            self.proof_id, attempt_id, state_id, move_summary, worker, note, move_id,
            event_id=event["id"], route_id=route_id, model_persona=model_persona,
            disposition=disposition, result_relation=result_relation,
        )
        return attempt

    def mark_attempt(self, attempt_id: str, status: str, evidence: str = "") -> Dict[str, Any]:
        attempt = {"id": attempt_id, "status": status, "evidence": evidence}
        event = self._append_event("attempt_updated", {"attempt": attempt})
        self.graph.update_attempt(attempt_id, status, evidence, self.proof_id, event["id"])
        return attempt

    def close_state(self, state_id: str, reason: str = "") -> None:
        event = self._append_event("state_closed", {"state_id": state_id, "reason": reason})
        self.graph.close_state(state_id, self.proof_id, reason, event["id"])

    def reopen_state(self, state_id: str, reason: str = "") -> None:
        event = self._append_event("state_reopened", {"state_id": state_id, "reason": reason})
        self.graph.reopen_state(self.proof_id, state_id, reason, event["id"])

    def add_route(self, route_id: str, display_path: str) -> Dict[str, Any]:
        route = {"id": route_id, "display_path": display_path}
        event = self._append_event("route_added", {"route": route})
        self.graph.add_route(self.proof_id, route_id, display_path, event["id"])
        return route

    def link_attempt_route(self, attempt_id: str, route_id: str) -> None:
        event = self._append_event("attempt_route_linked", {"attempt_id": attempt_id, "route_id": route_id})
        self.graph.link_attempt_route(self.proof_id, attempt_id, route_id, event["id"])

    def add_context_packet(
        self,
        context_id: str,
        packet_hash: str = "",
        compiler_version: str = "0.1.0",
        token_budget: int = 0,
        token_count: int = 0,
    ) -> Dict[str, Any]:
        context = {
            "id": context_id, "packet_hash": packet_hash, "compiler_version": compiler_version,
            "token_budget": token_budget, "token_count": token_count,
        }
        event = self._append_event("context_added", {"context": context})
        self.graph.add_context(self.proof_id, context_id, packet_hash, compiler_version,
                               token_budget, token_count, event["id"])
        return context

    def link_attempt_context(self, attempt_id: str, context_id: str) -> None:
        event = self._append_event("attempt_context_linked", {"attempt_id": attempt_id, "context_id": context_id})
        self.graph.link_attempt_context(self.proof_id, attempt_id, context_id, event["id"])

    def link_produced_claim(self, attempt_id: str, claim_id: str) -> None:
        event = self._append_event("claim_produced", {"attempt_id": attempt_id, "claim_id": claim_id})
        self.graph.link_produced_claim(self.proof_id, attempt_id, claim_id, event["id"])

    def link_state_claim(self, state_id: str, claim_id: str) -> None:
        event = self._append_event("state_claim_link_added", {"state_id": state_id, "claim_id": claim_id})
        self.graph.link_state_claim(self.proof_id, state_id, claim_id, event["id"])

    def write_artifact(self, name: str, content: str, kind: str = "note", attempt_id: str = "") -> Dict[str, Any]:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        artifact_path = self.artifact_dir / f"{digest}_{name.replace(' ', '_')}.txt"
        artifact_path.write_text(content, encoding="utf-8")
        artifact_id = self._next_id("art")
        artifact = {
            "id": artifact_id,
            "path": str(artifact_path),
            "kind": kind,
            "sha256": digest,
            "name": name,
        }
        event = self._append_event("artifact_added", {"artifact": artifact})
        self.graph.add_artifact(self.proof_id, artifact_id, kind, "", digest,
                                artifact_path.name, event["id"])
        if attempt_id:
            self.link_artifact(attempt_id, artifact_id)
        return artifact

    def link_artifact(self, attempt_id: str, artifact_id: str) -> None:
        event = self._append_event("artifact_linked", {"attempt_id": attempt_id, "artifact_id": artifact_id})
        self.graph.link_artifact(self.proof_id, attempt_id, artifact_id, event["id"])

    # ------------------------------------------------------------------
    # Independent checks — critics, experiments, verification
    # ------------------------------------------------------------------

    def add_critique(self, attempt_id: str, verdict: str, reason: str = "", critic_worker: str = "critic") -> Dict[str, Any]:
        critique_id = self._next_id("crq")
        critique = {"id": critique_id, "verdict": verdict, "reason": reason, "critic_worker": critic_worker}
        event = self._append_event("critique_added", {"attempt_id": attempt_id, "critique": critique})
        self.graph.add_critique(self.proof_id, critique_id, attempt_id, verdict, reason, critic_worker, event["id"])
        return critique

    def add_experiment(self, attempt_id: str, question: str, status: str = "ran") -> Dict[str, Any]:
        experiment_id = self._next_id("exp")
        experiment = {"id": experiment_id, "question": question, "status": status}
        event = self._append_event("experiment_added", {"attempt_id": attempt_id, "experiment": experiment})
        self.graph.add_experiment(self.proof_id, experiment_id, attempt_id, question, status, event["id"])
        return experiment

    def add_verification(
        self, attempt_id: str, claim_id: str, kind: str = "lean",
        status: str = "pending", lean_name: str = "", toolchain_hash: str = "",
    ) -> Dict[str, Any]:
        verification_id = self._next_id("ver")
        verification = {
            "id": verification_id, "kind": kind, "status": status,
            "lean_name": lean_name, "toolchain_hash": toolchain_hash,
        }
        event = self._append_event(
            "verification_added",
            {"attempt_id": attempt_id, "claim_id": claim_id, "verification": verification},
        )
        self.graph.add_verification(self.proof_id, verification_id, attempt_id, claim_id,
                                    kind, status, lean_name, toolchain_hash, event["id"])
        return verification

    # ------------------------------------------------------------------
    # Semantic relations (bypasses, supersedes, ...) + speculative layer
    # ------------------------------------------------------------------

    def add_relation(self, rel: str, from_id: str, to_id: str, route_id: str = "") -> None:
        event = self._append_event(
            "relation_added",
            {"rel": rel, "from_id": from_id, "to_id": to_id, "route_id": route_id},
        )
        self.graph.add_relation(self.proof_id, rel, from_id, to_id, event["id"], route_id)

    def add_bypass(self, move_id: str, state_id: str, route_id: str = "") -> None:
        event = self._append_event(
            "bypass_added", {"move_id": move_id, "state_id": state_id, "route_id": route_id},
        )
        self.graph.add_relation(self.proof_id, "BYPASSES", move_id, state_id, event["id"], route_id)

    def add_concept(self, concept_id: str, name: str, mechanism_tags: str = "") -> Dict[str, Any]:
        concept = {"id": concept_id, "name": name, "mechanism_tags": mechanism_tags}
        event = self._append_event("concept_added", {"concept": concept})
        self.graph.add_concept(self.proof_id, concept_id, name, mechanism_tags, event["id"])
        return concept

    def add_hypothesis(
        self,
        hypothesis_id: str,
        kind: str,
        target_state_id: str,
        falsification_test: str = "",
        novelty: float = 0.0,
        abductive_strength: float = 0.0,
        cost: float = 0.0,
        risk: float = 0.0,
    ) -> Dict[str, Any]:
        hypothesis = {
            "id": hypothesis_id, "kind": kind, "target_state_id": target_state_id,
            "falsification_test": falsification_test, "novelty": novelty,
            "abductive_strength": abductive_strength, "cost": cost, "risk": risk,
        }
        event = self._append_event("hypothesis_added", {"hypothesis": hypothesis})
        self.graph.add_hypothesis(
            self.proof_id, hypothesis_id, kind, target_state_id, falsification_test,
            novelty, abductive_strength, cost, risk, event_id=event["id"],
        )
        return hypothesis

    # ------------------------------------------------------------------
    # Snapshot / context
    # ------------------------------------------------------------------

    def export_snapshot(self, path: Optional[str | Path] = None) -> Path:
        target = Path(path) if path is not None else self.snapshot_dir / f"snapshot_{len(self.events):03d}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "theorem_kernel": self.theorem_kernel,
            "proof_id": self.proof_id,
            "claims": self.graph.get_all_claims(self.proof_id),
            "events": self.events,
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target

    def context_for(self, state_id: str) -> Dict[str, Any]:
        return self.graph.context_for(self.proof_id, state_id)
