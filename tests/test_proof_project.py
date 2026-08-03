import shutil
import tempfile
import unittest
from pathlib import Path

from proof_proto import ProofProject


class ProofProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="proof-proto-", dir="/tmp")
        self.project = ProofProject(self.temp_dir, "For all n, n^2 + n is even")

    def tearDown(self) -> None:
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

    def test_export_snapshot_writes_json(self) -> None:
        self.project.add_state("root", "Initial theorem state")
        snapshot = self.project.export_snapshot()
        self.assertTrue(snapshot.exists())
        self.assertIn("states", snapshot.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
