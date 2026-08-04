from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from proof_proto.langgraph_workflow import make_llm_client, run_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one theorem through the proof workflow")
    parser.add_argument("--theorem", required=True, help="The theorem statement to test")
    parser.add_argument(
        "--root",
        help="Workspace directory for the proof project; defaults to a temporary directory",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=1,
        help="How many explore/critique cycles to run",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://127.0.0.1:11434",
        help="Base URL for the local Ollama server",
    )
    parser.add_argument(
        "--model",
        default="llama3.1:8b",
        help="Model name to use with Ollama",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="proof-workflow-"))
    llm_client = make_llm_client()

    result = run_workflow(
        theorem=args.theorem,
        root=str(root),
        llm_client=llm_client,
        max_iterations=args.max_iterations,
    )

    project = result["project"]
    try:
        attempts = project.graph.get_attempts_for_state("root", project.proof_id)
        state = project.graph.get_state("root", project.proof_id)

        print()
        print("=" * 60)
        print(f"  THEOREM: {args.theorem}")
        print("=" * 60)
        print(f"  proof_id : {project.proof_id}")
        print(f"  status   : {state['status']}")
        print(f"  snapshot : {result['snapshot_path']}")
        print()
        print(f"  ATTEMPTS ({len(attempts)} total)")
        print("-" * 60)
        for a in reversed(attempts):
            print(f"  [{a['id']}]  status: {a['status']}")
            print(f"  Move    : {a['move_summary']}")
            print(f"  Claim   : {a['note']}")
            print(f"  Verdict : {a['evidence']}")
            print()
        print("=" * 60)
    finally:
        project.close()

    return 0