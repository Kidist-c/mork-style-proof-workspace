from __future__ import annotations

from typing import Any, Dict, List


class RulesMixin:
    """Graph semantics: AND/OR closure, taint propagation, cycle detection.

    Expects the host class to provide ``self._driver`` (a neo4j.Driver).
    """

    # ------------------------------------------------------------------
    # Cycle detection (claim dependency graph)
    # ------------------------------------------------------------------

    def _would_create_cycle(
        self,
        dependent_claim_id: str,
        depends_on_claim_id: str,
        proof_id: str = "",
    ) -> bool:
        """Return True if adding DEPENDS_ON from dependent→depends_on would close a cycle."""
        with self._driver.session() as s:
            result = s.run(
                "MATCH path = (b:Claim {id: $b_id, proof_id: $pid})"
                "-[:DEPENDS_ON*1..]->(a:Claim {id: $a_id, proof_id: $pid}) "
                "RETURN path LIMIT 1",
                b_id=depends_on_claim_id, a_id=dependent_claim_id, pid=proof_id,
            )
            return result.single() is not None

    # ------------------------------------------------------------------
    # AND/OR closure (paper §9.6)
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
    # Taint propagation (paper §4.10)
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
    # Eligible frontier (paper §4.7)
    # ------------------------------------------------------------------

    def eligible_frontier(self, proof_id: str) -> List[Dict[str, Any]]:
        """Eligible moves for leasing.

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
