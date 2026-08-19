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
        "--provider",
        choices=("auto", "groq", "ollama"),
        default="auto",
        help="LLM provider to use; auto picks Groq when GROQ_API_KEY is set, else Ollama",
    )
    parser.add_argument(
        "--model",
        help="Model name for the selected provider",
    )
    parser.add_argument(
            "--toolchain",
            default="",
            help="Lean toolchain identifier to record on formalized claims (§11.2); "
            "informational only unless a pinned lake project is wired in",
        )
    parser.add_argument(
            "--mathlib-revision",
            default="",
            help="mathlib revision to record on formalized claims (§11.2); informational only",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="proof-workflow-"))
    llm_client = make_llm_client(
        provider=args.provider,
        model=args.model,
        ollama_url=args.ollama_url,
    )

    result = run_workflow(
        theorem=args.theorem,
        root=str(root),
        llm_client=llm_client,
        max_iterations=args.max_iterations,
        toolchain=args.toolchain,
        mathlib_revision=args.mathlib_revision,
    )

    project = result["project"]
    try:
        attempts = project.graph.get_attempts_for_state("root", project.proof_id)
        moves = project.graph.get_moves_for_state("root", project.proof_id)
        state = project.graph.get_state("root", project.proof_id)

        print()
        print("=" * 60)
        print(f"  THEOREM: {args.theorem}")
        print("=" * 60)
        print(f"  proof_id : {project.proof_id}")
        print(f"  status   : {state['status'].upper()}")
        if state['status'] == 'closed':
            print(f"  closed   : {state.get('closed_reason', '')}")
        print(f"  snapshot : {result['snapshot_path']}")
        print()
        print(f"  MOVES ({len(moves)} total) — search DAG")
        print("-" * 60)
        for m in reversed(moves):
            print(f"  [{m['id']}]  kind: {m.get('kind', 'reduction')}  status: {m['status']}")
            print(f"  Move    : {m['move_summary']}")
            subgoals = project.graph.get_subgoals_for_move(m["id"], project.proof_id)
            if subgoals:
                print(f"  Subgoals ({len(subgoals)}):")
                for sg in subgoals:
                    print(f"    - [{sg['id']}] {sg['description']}")
            print()
        print(f"  ATTEMPTS ({len(attempts)} total)")
        print("-" * 60)
        for a in reversed(attempts):
            print(f"  [{a['id']}]  status: {a['status']}")
            print(f"  Move    : {a['move_summary']}")
            print(f"  Claim   : {a['note']}")
            print(f"  Verdict : {a.get('evidence', 'pending — no verdict yet')}")
            print()
        print("=" * 60)
        claims = project.graph.get_all_claims(project.proof_id)
        lean_claims = [c for c in claims if c.get("formalization_status")]
        if lean_claims:
                print(f"  LEAN FORMALIZATIONS ({len(lean_claims)} total)")
                print("-" * 60)
                for c in lean_claims:
                    print(f"  [{c['id']}]  formalization_status: {c['formalization_status']}")
                    print(f"  Informal : {c['statement']}")
                    if c.get("lean_name"):
                         print(f"  Lean name: {c['lean_name']}  (namespace: {c.get('lean_namespace', '')})")
                    if c.get("lean_statement_path"):
                         print(f"  Lean file: {c['lean_statement_path']}")
                    print()
                print("=" * 60)
    finally:
        project.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
