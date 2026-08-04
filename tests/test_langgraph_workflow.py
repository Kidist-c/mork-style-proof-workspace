import shutil
import tempfile
import unittest
from pathlib import Path

from proof_proto.langgraph_workflow import run_workflow


class DummyLLM:
    def explore(self, theorem: str, context: dict) -> dict:
        return {
            "move_summary": "Try parity decomposition",
            "claim_statement": "n^2 + n is even",
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

        # Verify root state is in Neo4j
        root_state = result["project"].graph.get_state("root", result["project"].proof_id)
        self.assertEqual(root_state["status"], "open")
        self.assertTrue(Path(self.temp_dir, "journal.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
