from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from neo4j import GraphDatabase

# ----------------------------------------------------------------------------
# Protocol status enums. The commit gate validates against
# ----------------------------------------------------------------------------

STATE_STATUSES = {"open", "closed", "tainted", "reopened"}
MOVE_STATUSES = {"queued", "open", "leased", "refuted", "dominated", "exhausted", "closed"}
CLAIM_STATUSES = {
    "conjectural", "empirical", "provisional", "critic_accepted",
    "lean_verified", "refuted", "retracted", "stale",
}
#  not mentioned in the paper might remove it later. 
ATTEMPT_STATUSES = {"pending", "supported", "critic_accepted", "refuted", "retracted"}

# Relationship types accepted by the generic add_relation() linker.
# Keeping an explicit allowlist means relationship type is never interpolated
# from untrusted input.
_REL_WHITELIST = {
    # search DAG 
    "SUPERSEDES", "ALTERNATIVE_TO", "GENERALIZES", "REFORMULATES", "FORMALIZES",
    "CONTRADICTS", "STRENGTHENS_ROUTE", "LEAVES_OPEN", "REDUCES_TARGET",
    "EXPOSES_BARRIER", "BYPASSES",
    # justification DAG 
    "SUPPORTED_BY", "PROVED_BY", "CONTRADICTED_BY", "VERIFIED_BY",
    "VERIFIED_BY_EXPERIMENT", "INVALIDATES",
    # state -> claim reference (used for taint reopening)
    "USES_CLAIM",
    # speculative layer 
    "SUGGESTS", "EXPECTS", "SOURCE_CONCEPT", "RELATED_TO", "FALSIFIED_BY",
    "ELABORATED_INTO",
}

# All node labels in the metagraph.
_LABELS = (
    "Proof", "State", "Claim", "Move", "Attempt", "Route", "Artifact",
    "Context", "Hypothesis", "Concept", "Critique", "Experiment", "Verification",
)

# prevents bad data from slipping in
def _check(value: str, allowed: Set[str], label: str) -> None:
    if value not in allowed:
        raise ValueError(
            f"invalid {label} {value!r}; expected one of {sorted(allowed)}"
        )


class Neo4jAdapter:
    """A Neo4j projection of the paper's MORK-backed PeTTa metagraph.

    Three linked DAGs (search / justification / provenance) plus a speculative
    hypothesis layer, all scoped by proof_id (one proof = one namespace).

    Neo4j is the semantic and query authority only. The append-only journal
    (SQL / JSONL) is the durability authority — this graph can always be wiped
    and rebuilt by replay (see wipe_and_rebuild).
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "proofagent123",
    ):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._ensure_constraints()  #this create a unique constraints for each node type usiing compostie key (proof_id,id)

    def close(self) -> None:
        self._driver.close()

    
    def _ensure_constraints(self) -> None:
        with self._driver.session() as s:
            for label in _LABELS:
                s.run(
                    f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE (n.proof_id, n.id) IS UNIQUE"
                )
            for label, prop in (("State", "status"), ("Move", "status"), ("Claim", "status")):
                s.run(
                    f"CREATE INDEX {label.lower()}_{prop} IF NOT EXISTS "
                    f"FOR (n:{label}) ON (n.proof_id, n.{prop})"
                )

    def _edge_id(self, event_id: str, kind: str) -> str:
        return f"{event_id}-{kind}" if kind else event_id

    def _would_create_cycle(
        self,
        dependent_claim_id: str,
        depends_on_claim_id: str,
        proof_id: str = "",
    ) -> bool:
        """Return True if adding DEPENDS_ON from dependent→depends_on would close a cycle.

        Checks whether *depends_on_claim_id* can already reach
        *dependent_claim_id* through existing DEPENDS_ON edges.
        """
        with self._driver.session() as s:
            result = s.run(
                "MATCH path = (b:Claim {id: $b_id, proof_id: $pid})"
                "-[:DEPENDS_ON*1..]->(a:Claim {id: $a_id, proof_id: $pid}) "
                "RETURN path LIMIT 1",
                b_id=depends_on_claim_id, a_id=dependent_claim_id, pid=proof_id,
            )
            return result.single() is not None

    # ------------------------------------------------------------------
    # Proof node — one per theorem project (namespace anchor)
    # ------------------------------------------------------------------

    def init_proof(
        self,
        proof_id: str,
        theorem_kernel: str,
        theorem_hash: str = "",
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (p:Proof {proof_id: $pid, id: $pid}) "
                "ON CREATE SET p.theorem_kernel = $k, p.theorem_hash = $h, "
                "              p.active_revision = 0, p.created_in_event = $evt "
                "ON MATCH SET p.theorem_kernel = $k, p.theorem_hash = $h",
                pid=proof_id,
                k=theorem_kernel,
                h=theorem_hash,
                evt=event_id,
            )

    # ------------------------------------------------------------------
    # States (search DAG — OR point) - a problem state in the search process
    # ------------------------------------------------------------------

    def add_state(
        self,
        proof_id: str,
        state_id: str,
        description: str,
        parent_id: Optional[str] = None,
        kind: str = "or",
        assumptions: str = "",
        event_id: str = "",
    ) -> None:
        _check(kind, {"or", "goal", "and"}, "state kind")
        with self._driver.session() as s:
            s.run(
                "MERGE (st:State {proof_id: $pid, id: $id}) "
                "ON CREATE SET st.description = $desc, st.status = 'open', "
                "              st.kind = $kind, st.assumptions = $ass, "
                "              st.created_in_event = $evt "
                "ON MATCH SET st.description = $desc, st.kind = $kind",
                pid=proof_id, id=state_id, desc=description,
                kind=kind, ass=assumptions, evt=event_id,
            )
            s.run(
                "MATCH (p:Proof {proof_id: $pid, id: $pid}), (st:State {proof_id: $pid, id: $id}) "
                "MERGE (p)-[r:HAS_STATE {edge_id: $eid}]->(st) "
                "ON CREATE SET st.description = $desc, st.status = 'open', st.created_in_event = $evt "
                "SET r.event_id = $evt",
                pid=proof_id, id=state_id, desc=description,
                eid=self._edge_id(event_id, "HAS_STATE"), evt=event_id,
            )
            if parent_id:
                s.run(
                "MATCH (child:State {proof_id: $pid, id: $cid}), "
                "(parent:State {proof_id: $pid, id: $pid2}) "
                "MERGE (child)-[r:CHILD_OF {edge_id: $eid}]->(parent) "
                "ON CREATE SET child.status = 'open', child.created_in_event = $evt "
                "SET r.event_id = $evt",
                    pid=proof_id, cid=state_id, pid2=parent_id,
                    eid=self._edge_id(event_id, "CHILD_OF"), evt=event_id,
                )

    def get_state(self, state_id: str, proof_id: str = "") -> Optional[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (st:State {id: $id}) "
                "WHERE $pid = '' OR st.proof_id = $pid "
                "RETURN st",
                id=state_id, pid=proof_id,
            )
            record = result.single()
            return dict(record["st"]) if record else None

    def update_state_status(
        self,
        proof_id: str,
        state_id: str,
        status: str,
        reason: str = "",
        event_id: str = "",
    ) -> None:
        _check(status, STATE_STATUSES, "state status")
        with self._driver.session() as s:
            s.run(
                "MATCH (st:State {proof_id: $pid, id: $id}) "
                "SET st.status = $status, st.status_updated_in_event = $evt, "
                "    st.closed_reason = $reason",
                pid=proof_id, id=state_id, status=status, evt=event_id, reason=reason,
            )

    # ------------------------------------------------------------------
    # Claims (justification DAG)
    # ------------------------------------------------------------------

    def add_claim(
        self,
        proof_id: str,
        claim_id: str,
        statement: str,
        status: str = "conjectural",
        statement_blob: str = "",
        event_id: str = "",
    ) -> None:
        _check(status, CLAIM_STATUSES, "claim status")
        with self._driver.session() as s:
            s.run(
                "MERGE (c:Claim {proof_id: $pid, id: $id}) "
                "ON CREATE SET c.statement = $stmt, c.status = $status, "
                "              c.statement_blob = $blob, c.created_in_event = $evt "
                "ON MATCH SET c.statement = $stmt, c.status = $status",
                pid=proof_id, id=claim_id, stmt=statement,
                status=status, blob=statement_blob, evt=event_id,
            )
            s.run(
                "MATCH (p:Proof {proof_id: $pid, id: $pid}), (c:Claim {proof_id: $pid, id: $id}) "
                "MERGE (p)-[r:HAS_CLAIM {edge_id: $eid}]->(c) "
                "ON CREATE SET c.statement = $stmt, c.status = $status, c.created_in_event = $evt "
                "SET r.event_id = $evt",
                pid=proof_id, id=claim_id, stmt=statement, status=status,
                eid=self._edge_id(event_id, "HAS_CLAIM"), evt=event_id,
            )

    def update_claim_status(
        self,
        claim_id: str,
        status: str,
        proof_id: str = "",
        event_id: str = "",
        reason: str = "",
    ) -> None:
        _check(status, CLAIM_STATUSES, "claim status")
        with self._driver.session() as s:
            s.run(
                "MATCH (c:Claim {id: $id}) "
                "WHERE $pid = '' OR c.proof_id = $pid "
                "SET c.status = $status, c.status_updated_in_event = $evt, "
                "    c.status_reason = CASE WHEN $reason <> '' "
                "                            THEN $reason ELSE c.status_reason END",
                id=claim_id, status=status, pid=proof_id, evt=event_id, reason=reason,
            )

    def get_all_claims(self, proof_id: str) -> List[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (p:Proof {proof_id: $pid, id: $pid})-[:HAS_CLAIM]->(c:Claim) "
                "RETURN c",
                pid=proof_id,
            )
            return [dict(r["c"]) for r in result]

    # this creates a DEPENDS_ON relationship between two claims
    def add_claim_dependency(
        self,
        dependent_claim_id: str,
        depends_on_claim_id: str,
        proof_id: str = "",
        event_id: str = "",
    ) -> None:
        """DEPENDS_ON edge — raises ValueError if it would create a cycle."""
        if proof_id and self._would_create_cycle(dependent_claim_id, depends_on_claim_id, proof_id):
            raise ValueError(
                f"Adding DEPENDS_ON {dependent_claim_id} -> {depends_on_claim_id} "
                f"would create a cycle in the claim dependency graph"
            )
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Claim {id: $a_id}), (b:Claim {id: $b_id}) "
                "WHERE ($pid = '' OR a.proof_id = $pid) "
                "  AND ($pid = '' OR b.proof_id = $pid) "
                "MERGE (a)-[r:DEPENDS_ON {edge_id: $eid}]->(b) "
                "ON CREATE SET a.proof_id = CASE WHEN $pid = '' THEN a.proof_id ELSE $pid END "
                "SET r.event_id = $evt",
                a_id=dependent_claim_id, b_id=depends_on_claim_id,
                pid=proof_id, eid=self._edge_id(event_id, "DEPENDS_ON"), evt=event_id,
            )
    #this creates USES_CLAIM  realationship between state and claim 
    def link_state_claim(
        self,
        proof_id: str,
        state_id: str,
        claim_id: str,
        event_id: str = "",
    ) -> None:
        """(:State)-[:USES_CLAIM]->(:Claim) — a state's established/provisional refs."""
        with self._driver.session() as s:
            s.run(
                "MATCH (st:State {proof_id: $pid, id: $sid}), (c:Claim {proof_id: $pid, id: $cid}) "
                "MERGE (st)-[r:USES_CLAIM {edge_id: $eid}]->(c) "
                "ON CREATE SET st.created_in_event = $evt "
                "SET r.event_id = $evt",
                pid=proof_id, sid=state_id, cid=claim_id,
                eid=self._edge_id(event_id, "USES_CLAIM"), evt=event_id,
            )

    # ------------------------------------------------------------------
    # Moves (search DAG — AND point). State proposes (OR), move requires (AND).
    # ------------------------------------------------------------------

    def add_move(
        self,
        proof_id: str,
        move_id: str,
        state_id: str,
        move_summary: str,
        kind: str = "reduction",
        note: str = "",
        event_id: str = "",
        score: Optional[Dict[str, Any]] = None,
        cost_estimate: Optional[str] = None,
        status: str = "queued",
    ) -> None:
        _check(status, MOVE_STATUSES, "move status")
        with self._driver.session() as s:
            s.run(
                "MERGE (m:Move {proof_id: $pid, id: $id}) "
                "ON CREATE SET m.move_summary = $sum, m.kind = $kind, "
                "              m.note = $note, m.status = $status, "
                "              m.score = $score, m.cost_estimate = $cost, "
                "              m.repeated_failure_count = 0, m.created_in_event = $evt "
                "ON MATCH SET m.move_summary = $sum, m.kind = $kind, m.note = $note",
                pid=proof_id, id=move_id, sum=move_summary, kind=kind,
                note=note, status=status, score=score, cost=cost_estimate, evt=event_id,
            )
            s.run(
                "MATCH (st:State {proof_id: $pid, id: $sid}), (m:Move {proof_id: $pid, id: $mid}) "
                "MERGE (st)-[r:PROPOSES {edge_id: $eid}]->(m) "
                "ON CREATE SET m.status = $status, m.created_in_event = $evt "
                "SET r.event_id = $evt",
                pid=proof_id, sid=state_id, mid=move_id,
                eid=self._edge_id(event_id, "PROPOSES"), evt=event_id, status=status,
            )

    def add_required_subgoal(
        self,
        proof_id: str,
        move_id: str,
        subgoal_id: str,
        description: str,
        parent_state_id: Optional[str] = None,
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (st:State {proof_id: $pid, id: $id}) "
                "ON CREATE SET st.description = $desc, st.status = 'open', "
                "              st.kind = 'and', st.created_in_event = $evt",
                pid=proof_id, id=subgoal_id, desc=description, evt=event_id,
            )
            s.run(
                "MATCH (p:Proof {proof_id: $pid, id: $pid}), (st:State {proof_id: $pid, id: $id}) "
                "MERGE (p)-[r:HAS_STATE {edge_id: $eid}]->(st) "
                "ON CREATE SET st.description = $desc, st.status = 'open', st.created_in_event = $evt "
                "SET r.event_id = $evt",
                pid=proof_id, id=subgoal_id, desc=description,
                eid=self._edge_id(event_id, "HAS_STATE"), evt=event_id,
            )
            s.run(
                "MATCH (m:Move {proof_id: $pid, id: $mid}), (st:State {proof_id: $pid, id: $sid}) "
                "MERGE (m)-[r:REQUIRES {edge_id: $eid}]->(st) "
                "ON CREATE SET st.status = 'open', st.created_in_event = $evt "
                "SET r.event_id = $evt",
                pid=proof_id, mid=move_id, sid=subgoal_id,
                eid=self._edge_id(event_id, "REQUIRES"), evt=event_id,
            )
            if parent_state_id:
                s.run(
                    "MATCH (child:State {proof_id: $pid, id: $cid}), "
                    "(parent:State {proof_id: $pid, id: $pid2}) "
                    "MERGE (child)-[r:CHILD_OF {edge_id: $eid}]->(parent) "
                    "SET r.event_id = $evt",
                    pid=proof_id, cid=subgoal_id, pid2=parent_state_id,
                    eid=self._edge_id(event_id, "CHILD_OF"), evt=event_id,
                )

    def update_move_status(
        self,
        move_id: str,
        status: str,
        proof_id: str = "",
        event_id: str = "",
    ) -> None:
        _check(status, MOVE_STATUSES, "move status")
        with self._driver.session() as s:
            s.run(
                "MATCH (m:Move {id: $id}) "
                "WHERE $pid = '' OR m.proof_id = $pid "
                "SET m.status = $status, m.status_updated_in_event = $evt",
                id=move_id, status=status, pid=proof_id, evt=event_id,
            )

    def get_moves_for_state(self, state_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (st:State {id: $sid})-[:PROPOSES]->(m:Move) "
                "WHERE $pid = '' OR m.proof_id = $pid "
                "RETURN m ORDER BY m.id",
                sid=state_id, pid=proof_id,
            )
            return [dict(r["m"]) for r in result]

    def get_subgoals_for_move(self, move_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (m:Move {id: $mid})-[:REQUIRES]->(st:State) "
                "WHERE $pid = '' OR st.proof_id = $pid "
                "RETURN st ORDER BY st.id",
                mid=move_id, pid=proof_id,
            )
            return [dict(r["st"]) for r in result]

    def eligible_frontier(self, proof_id: str) -> List[Dict[str, Any]]:
        """Eligible moves for leasing (paper section 4.7).

        (Open ∪ Reopened) − (Leased ∪ Refuted ∪ Dominated ∪ Exhausted),
        restricted to moves whose state is not tainted/refuted.
        """
        with self._driver.session() as s:
            result = s.run(
                "MATCH (st:State {proof_id: $pid})-[:PROPOSES]->(m:Move {proof_id: $pid}) "
                "WHERE m.status IN ['open', 'reopened'] "
                "  AND m.status <> 'leased' AND m.status <> 'refuted' "
                "  AND m.status <> 'dominated' AND m.status <> 'exhausted' "
                "  AND st.status <> 'tainted' AND st.status <> 'refuted' AND st.status <> 'closed' "
                "RETURN m ORDER BY m.status, m.id",
                pid=proof_id,
            )
            return [dict(r["m"]) for r in result]

    # ------------------------------------------------------------------
    # Attempts (provenance DAG) 
    # attempt - is an execution record
    # ------------------------------------------------------------------

    def add_attempt(
        self,
        proof_id: str,
        attempt_id: str,
        state_id: str,
        move_summary: str,
        worker: str = "explorer",
        note: str = "",
        move_id: Optional[str] = None,
        event_id: str = "",
        route_id: Optional[str] = None,
        model_persona: str = "",
        disposition: str = "",
        result_relation: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (a:Attempt {proof_id: $pid, id: $id}) "
                "ON CREATE SET a.move_summary = $sum, a.worker = $worker, "
                "              a.note = $note, a.status = 'pending', "
                "              a.model_persona = $persona, a.disposition = $disp, "
                "              a.result_relation = $rel, a.created_in_event = $evt "
                "ON MATCH SET a.move_summary = $sum, a.worker = $worker, a.note = $note",
                pid=proof_id, id=attempt_id, sum=move_summary, worker=worker,
                note=note, persona=model_persona, disp=disposition,
                rel=result_relation, evt=event_id,
            )
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (st:State {proof_id: $pid, id: $sid}) "
                "MERGE (a)-[r:ON_STATE {edge_id: $eid}]->(st) "
                "SET r.event_id = $evt",
                pid=proof_id, aid=attempt_id, sid=state_id,
                eid=self._edge_id(event_id, "ON_STATE"), evt=event_id,
            )
            if move_id:
                s.run(
                    "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (m:Move {proof_id: $pid, id: $mid}) "
                    "MERGE (a)-[r:ON_MOVE {edge_id: $eid}]->(m) "
                    "SET r.event_id = $evt",
                    pid=proof_id, aid=attempt_id, mid=move_id,
                    eid=self._edge_id(event_id, "ON_MOVE"), evt=event_id,
                )
            if route_id:
                s.run(
                    "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (r:Route {proof_id: $pid, id: $rid}) "
                    "MERGE (a)-[r2:VIA_ROUTE {edge_id: $eid}]->(r) "
                    "SET r2.event_id = $evt",
                    pid=proof_id, aid=attempt_id, rid=route_id,
                    eid=self._edge_id(event_id, "VIA_ROUTE"), evt=event_id,
                )

    def update_attempt(
        self,
        attempt_id: str,
        status: str,
        evidence: str = "",
        proof_id: str = "",
        event_id: str = "",
    ) -> None:
        _check(status, ATTEMPT_STATUSES, "attempt status")
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Attempt {id: $id}) "
                "WHERE $pid = '' OR a.proof_id = $pid "
                "SET a.status = $status, a.evidence = $evidence, "
                "    a.status_updated_in_event = $evt",
                id=attempt_id, status=status, evidence=evidence, pid=proof_id, evt=event_id,
            )

    def get_attempts_for_state(self, state_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (a:Attempt)-[:ON_STATE]->(st:State {id: $sid}) "
                "WHERE $pid = '' OR a.proof_id = $pid "
                "RETURN a",
                sid=state_id, pid=proof_id,
            )
            return [dict(r["a"]) for r in result]

    def get_attempts_for_move(self, move_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (a:Attempt)-[:ON_MOVE]->(m:Move {id: $mid}) "
                "WHERE $pid = '' OR a.proof_id = $pid "
                "RETURN a",
                mid=move_id, pid=proof_id,
            )
            return [dict(r["a"]) for r in result]

    def link_attempt_route(self, proof_id: str, attempt_id: str, route_id: str, event_id: str = "") -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (r:Route {proof_id: $pid, id: $rid}) "
                "MERGE (a)-[r2:VIA_ROUTE {edge_id: $eid}]->(r) "
                "SET r2.event_id = $evt",
                pid=proof_id, aid=attempt_id, rid=route_id,
                eid=self._edge_id(event_id, "VIA_ROUTE"), evt=event_id,
            )

    def link_attempt_context(self, proof_id: str, attempt_id: str, context_id: str, event_id: str = "") -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (c:Context {proof_id: $pid, id: $cid}) "
                "MERGE (a)-[r:USED_CONTEXT {edge_id: $eid}]->(c) "
                "SET r.event_id = $evt",
                pid=proof_id, aid=attempt_id, cid=context_id,
                eid=self._edge_id(event_id, "USED_CONTEXT"), evt=event_id,
            )

    def link_produced_claim(self, proof_id: str, attempt_id: str, claim_id: str, event_id: str = "") -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (c:Claim {proof_id: $pid, id: $cid}) "
                "MERGE (a)-[r:PRODUCED_CLAIM {edge_id: $eid}]->(c) "
                "SET r.event_id = $evt",
                pid=proof_id, aid=attempt_id, cid=claim_id,
                eid=self._edge_id(event_id, "PRODUCED_CLAIM"), evt=event_id,
            )

    # ------------------------------------------------------------------
    # Routes, artifacts, contexts (provenance DAG)
    # routes - is a path used during the proof search ( a file system , a code path , an execution route, a tool route)
    # ------------------------------------------------------------------

    def add_route(self, proof_id: str, route_id: str, display_path: str, event_id: str = "") -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (r:Route {proof_id: $pid, id: $id}) "
                "ON CREATE SET r.display_path = $path, r.created_in_event = $evt",
                pid=proof_id, id=route_id, path=display_path, evt=event_id,
            )
    #  artifact is something produced during the proof process like file , validation  output

    def add_artifact(
        self,
        proof_id: str,
        artifact_id: str,
        kind: str,
        media_type: str = "",
        sha256: str = "",
        filename: str = "",
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (art:Artifact {proof_id: $pid, id: $id}) "
                "ON CREATE SET art.kind = $kind, art.media_type = $media, "
                "              art.sha256 = $sha, art.filename = $fname, "
                "              art.created_in_event = $evt",
                pid=proof_id, id=artifact_id, kind=kind, media=media_type,
                sha=sha256, fname=filename, evt=event_id,
            )

    def link_artifact(self, proof_id: str, attempt_id: str, artifact_id: str, event_id: str = "") -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (art:Artifact {proof_id: $pid, id: $aid2}) "
                "MERGE (a)-[r:PRODUCED_ARTIFACT {edge_id: $eid}]->(art) "
                "SET r.event_id = $evt",
                pid=proof_id, aid=attempt_id, aid2=artifact_id,
                eid=self._edge_id(event_id, "PRODUCED_ARTIFACT"), evt=event_id,
            )

    # context node captures packet_hash , complier_version, token_budget, token_count. and used for reproduciblity 

    def add_context(
        self,
        proof_id: str,
        context_id: str,
        packet_hash: str = "",
        compiler_version: str = "",
        token_budget: int = 0,
        token_count: int = 0,
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (c:Context {proof_id: $pid, id: $id}) "
                "ON CREATE SET c.packet_hash = $hash, c.compiler_version = $ver, "
                "              c.token_budget = $budget, c.token_count = $count, "
                "              c.created_in_event = $evt",
                pid=proof_id, id=context_id, hash=packet_hash, ver=compiler_version,
                budget=token_budget, count=token_count, evt=event_id,
            )

    # ------------------------------------------------------------------
    # Critics, experiments, verification (independent checks)
    # ------------------------------------------------------------------
    # creates a critque node which is basically a negative review of an attempt
    def add_critique(
        self,
        proof_id: str,
        critique_id: str,
        attempt_id: str,
        verdict: str,
        reason: str = "",
        critic_worker: str = "critic",
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (cr:Critique {proof_id: $pid, id: $id}) "
                "ON CREATE SET cr.verdict = $verdict, cr.reason = $reason, "
                "              cr.critic_worker = $worker, cr.created_in_event = $evt",
                pid=proof_id, id=critique_id, verdict=verdict, reason=reason,
                worker=critic_worker, evt=event_id,
            )
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (cr:Critique {proof_id: $pid, id: $cid}) "
                "MERGE (a)-[r:HAD_CRITIQUE {edge_id: $eid}]->(cr) "
                "SET r.event_id = $evt",
                pid=proof_id, aid=attempt_id, cid=critique_id,
                eid=self._edge_id(event_id, "HAD_CRITIQUE"), evt=event_id,
            )
    # 
    def add_experiment(
        self,
        proof_id: str,
        experiment_id: str,
        attempt_id: str,
        question: str,
        status: str = "ran",
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (e:Experiment {proof_id: $pid, id: $id}) "
                "ON CREATE SET e.question = $q, e.status = $status, "
                "              e.created_in_event = $evt",
                pid=proof_id, id=experiment_id, q=question, status=status, evt=event_id,
            )
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (e:Experiment {proof_id: $pid, id: $eid}) "
                "MERGE (a)-[r:RAN {edge_id: $edge}]->(e) "
                "SET r.event_id = $evt",
                pid=proof_id, aid=attempt_id, eid=experiment_id,
                edge=self._edge_id(event_id, "RAN"), evt=event_id,
            )
    #  this is a formal or lean verification result attached to a clain
    def add_verification(
        self,
        proof_id: str,
        verification_id: str,
        attempt_id: str,
        claim_id: str,
        kind: str = "lean",
        status: str = "pending",
        lean_name: str = "",
        toolchain_hash: str = "",
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (v:Verification {proof_id: $pid, id: $id}) "
                "ON CREATE SET v.kind = $kind, v.status = $status, "
                "              v.lean_name = $lname, v.toolchain_hash = $tool, "
                "              v.created_in_event = $evt",
                pid=proof_id, id=verification_id, kind=kind, status=status,
                lname=lean_name, tool=toolchain_hash, evt=event_id,
            )
            s.run(
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (v:Verification {proof_id: $pid, id: $vid}) "
                "MERGE (a)-[r:HAD_VERIFICATION {edge_id: $eid}]->(v) "
                "SET r.event_id = $evt",
                pid=proof_id, aid=attempt_id, vid=verification_id,
                eid=self._edge_id(event_id, "HAD_VERIFICATION"), evt=event_id,
            )
            s.run(
                "MATCH (v:Verification {proof_id: $pid, id: $vid}), (c:Claim {proof_id: $pid, id: $cid}) "
                "MERGE (v)-[r:OF {edge_id: $eid}]->(c) "
                "SET r.event_id = $evt",
                pid=proof_id, vid=verification_id, cid=claim_id,
                eid=self._edge_id(event_id, "OF"), evt=event_id,
            )

    # ------------------------------------------------------------------
    # Concepts + speculative hypotheses (Hyperon layer)
    # ------------------------------------------------------------------

    def add_concept(
        self,
        proof_id: str,
        concept_id: str,
        name: str,
        mechanism_tags: str = "",
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (c:Concept {proof_id: $pid, id: $id}) "
                "ON CREATE SET c.name = $name, c.mechanism_tags = $tags, "
                "              c.created_in_event = $evt",
                pid=proof_id, id=concept_id, name=name, tags=mechanism_tags, evt=event_id,
            )

    def add_hypothesis(
        self,
        proof_id: str,
        hypothesis_id: str,
        kind: str,
        target_state_id: str,
        falsification_test: str = "",
        novelty: float = 0.0,
        abductive_strength: float = 0.0,
        cost: float = 0.0,
        risk: float = 0.0,
        lifecycle_status: str = "queued",
        event_id: str = "",
    ) -> None:
        with self._driver.session() as s:
            s.run(
                "MERGE (h:Hypothesis {proof_id: $pid, id: $id}) "
                "ON CREATE SET h.kind = $kind, h.layer = 'speculative', "
                "              h.falsification_test = $test, h.novelty = $nov, "
                "              h.abductive_strength = $ab, h.cost = $cost, "
                "              h.risk = $risk, h.lifecycle_status = $lc, "
                "              h.created_in_event = $evt",
                pid=proof_id, id=hypothesis_id, kind=kind, test=falsification_test,
                nov=novelty, ab=abductive_strength, cost=cost, risk=risk,
                lc=lifecycle_status, evt=event_id,
            )
            s.run(
                "MATCH (h:Hypothesis {proof_id: $pid, id: $hid}), (st:State {proof_id: $pid, id: $sid}) "
                "MERGE (h)-[r:TARGETS {edge_id: $eid}]->(st) "
                "SET r.event_id = $evt",
                pid=proof_id, hid=hypothesis_id, sid=target_state_id,
                eid=self._edge_id(event_id, "TARGETS"), evt=event_id,
            )

    def add_relation(
        self,
        proof_id: str,
        rel: str,
        source_id: str,
        target_id: str,
        event_id: str = "",
        route_id: str = "",
    ) -> None:
        """Generic typed relationship linker (whitelisted rel types only).

        Covers the long tail: SUPERSEDES, BYPASSES, STRENGTHENS_ROUTE,
        SUPPORTED_BY, PROVED_BY, CONTRADICTED_BY, SUGGESTS, EXPECTS, ... —
        see _REL_WHITELIST.
        """
        if rel not in _REL_WHITELIST:
            raise ValueError(f"relationship type {rel!r} not in whitelist")
        with self._driver.session() as s:
            s.run(
                "MATCH (a {proof_id: $pid, id: $sid}), (b {proof_id: $pid, id: $tid}) "
                f"MERGE (a)-[r:{rel} {{edge_id: $eid}}]->(b) "
                "SET r.event_id = $evt, r.route_id = $rid",
                pid=proof_id, sid=source_id, tid=target_id,
                eid=self._edge_id(event_id, rel), evt=event_id, rid=route_id,
            )

    # ------------------------------------------------------------------
    # AND/OR closure — the graph semantics (paper §9.6)
    # ------------------------------------------------------------------

    def state_is_solved(self, proof_id: str, state_id: str) -> bool:
        """OR rule: a state is solved when any proposed move is closed."""
        with self._driver.session() as s:
            rec = s.run(
                "MATCH (st:State {proof_id: $pid, id: $sid})-[:PROPOSES]->(m:Move {proof_id: $pid}) "
                "WHERE m.status = 'closed' RETURN count(m) AS c",
                pid=proof_id, sid=state_id,
            ).single()
            return rec["c"] > 0

    def move_is_complete(self, proof_id: str, move_id: str) -> bool:
        """AND rule: a move is complete when every REQUIRES subgoal is closed."""
        with self._driver.session() as s:
            rec = s.run(
                "MATCH (m:Move {proof_id: $pid, id: $mid})-[:REQUIRES]->(sg:State {proof_id: $pid}) "
                "WHERE sg.status <> 'closed' AND sg.status <> 'reopened' "
                "RETURN count(sg) AS open_subgoals",
                pid=proof_id, mid=move_id,
            ).single()
            return rec["open_subgoals"] == 0

    def close_state(
        self,
        state_id: str,
        proof_id: str,
        reason: str = "",
        event_id: str = "",
    ) -> None:
        """Mark a state closed, close its proposed moves, then propagate
        closures upward (AND then OR) to a fixpoint.

        NOTE: BYPASSES is deliberately NOT a PROPOSES edge, so a bypass never
        closes the literal target (N107 pattern) — that is enforced structurally.
        """
        self.update_state_status(proof_id, state_id, "closed", reason, event_id)
        with self._driver.session() as s:
            s.run(
                "MATCH (st:State {proof_id: $pid, id: $sid})-[:PROPOSES]->(m:Move {proof_id: $pid}) "
                "SET m.status = 'closed', m.status_updated_in_event = $evt",
                pid=proof_id, sid=state_id, evt=event_id,
            )
        self._propagate_closures(proof_id, event_id)

    def _propagate_closures(self, proof_id: str, event_id: str = "", max_iter: int = 64) -> None:
        with self._driver.session() as s:
            for _ in range(max_iter):
                # AND: a move closes once every REQUIRES subgoal is closed.
                r1 = s.run(
                    "MATCH (m:Move {proof_id: $pid}) "
                    "WHERE m.status <> 'closed' "
                    "AND NOT exists { (m)-[:REQUIRES]->(sg:State {proof_id: $pid}) "
                    "                  WHERE sg.status <> 'closed' AND sg.status <> 'reopened' } "
                    "SET m.status = 'closed', m.status_updated_in_event = $evt "
                    "RETURN count(m) AS n",
                    pid=proof_id, evt=event_id,
                ).single()["n"]
                # OR: a state closes once any proposed move is closed.
                r2 = s.run(
                    "MATCH (st:State {proof_id: $pid}) "
                    "WHERE st.status <> 'closed' AND st.status <> 'reopened' "
                    "AND exists { (st)-[:PROPOSES]->(m:Move {proof_id: $pid}) "
                    "             WHERE m.status = 'closed' } "
                    "SET st.status = 'closed', st.status_updated_in_event = $evt "
                    "RETURN count(st) AS n",
                    pid=proof_id, evt=event_id,
                ).single()["n"]
                if r1 == 0 and r2 == 0:
                    break

    def reopen_state(self, proof_id: str, state_id: str, reason: str = "", event_id: str = "") -> None:
        self.update_state_status(proof_id, state_id, "reopened", reason, event_id)

    # ------------------------------------------------------------------
    # Taint propagation (paper §4.10) — a claim refutation cascades through
    # the justification DAG and reopens states that depended on the claim.
    # ------------------------------------------------------------------

    def propagate_taint(self, proof_id: str, claim_id: str, event_id: str = "", reason: str = "") -> Dict[str, Any]:
        """Refute a claim and cascade:
          1. mark the root claim refuted;
          2. taint every transitive DEPENDS_ON dependent (taint cone);
          3. reopen closed states that used a tainted claim.
        Returns a summary for audit/milestones.
        """
        with self._driver.session() as s:
            s.run(
                "MATCH (c:Claim {proof_id: $pid, id: $cid}) "
                "SET c.status = 'refuted', c.status_updated_in_event = $evt, "
                "    c.status_reason = CASE WHEN $reason <> '' "
                "                            THEN $reason ELSE c.status_reason END",
                pid=proof_id, cid=claim_id, evt=event_id, reason=reason,
            )
            result = s.run(
                "MATCH (root:Claim {proof_id: $pid, id: $cid})"
                "<-[:DEPENDS_ON*1..]-(d:Claim {proof_id: $pid}) "
                "SET d.status = 'tainted', d.taint_source = $src, "
                "    d.status_updated_in_event = $evt "
                "RETURN collect(DISTINCT d.id) AS tainted",
                pid=proof_id, cid=claim_id, src=claim_id, evt=event_id,
            ).single()
            tainted = result["tainted"] if result else []

            reopened = []
            if tainted:
                reopened = s.run(
                    "MATCH (st:State {proof_id: $pid})"
                    "-[:USES_CLAIM]->(c:Claim {proof_id: $pid}) "
                    "WHERE c.id IN $tainted AND st.status = 'closed' "
                    "SET st.status = 'reopened', "
                    "    st.closed_reason = 'taint: ' + $src, "
                    "    st.status_updated_in_event = $evt "
                    "RETURN collect(DISTINCT st.id) AS reopened",
                    pid=proof_id, tainted=tainted, src=claim_id, evt=event_id,
                ).single()["reopened"]
        return {"refuted": claim_id, "tainted": tainted, "reopened_states": reopened}

    def taint_cone(self, proof_id: str, claim_id: str) -> List[str]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (root:Claim {proof_id: $pid, id: $cid})"
                "<-[:DEPENDS_ON*1..]-(d:Claim {proof_id: $pid}) "
                "RETURN collect(DISTINCT d.id) AS ids",
                pid=proof_id, cid=claim_id,
            ).single()
            return result["ids"] if result else []

    # ------------------------------------------------------------------
    # Context query — replaces context_for() in ProofProject
    # ------------------------------------------------------------------

    def context_for(self, proof_id: str, state_id: str) -> Dict[str, Any]:
        moves = self.get_moves_for_state(state_id, proof_id)
        return {
            "state": self.get_state(state_id, proof_id),
            "moves": moves,
            "attempts": self.get_attempts_for_state(state_id, proof_id),
            "claims": self.get_all_claims(proof_id),
            "subgoals": [
                sg
                for move in moves
                for sg in self.get_subgoals_for_move(move["id"], proof_id)
            ],
            "frontier": self.eligible_frontier(proof_id),
        }

    # ------------------------------------------------------------------
    # Rebuild from journal
    # ------------------------------------------------------------------

    def wipe_and_rebuild(self, proof_id: str, events: List[Dict[str, Any]]) -> None:
        with self._driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.proof_id = $pid DETACH DELETE n",
                pid=proof_id,
            )
        for event in events:
            self._replay_event(proof_id, event)

    def _replay_event(self, proof_id: str, event: Dict[str, Any]) -> None:
        t = event.get("type")
        p = event.get("payload", {})
        evt = event.get("id", "")
        if t == "project_init":
            self.init_proof(proof_id, p.get("theorem_kernel", ""), event_id=evt)
        elif t == "state_added":
            st = p["state"]
            self.add_state(proof_id, st["id"], st["description"], st.get("parent"),
                           kind=st.get("kind", "or"), assumptions=st.get("assumptions", ""),
                           event_id=evt)
        elif t == "claim_added":
            c = p["claim"]
            self.add_claim(proof_id, c["id"], c["statement"], c.get("status", "conjectural"), event_id=evt)
        elif t == "claim_dependency_added":
            self.add_claim_dependency(p["dependent_claim_id"], p["depends_on_claim_id"], proof_id, evt)
        elif t == "move_added":
            mv = p["move"]
            self.add_move(proof_id, mv["id"], mv["state_id"], mv["move_summary"],
                          mv.get("kind", "reduction"), mv.get("note", ""), event_id=evt,
                          status=mv.get("status", "open"))
        elif t == "subgoal_added":
            sg = p["subgoal"]
            self.add_required_subgoal(proof_id, p["move_id"], sg["id"], sg["description"],
                                      sg.get("parent"), event_id=evt)
        elif t == "move_updated":
            mv = p["move"]
            self.update_move_status(mv["id"], mv["status"], proof_id, evt)
        elif t == "attempt_recorded":
            a = p["attempt"]
            self.add_attempt(proof_id, a["id"], a["state_id"], a["move_summary"],
                             a.get("worker", "explorer"), a.get("note", ""),
                             a.get("move_id"), event_id=evt,
                             route_id=a.get("route_id"), model_persona=a.get("model_persona", ""),
                             disposition=a.get("disposition", ""), result_relation=a.get("result_relation", ""))
        elif t == "attempt_updated":
            a = p["attempt"]
            self.update_attempt(a["id"], a["status"], a.get("evidence", ""), proof_id, evt)
        elif t == "state_closed":
            self.close_state(p["state_id"], proof_id, p.get("reason", ""), evt)
        elif t == "state_reopened":
            self.reopen_state(proof_id, p["state_id"], p.get("reason", ""), evt)
        elif t == "claim_updated":
            self.update_claim_status(p["claim_id"], p["status"], proof_id, evt,
                                     p.get("reason", ""))
        elif t == "taint_propagated":
            self.propagate_taint(proof_id, p["claim_id"], evt, p.get("reason", ""))
        elif t == "route_added":
            r = p["route"]
            self.add_route(proof_id, r["id"], r["display_path"], evt)
        elif t == "context_added":
            c = p["context"]
            self.add_context(proof_id, c["id"], c.get("packet_hash", ""),
                             c.get("compiler_version", ""), c.get("token_budget", 0),
                             c.get("token_count", 0), evt)
        elif t == "artifact_added":
            a = p["artifact"]
            self.add_artifact(proof_id, a["id"], a.get("kind", "note"),
                              a.get("media_type", ""), a.get("sha256", ""),
                              a.get("filename", ""), evt)
        elif t == "artifact_linked":
            self.link_artifact(proof_id, p["attempt_id"], p["artifact_id"], evt)
        elif t == "attempt_route_linked":
            self.link_attempt_route(proof_id, p["attempt_id"], p["route_id"], evt)
        elif t == "attempt_context_linked":
            self.link_attempt_context(proof_id, p["attempt_id"], p["context_id"], evt)
        elif t == "claim_produced":
            self.link_produced_claim(proof_id, p["attempt_id"], p["claim_id"], evt)
        elif t == "critique_added":
            c = p["critique"]
            self.add_critique(proof_id, c["id"], p["attempt_id"], c["verdict"],
                              c.get("reason", ""), c.get("critic_worker", "critic"), evt)
        elif t == "experiment_added":
            e = p["experiment"]
            self.add_experiment(proof_id, e["id"], p["attempt_id"], e["question"],
                                e.get("status", "ran"), evt)
        elif t == "verification_added":
            v = p["verification"]
            self.add_verification(proof_id, v["id"], p["attempt_id"], p["claim_id"],
                                  v.get("kind", "lean"), v.get("status", "pending"),
                                  v.get("lean_name", ""), v.get("toolchain_hash", ""), evt)
        elif t == "bypass_added":
            self.add_relation(proof_id, "BYPASSES", p["move_id"], p["state_id"], evt, p.get("route_id", ""))
        elif t == "relation_added":
            self.add_relation(proof_id, p["rel"], p["from_id"], p["to_id"], evt, p.get("route_id", ""))
        elif t == "concept_added":
            c = p["concept"]
            self.add_concept(proof_id, c["id"], c["name"], c.get("mechanism_tags", ""), evt)
        elif t == "hypothesis_added":
            h = p["hypothesis"]
            self.add_hypothesis(proof_id, h["id"], h["kind"], h["target_state_id"],
                                h.get("falsification_test", ""), h.get("novelty", 0.0),
                                h.get("abductive_strength", 0.0), h.get("cost", 0.0),
                                h.get("risk", 0.0), h.get("lifecycle_status", "queued"), evt)
        elif t == "state_claim_link_added":
            self.link_state_claim(proof_id, p["state_id"], p["claim_id"], evt)
