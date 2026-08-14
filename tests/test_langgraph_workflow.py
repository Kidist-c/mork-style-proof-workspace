import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from proof_proto.langgraph_workflow import run_workflow


class DummyLLM:
    def explore(self, theorem: str, context: dict) -> dict:
        return {
            "move_summary": "Try parity decomposition",
            "claim_statement": "n^2 + n is even",
            "required_subgoals": ["Show n^2 and n have the same parity"],
        }

    def critique(self, theorem: str, move_summary: str, claim_statement: str, context: dict) -> dict:
        return {
            "decision": "continue",
            "reason": "The parity split is a sensible next step",
            "status": "supported",
        }


class LangGraphWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="langgraph-proof-", dir="/tmp")

    def tearDown(self) -> None:
        result = getattr(self, "_result", None)
        if result:
            result["project"].close()
        shutil.rmtree(self.temp_dir)

    def test_formal_verifier_is_theorem_agnostic(self) -> None:
        from proof_proto.langgraph_workflow import FormalVerifier

        verdict = FormalVerifier().verify(
            "For all integers n, n^2 + n is divisible by 2",
            [{"move_summary": "rewrite the goal", "status": "supported"}],
        )

        self.assertFalse(verdict["closed"])
        self.assertIn("proof", verdict["reason"].lower())

    def test_module_cli_invokes_main(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "proof_proto.cli", "--help"],
            capture_output=True,
            text=True,
            cwd="/home/tsigemariam/ben's-idea-prototype",
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Run one theorem through the proof workflow", result.stdout)

    def test_run_workflow_creates_state_and_attempt(self) -> None:
        self._result = run_workflow(
            theorem="For all n, n^2 + n is even",
            root=self.temp_dir,
            llm_client=DummyLLM(),
            max_iterations=1,
        )
        result = self._result

        self.assertEqual(result["theorem"], "For all n, n^2 + n is even")
        self.assertIn("project", result)

        # Verify attempts exist in Neo4j via graph traversal
        attempts = result["project"].graph.get_attempts_for_state("root", result["project"].proof_id)
        self.assertGreaterEqual(len(attempts), 1)

        # Verify the search DAG: explorer proposed a Move, which required a subgoal
        moves = result["project"].graph.get_moves_for_state("root", result["project"].proof_id)
        self.assertGreaterEqual(len(moves), 1)
        first_move = moves[0]
        subgoals = result["project"].graph.get_subgoals_for_move(
            first_move["id"], result["project"].proof_id
        )
        self.assertEqual(len(subgoals), 1)

        # Verify root state is in Neo4j
        root_state = result["project"].graph.get_state("root", result["project"].proof_id)
        self.assertEqual(root_state["status"], "open")
        self.assertTrue(Path(self.temp_dir, "journal.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
