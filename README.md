# Rapid prototype for a MORK-style proof workspace

This workspace now contains a small Python prototype of the paper's core ideas:

- a persistent proof project directory
- an append-only JSONL event journal
- a simple state/claim/attempt graph
- artifact storage and snapshot export

## Quick start

```bash
cd /home/tsigemariam/rag-rag
python3 -m unittest discover -s tests
```

## Example usage

```bash
python3 - <<'PY'
from proof_proto import ProofProject

project = ProofProject("/tmp/my-proof-project", "For all n, n^2 + n is even")
project.add_state("root", "Initial theorem state")
project.add_claim("claim-1", "n^2 + n is always even")
project.record_attempt("attempt-1", "root", "Try parity decomposition")
project.write_artifact("notes", "We should test odd and even cases")
project.export_snapshot()
print(project.context_for("root"))
PY
```

## What this prototype captures

1. Persistent state on disk instead of chat history
2. Stable records for attempts and artifacts
3. A basic claim ledger and status model
4. A simple event journal that can be replayed or audited

The next step would be to add a real planner/critic loop, a Lean adapter, or a small web UI.
