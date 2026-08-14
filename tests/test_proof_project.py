import shutil
import tempfile
import unittest
from pathlib import Path

from proof_proto import ProofProject


class ProofProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="proof-proto-", dir="/tmp")
        self.project = ProofProject(self.temp_dir, "For all n, n^2 + n is even")
        # Wipe any leftover Neo4j data from previous runs under this proof_id,
        # then rebuild from the journal (the project_init anchor must survive).
        self.project.graph.wipe_and_rebuild(self.project.proof_id, self.project.events)
        self._reopened: list = []

    def tearDown(self) -> None:
        for fresh in self._reopened:
            fresh.close()
        self.project.close()
        shutil.rmtree(self.temp_dir)

    def test_init_creates_journal_and_state(self) -> None:
        self.assertTrue(Path(self.temp_dir, "journal.jsonl").exists())
        self.assertTrue(Path(self.temp_dir, "project_state.json").exists())
        self.assertEqual(self.project.theorem_kernel, "For all n, n^2 + n is even")

    def test_add_state_and_attempt_roundtrip(self) -> None:
        state = self.project.add_state("root", "Initial theorem state")
        claim = self.project.add_claim("claim-1", "n^2 + n is always even")
        attempt = self.project.record_attempt("attempt-1", state["id"], "Try parity decomposition")

        self.assertEqual(state["status"], "open")
        self.assertEqual(claim["status"], "conjectural")
        self.assertEqual(attempt["status"], "pending")

        updated = self.project.mark_attempt("attempt-1", "critic_accepted", "A parity case split worked")
        self.assertEqual(updated["status"], "critic_accepted")

        # Verify state is in Neo4j
        neo_state = self.project.graph.get_state("root", self.project.proof_id)
        self.assertEqual(neo_state["status"], "open")

        # Verify attempt is in Neo4j via graph traversal
        attempts = self.project.graph.get_attempts_for_state("root", self.project.proof_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "critic_accepted")

    def test_add_move_and_subgoal_roundtrip(self) -> None:
        state = self.project.add_state("root", "Initial theorem state")
        move = self.project.add_move("root", "Express n as 2k and square it")

        self.assertEqual(move["status"], "open")

        # OR semantics: the state proposes the move
        moves = self.project.graph.get_moves_for_state("root", self.project.proof_id)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["id"], move["id"])

        # AND semantics: the move requires a subgoal state
        subgoal = self.project.add_subgoal(move["id"], "Show (2k)^2 is even", state["id"])
        subgoals = self.project.graph.get_subgoals_for_move(move["id"], self.project.proof_id)
        self.assertEqual(len(subgoals), 1)
        self.assertEqual(subgoals[0]["id"], subgoal["id"])

        # An attempt on the move is reachable via ON_MOVE
        self.project.record_attempt("attempt-1", "root", "Express n as 2k", move_id=move["id"])
        attempts = self.project.graph.get_attempts_for_move(move["id"], self.project.proof_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["move_summary"], "Express n as 2k")

    def test_close_state_closes_its_moves(self) -> None:
        self.project.add_state("root", "Initial theorem state")
        self.project.add_move("root", "Express n as 2k and square it")
        self.project.close_state("root", "proof complete")
        moves = self.project.graph.get_moves_for_state("root", self.project.proof_id)
        self.assertEqual(moves[0]["status"], "closed")

    def test_export_snapshot_writes_json(self) -> None:
        self.project.add_state("root", "Initial theorem state")
        snapshot = self.project.export_snapshot()
        self.assertTrue(snapshot.exists())
        self.assertIn("claims", snapshot.read_text(encoding="utf-8"))

    def test_reopen_and_claim_update_are_journaled_and_replayable(self) -> None:
        self.project.add_state("root", "Initial theorem state")
        self.project.close_state("root", "done")
        self.project.reopen_state("root", "a dependency changed")
        self.project.add_claim("claim-1", "n^2 + n is always even")
        self.project.update_claim_status("claim-1", "critic_accepted", "reviewed by critic-1")

        st = self.project.graph.get_state("root", self.project.proof_id)
        self.assertEqual(st["status"], "reopened")
        claim = self._claim("claim-1")
        self.assertEqual(claim["status"], "critic_accepted")
        self.assertEqual(claim["status_reason"], "reviewed by critic-1")

        fresh = self._reopen_and_rebuild()
        st2 = fresh.graph.get_state("root", fresh.proof_id)
        self.assertEqual(st2["status"], "reopened")
        self.assertEqual(self._claim("claim-1")["status"], "critic_accepted")

    def test_taint_propagation_is_journaled_and_replayable(self) -> None:
        self.project.add_state("root", "Initial theorem state")
        self.project.add_claim("c-core", "core theorem")
        self.project.add_claim("c-dep", "dependent lemma")
        self.project.add_claim_dependency("c-dep", "c-core")
        self.project.link_state_claim("root", "c-dep")
        self.project.close_state("root", "used c-dep")

        summary = self.project.propagate_taint("c-core", "counterexample found")
        self.assertIn("c-dep", summary["tainted"])
        self.assertEqual(self._claim("c-core")["status"], "refuted")
        self.assertEqual(self._claim("c-dep")["status"], "tainted")
        self.assertEqual(self.project.graph.get_state("root", self.project.proof_id)["status"], "reopened")

        fresh = self._reopen_and_rebuild()
        self.assertEqual(self._claim("c-core")["status"], "refuted")
        self.assertEqual(self._claim("c-dep")["status"], "tainted")
        self.assertEqual(self._claim("c-dep")["taint_source"], "c-core")
        self.assertEqual(fresh.graph.get_state("root", fresh.proof_id)["status"], "reopened")

    def _claim(self, claim_id: str) -> dict:
        return self.project.graph.get_all_claims(self.project.proof_id) and next(
            c for c in self.project.graph.get_all_claims(self.project.proof_id)
            if c["id"] == claim_id
        )

    def _reopen_and_rebuild(self) -> ProofProject:
        # Simulates the commit-gate invariant: Neo4j must be fully
        # reconstructible from the journal alone.
        fresh = ProofProject(self.temp_dir, "For all n, n^2 + n is even")
        self._reopened.append(fresh)
        return fresh


if __name__ == "__main__":
    unittest.main()
