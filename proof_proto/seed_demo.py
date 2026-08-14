"""Seed a demo proof that exercises the full Neo4j metagraph schema.

The demo mirrors the paper's N107 pattern (section 13.4):
  - a literal goal stays OPEN,
  - a deterministic-shield move BYPASSES it for a route objective,
  - claims form a DEPENDS_ON chain,
  - attempts carry provenance (route, context, artifacts, critic, experiment),
  - one speculative Hyperon hypothesis sits in the speculative layer.

It is fully journal-backed: wipe_and_rebuild() reproduces the same graph.

Run:  python3 -m proof_proto.seed_demo
Then: python3 -m proof_proto.visualize demo-bypass
"""

from __future__ import annotations

import shutil
from pathlib import Path

from proof_proto.proof_project import ProofProject

DEMO_ROOT = Path(__file__).resolve().parent.parent / "proofs" / "demo-bypass"


def seed() -> ProofProject:
    if DEMO_ROOT.exists():
        shutil.rmtree(DEMO_ROOT)
    DEMO_ROOT.mkdir(parents=True)
    theorem = (
        "For every random cubic XAG and every adversary family A, the "
        "encoding collapses below cubic effective entropy."
    )
    p = ProofProject(DEMO_ROOT, theorem)

    # -- search DAG -------------------------------------------------------
    p.add_state("g-random-cubic", "Literal target: random-cubic encoding collapses", kind="goal")
    p.add_state("sub-shield", "Route objective: deterministic algebraic shield")

    m_closure = p.add_move(
        "g-random-cubic",
        "Direct moment/union-bound compression of all even moments",
        kind="reduction",
        note="fails: quadratic budget too small (N108 obstruction)",
    )
    p.add_subgoal(m_closure["id"], "Bound the 2k-th moment for all k", "g-random-cubic")
    p.update_move_status(m_closure["id"], "refuted")

    m_shield = p.add_move(
        "g-random-cubic",
        "Deterministic algebraic shield over the modified ensemble",
        kind="shield",
        note="N107/N108/N109 hybrid: spectral + Hall-expander shield, random quadratic core",
    )
    p.add_subgoal(m_shield["id"], "Show bad translations form a sparse set", "g-random-cubic")
    p.update_move_status(m_shield["id"], "open")

    # routes (route ≠ identity; distinct routes may reach the same state)
    r_spectral = p.add_route("r-spectral", "root/spectral-shield")
    p.add_relation("STRENGTHENS_ROUTE", m_shield["id"], "g-random-cubic", r_spectral["id"])
    # N107: bypass solves the ROUTE objective but MUST NOT close the literal target
    p.add_bypass(m_shield["id"], "sub-shield", r_spectral["id"])

    # -- justification DAG ------------------------------------------------
    c_barrier = p.add_claim("c-cb-core", "General quadratic Cayley-Bacharach core theorem")
    c_mom = p.add_claim("c-even-moments", "All-even-moment theorem follows from the CB core")
    c_shield = p.add_claim(
        "c-shield-injective",
        "Deterministic algebraic shield is injective for the modified ensemble",
        status="provisional",
    )
    p.add_claim_dependency("c-even-moments", "c-cb-core")
    p.add_claim_dependency("c-shield-injective", "c-cb-core")
    p.link_state_claim("g-random-cubic", "c-even-moments")
    p.link_state_claim("sub-shield", "c-shield-injective")

    # -- provenance DAG ---------------------------------------------------
    a_shield = p.record_attempt(
        "att-108",
        "g-random-cubic",
        "Spectral shield + random quadratic core amplifies entropy budget to cubic scale",
        worker="explorer",
        note="obstruction inversion: transform adversary class into rare bad-translation set",
        move_id=m_shield["id"],
        route_id=r_spectral["id"],
        model_persona="explorer-analogy",
        disposition="reduction",
        result_relation="strengthens-route",
    )
    p.add_context_packet("ctx-108", packet_hash="sha256:deadbeef108", token_budget=60000, token_count=43192)
    p.link_attempt_context(a_shield["id"], "ctx-108")
    p.link_attempt_route(a_shield["id"], r_spectral["id"])
    p.link_produced_claim(a_shield["id"], c_shield["id"])
    p.write_artifact("verify_n108_spectral_shield.py",
                     "exhaustive finite checks for n<=8\n", kind="python-source", attempt_id=a_shield["id"])
    p.add_critique(a_shield["id"], "critic_accepted",
                   "No local defect under recorded assumptions; checks the exact statement", "critic-1")
    p.add_experiment(a_shield["id"], "Does the core-dimension bound fail for n <= 8?", status="ran")
    p.add_verification(a_shield["id"], c_shield["id"], kind="lean", status="pending",
                       lean_name="Proof_demo_b.c_shield_injective", toolchain_hash="sha256:lean4-4.16.0")

    # -- speculative layer (Hyperon) --------------------------------------
    p.add_concept("concept-rank-collapse", "rank collapse", mechanism_tags="low-degree,hiding-place")
    p.add_concept("concept-spectral-bias", "spectral bias", mechanism_tags="dense-language,sparse-bad-translations")
    h = p.add_hypothesis(
        "h701",
        "representation-change",
        "g-random-cubic",
        falsification_test="test finite XAG cells for multiplicative-skeleton certificate (n<=8)",
        novelty=0.81,
        abductive_strength=0.74,
        cost=0.4,
        risk=0.5,
    )
    p.add_relation("SOURCE_CONCEPT", h["id"], "concept-rank-collapse")
    p.add_relation("SOURCE_CONCEPT", h["id"], "concept-spectral-bias")
    p.add_relation("SUGGESTS", h["id"], m_shield["id"])
    p.add_relation("EXPECTS", h["id"], c_shield["id"])
    p.add_relation("RELATED_TO", "concept-rank-collapse", "concept-spectral-bias")

    # -- the claim that proves the literal target is NOT closed ------------
    assert p.graph.get_state("g-random-cubic", p.proof_id)["status"] == "open", (
        "N107 invariant broken: a bypass must not close the literal target"
    )
    print(f"seeded demo-bypass: {DEMO_ROOT}")
    print("  literal target g-random-cubic status:", p.graph.get_state("g-random-cubic", p.proof_id)["status"])
    print("  frontier (eligible moves):", [m["id"] for m in p.graph.eligible_frontier(p.proof_id)])
    return p


if __name__ == "__main__":
    seed().close()
