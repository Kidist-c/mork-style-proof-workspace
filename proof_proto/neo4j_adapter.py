from __future__ import annotations

from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase


class Neo4jAdapter:
    """Replaces the Python dict graph in ProofProject with a real Neo4j graph.

    The journal (journal.jsonl) remains the durability authority.
    Neo4j is the query and semantic authority — it can be wiped and rebuilt
    from the journal at any time.

    Cypher primer:
      MERGE  — create node/relationship if it doesn't exist, otherwise match it
      MATCH  — find existing nodes/relationships
      SET    — update properties
      RETURN — what to give back to Python
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "proofagent123",
    ):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._ensure_constraints()

    def close(self) -> None:
        self._driver.close()

    # ------------------------------------------------------------------
    # Schema constraints — run once on startup
    # These tell Neo4j that id must be unique per label,
    # so lookups by id are fast and duplicates are impossible.
    # ------------------------------------------------------------------

    def _ensure_constraints(self) -> None:
        with self._driver.session() as s:
            for label in ("Proof", "State", "Claim", "Attempt"):
                s.run(
                    f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                )

    # ------------------------------------------------------------------
    # Proof node — one per theorem project
    # ------------------------------------------------------------------

    def init_proof(self, proof_id: str, theorem_kernel: str) -> None:
        """Create the root Proof node if it doesn't exist yet."""
        with self._driver.session() as s:
            s.run(
                # MERGE means: create this node only if no Proof with this id exists
                "MERGE (p:Proof {id: $id}) "
                "ON CREATE SET p.theorem_kernel = $theorem_kernel",
                id=proof_id,
                theorem_kernel=theorem_kernel,
            )

    # ------------------------------------------------------------------
    # States
    # ------------------------------------------------------------------

    def add_state(
        self,
        proof_id: str,
        state_id: str,
        description: str,
        parent_id: Optional[str] = None,
    ) -> None:
        with self._driver.session() as s:
            # Create the State node
            s.run(
                "MERGE (st:State {id: $id}) "
                "SET st.description = $description, st.status = 'open', st.proof_id = $proof_id",
                id=state_id,
                description=description,
                proof_id=proof_id,
            )
            # Connect it to the Proof node
            s.run(
                "MATCH (p:Proof {id: $proof_id}), (st:State {id: $state_id}) "
                "MERGE (p)-[:HAS_STATE]->(st)",
                proof_id=proof_id,
                state_id=state_id,
            )
            # If it has a parent state, create the CHILD_OF relationship
            if parent_id:
                s.run(
                    "MATCH (child:State {id: $child_id}), (parent:State {id: $parent_id}) "
                    "MERGE (child)-[:CHILD_OF]->(parent)",
                    child_id=state_id,
                    parent_id=parent_id,
                )

    def get_state(self, state_id: str, proof_id: str = "") -> Optional[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (st:State {id: $id, proof_id: $proof_id}) RETURN st",
                id=state_id,
                proof_id=proof_id,
            )
            record = result.single()
            return dict(record["st"]) if record else None

    # ------------------------------------------------------------------
    # Claims
    # ------------------------------------------------------------------

    def add_claim(
        self,
        proof_id: str,
        claim_id: str,
        statement: str,
        status: str = "conjectural",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (c:Claim {id: $id}) "
                "SET c.statement = $statement, c.status = $status, c.proof_id = $proof_id",
                id=claim_id,
                statement=statement,
                status=status,
                proof_id=proof_id,
            )
            s.run(
                "MATCH (p:Proof {id: $proof_id}), (c:Claim {id: $claim_id}) "
                "MERGE (p)-[:HAS_CLAIM]->(c)",
                proof_id=proof_id,
                claim_id=claim_id,
            )

    def update_claim_status(self, claim_id: str, status: str) -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (c:Claim {id: $id}) SET c.status = $status",
                id=claim_id,
                status=status,
            )

    def get_all_claims(self, proof_id: str) -> List[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (p:Proof {id: $proof_id})-[:HAS_CLAIM]->(c:Claim) RETURN c",
                proof_id=proof_id,
            )
            return [dict(r["c"]) for r in result]

    # ------------------------------------------------------------------
    # Attempts
    # ------------------------------------------------------------------

    def add_attempt(
        self,
        proof_id: str,
        attempt_id: str,
        state_id: str,
        move_summary: str,
        worker: str = "explorer",
        note: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (a:Attempt {id: $id}) "
                "SET a.move_summary = $move_summary, a.worker = $worker, "
                "    a.note = $note, a.status = 'pending', a.proof_id = $proof_id",
                id=attempt_id,
                move_summary=move_summary,
                worker=worker,
                note=note,
                proof_id=proof_id,
            )
            # Attempt -[:ON_STATE]-> State
            # This is the key relationship — it tells us which state this attempt belongs to
            s.run(
                "MATCH (a:Attempt {id: $attempt_id}), (st:State {id: $state_id}) "
                "MERGE (a)-[:ON_STATE]->(st)",
                attempt_id=attempt_id,
                state_id=state_id,
            )

    def update_attempt(self, attempt_id: str, status: str, evidence: str = "") -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Attempt {id: $id}) SET a.status = $status, a.evidence = $evidence",
                id=attempt_id,
                status=status,
                evidence=evidence,
            )

    def close_state(self, state_id: str, proof_id: str, reason: str = "") -> None:
        """Mark a state as closed — proof is complete for this state."""
        with self._driver.session() as s:
            s.run(
                "MATCH (st:State {id: $id, proof_id: $proof_id}) "
                "SET st.status = 'closed', st.closed_reason = $reason",
                id=state_id,
                proof_id=proof_id,
                reason=reason,
            )

    def get_attempts_for_state(self, state_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        """Get all attempts that worked on a given state, scoped to a proof."""
        with self._driver.session() as s:
            result = s.run(
                "MATCH (a:Attempt)-[:ON_STATE]->(st:State {id: $state_id}) "
                "WHERE a.proof_id = $proof_id "
                "RETURN a",
                state_id=state_id,
                proof_id=proof_id,
            )
            return [dict(r["a"]) for r in result]

    # ------------------------------------------------------------------
    # Context query — replaces context_for() in ProofProject
    # ------------------------------------------------------------------

    def context_for(self, proof_id: str, state_id: str) -> Dict[str, Any]:
        """Compile context for a state — state info, its attempts, all claims."""
        return {
            "state": self.get_state(state_id, proof_id),
            "attempts": self.get_attempts_for_state(state_id, proof_id),
            "claims": self.get_all_claims(proof_id),
        }

    # ------------------------------------------------------------------
    # Taint propagation — this is impossible with plain dicts
    # When a claim is refuted, mark everything that depends on it as tainted.
    # ------------------------------------------------------------------

    def propagate_taint(self, claim_id: str) -> List[str]:
        """Mark a claim refuted and taint all claims that depend on it.

        Cypher: follow DEPENDS_ON relationships to any depth using *
        This would require recursive loops in plain Python dicts.
        """
        with self._driver.session() as s:
            result = s.run(
                # Find all claims reachable from this one via DEPENDS_ON, at any depth
                "MATCH (root:Claim {id: $claim_id})<-[:DEPENDS_ON*1..]-(dependent:Claim) "
                "SET dependent.status = 'tainted' "
                "RETURN dependent.id AS id",
                claim_id=claim_id,
            )
            tainted = [r["id"] for r in result]
            # Mark the root claim itself as refuted
            s.run(
                "MATCH (c:Claim {id: $id}) SET c.status = 'refuted'",
                id=claim_id,
            )
            return tainted

    def add_claim_dependency(self, dependent_claim_id: str, depends_on_claim_id: str) -> None:
        """Record that one claim depends on another.

        This builds the justification DAG the paper describes.
        """
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Claim {id: $a_id}), (b:Claim {id: $b_id}) "
                "MERGE (a)-[:DEPENDS_ON]->(b)",
                a_id=dependent_claim_id,
                b_id=depends_on_claim_id,
            )

    # ------------------------------------------------------------------
    # Rebuild from journal — Neo4j is never the source of truth
    # ------------------------------------------------------------------

    def wipe_and_rebuild(self, proof_id: str, events: list) -> None:
        """Wipe all nodes for this proof and replay events to rebuild the graph.

        This proves Neo4j is just a derived view of the journal.
        """
        with self._driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.proof_id = $proof_id DETACH DELETE n",
                proof_id=proof_id,
            )
        for event in events:
            self._replay_event(proof_id, event)

    def _replay_event(self, proof_id: str, event: Dict[str, Any]) -> None:
        t = event.get("type")
        p = event.get("payload", {})
        if t == "project_init":
            self.init_proof(proof_id, p.get("theorem_kernel", ""))
        elif t == "state_added":
            st = p["state"]
            self.add_state(proof_id, st["id"], st["description"], st.get("parent"))
        elif t == "claim_added":
            c = p["claim"]
            self.add_claim(proof_id, c["id"], c["statement"], c.get("status", "conjectural"))
        elif t == "attempt_recorded":
            a = p["attempt"]
            self.add_attempt(proof_id, a["id"], a["state_id"], a["move_summary"], a.get("worker", "explorer"), a.get("note", ""))
        elif t == "attempt_updated":
            a = p["attempt"]
            self.update_attempt(a["id"], a["status"], a.get("evidence", ""))
        elif t == "state_closed":
            self.close_state(p["state_id"], proof_id, p.get("reason", ""))
