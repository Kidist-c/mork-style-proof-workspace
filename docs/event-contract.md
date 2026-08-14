# Journal Event Contract — for the Commit-Gate Implementer

*Owner: this document is the handoff between the Neo4j-schema task and the
commit-gate (SQL) task. The Neo4j side is DONE and verified; the gate must
validate exactly the event set below before any write reaches Neo4j.*

*Companion doc: `docs/neo4j-metagraph-schema.md` (graph side). Source of truth
for the paper: `sources/paper_text.txt`.*

---

## 1. Authority model (what the gate must enforce)

```
SQL journal      durability + replay authority  (append-only, hash-chained)
SQL commit gate  the ONLY writer of committed graph state (validate + emit)
Neo4j metagraph  semantic + query authority      (derived, rebuildable)
```

1. The gate receives an event, validates it (schema, enums, referential
   integrity, status transitions, dependency acyclicity, hashes), then writes
   it to the journal *and* applies it to Neo4j.
2. Neo4j is **never** the source of truth. Every graph mutation is stamped with
   the `event_id` that caused it. Any drift is detected by `wipe_and_rebuild` +
   digest comparison.
3. Leases and fencing tokens stay in SQL; Neo4j only sees the
   `Move.status='leased'` projection after the SQL transaction commits.

---

## 2. Event envelope

Every journal line is one JSON object:

```json
{
  "id": "evt-001",
  "type": "state_added",
  "timestamp": "2026-08-14T08:47:06+00:00",
  "payload": { }
}
```

| Field | Rule (gate validates) |
|---|---|
| `id` | Monotonic per proof: `evt-<n>` with `n = len(events)+1`. No gaps, no reuse. |
| `type` | Must be one of the 28 types in §5. Unknown type → reject. |
| `timestamp` | ISO-8601 UTC. |
| `payload` | Frozen schema per type (§5). Extra keys → reject. |

Every relationship written to Neo4j carries `edge_id` (= `event_id-kind`) and
`event_id`, so every edge is traceable to its creating event.

---

## 3. Status enums (authoritative — mirror of `proof_proto/neo4j_adapter.py`)

```python
STATE_STATUSES  = {"open", "closed", "tainted", "reopened"}
MOVE_STATUSES   = {"queued", "open", "leased", "refuted", "dominated", "exhausted", "closed"}
CLAIM_STATUSES  = {"conjectural", "empirical", "provisional", "critic_accepted",
                   "lean_verified", "refuted", "retracted", "stale"}
ATTEMPT_STATUSES = {"pending", "supported", "critic_accepted", "refuted", "retracted"}
```

No numeric "confidence" exists — status is discrete and evidence-bound.

---

## 4. Relationship whitelist (for `relation_added` / `bypass_added`)

The gate must reject any `rel` not in this set (prevents type injection):

```
Search DAG:      SUPERSEDES, ALTERNATIVE_TO, GENERALIZES, REFORMULATES,
                 FORMALIZES, CONTRADICTS, STRENGTHENS_ROUTE, LEAVES_OPEN,
                 REDUCES_TARGET, EXPOSES_BARRIER, BYPASSES
Justification:   SUPPORTED_BY, PROVED_BY, CONTRADICTED_BY, VERIFIED_BY,
                 VERIFIED_BY_EXPERIMENT, INVALIDATES
State→Claim:     USES_CLAIM
Speculative:     SUGGESTS, EXPECTS, SOURCE_CONCEPT, RELATED_TO, FALSIFIED_BY,
                 ELABORATED_INTO
```

Dedicated edges (`PROPOSES`, `REQUIRES`, `CHILD_OF`, `DEPENDS_ON`, `ON_STATE`,
`ON_MOVE`, `VIA_ROUTE`, `USED_CONTEXT`, `PRODUCED_CLAIM`, `PRODUCED_ARTIFACT`,
`HAD_CRITIQUE`, `RAN`, `HAD_VERIFICATION`, `OF`, `TARGETS`, `HAS_STATE`,
`HAS_CLAIM`) are emitted by their own event types, not via `relation_added`.

---

## 5. Event catalog (28 types)

Payload schema + gate validation + Neo4j replay handler.

| # | `type` | payload | Neo4j handler | Gate must validate |
|---|--------|---------|---------------|--------------------|
| 1 | `project_init` | `{theorem_kernel, created_at}` | `init_proof` | exactly one per proof; first event |
| 2 | `state_added` | `state: {id, description, parent?, kind, status, assumptions}` | `add_state` | `kind ∈ {or, goal, and}`; parent exists if set; unique `(proof_id, id)` |
| 3 | `claim_added` | `claim: {id, statement, status}` | `add_claim` | `status ∈ CLAIM_STATUSES`; unique id |
| 4 | `claim_dependency_added` | `{dependent_claim_id, depends_on_claim_id}` | `add_claim_dependency` | both claims exist; **no cycle** (DEPENDS_ON must stay acyclic) |
| 5 | `move_added` | `move: {id, state_id, move_summary, kind, note, status}` | `add_move` | `status ∈ MOVE_STATUSES`; state exists; creates `PROPOSES` edge |
| 6 | `subgoal_added` | `{move_id, subgoal: {id, description, parent?}}` | `add_required_subgoal` | move exists; creates `REQUIRES` AND-edge |
| 7 | `move_updated` | `move: {id, status}` | `update_move_status` | `status ∈ MOVE_STATUSES`; legal transition |
| 8 | `attempt_recorded` | `attempt: {id, state_id, move_summary, worker, note, status, route_id?, model_persona, disposition, result_relation, move_id?}` | `add_attempt` | state exists; `move_id`/`route_id` exist if set; unique id |
| 9 | `attempt_updated` | `attempt: {id, status, evidence}` | `update_attempt` | `status ∈ ATTEMPT_STATUSES`; attempt exists |
| 10 | `state_closed` | `{state_id, reason}` | `close_state` (runs AND/OR closure to fixpoint) | state exists; state not already closed |
| 11 | `state_reopened` | `{state_id, reason}` | `reopen_state` | state exists; state was closed |
| 12 | `claim_updated` | `{claim_id, status, reason}` | `update_claim_status` | `status ∈ CLAIM_STATUSES`; claim exists |
| 13 | `taint_propagated` | `{claim_id, reason}` | `propagate_taint` (refutes root, taints DEPENDS_ON cone, reopens dependent states) | claim exists; root must not already be refuted |
| 14 | `route_added` | `route: {id, display_path}` | `add_route` | unique id |
| 15 | `context_added` | `context: {id, packet_hash, compiler_version, token_budget, token_count}` | `add_context` | unique id |
| 16 | `artifact_added` | `artifact: {id, kind, media_type, sha256, filename}` | `add_artifact` | `sha256` format; unique id |
| 17 | `artifact_linked` | `{attempt_id, artifact_id}` | `link_artifact` | both exist |
| 18 | `attempt_route_linked` | `{attempt_id, route_id}` | `link_attempt_route` | both exist |
| 19 | `attempt_context_linked` | `{attempt_id, context_id}` | `link_attempt_context` | both exist |
| 20 | `claim_produced` | `{attempt_id, claim_id}` | `link_produced_claim` | both exist |
| 21 | `critique_added` | `{attempt_id, critique: {id, verdict, reason, critic_worker}}` | `add_critique` | attempt exists; unique id |
| 22 | `experiment_added` | `{attempt_id, experiment: {id, question, status}}` | `add_experiment` | attempt exists; unique id |
| 23 | `verification_added` | `{attempt_id, claim_id, verification: {id, kind, status, lean_name, toolchain_hash}}` | `add_verification` | attempt + claim exist; unique id |
| 24 | `relation_added` | `{rel, from_id, to_id, route_id}` | `add_relation` | `rel ∈` §4 whitelist; both endpoints exist; route_id exists if set |
| 25 | `bypass_added` | `{move_id, state_id, route_id}` | `add_relation(BYPASSES)` | move + state + route exist; **must NOT close the literal target** (N107) |
| 26 | `concept_added` | `concept: {id, name, mechanism_tags}` | `add_concept` | unique id |
| 27 | `hypothesis_added` | `hypothesis: {id, kind, target_state_id, falsification_test, novelty, abductive_strength, cost, risk}` | `add_hypothesis` | `layer='speculative'` is forced; target state exists |
| 28 | `state_claim_link_added` | `{state_id, claim_id}` | `link_state_claim` | both exist |

Notes:
- Events 1–10 existed before this handoff; events **11–13 are new** (added so
  refute/taint, claim-status change, and reopen are journaled and replayable —
  they were previously adapter-only and caused graph/journal drift).
- `state_closed` triggers AND/OR closure propagation (`_propagate_closures`);
  the gate should not emit a separate event for each propagated closure.
- `bypass_added` is structurally distinct from `PROPOSES`, so closing a route
  via bypass never closes the literal target (N107) — enforced by the graph,
  do not "helpfully" close it in the gate.

---

## 6. Replay contract

```python
adapter.wipe_and_rebuild(proof_id, events)   # DELETE namespace, replay in order
```

1. Replaying a journal must reproduce the graph **exactly** — node labels,
   relationship types, statuses. Verified by `tests/test_proof_project.py`
   (`test_reopen_and_claim_update_are_journaled_and_replayable`,
   `test_taint_propagation_is_journaled_and_replayable`) and the
   `demo-bypass` inventory comparison.
2. The gate's digest check (paper §14.7): replay the journal, compute a graph
   digest, compare against the stored checkpoint. Any mismatch = drift bug.
3. Never call adapter mutation methods without a journal event behind them.
   The journaled entry points are `ProofProject` methods — use those as the
   model for the gate's writer.

---

## 7. What stays SQL-side (the gate's job — not in Neo4j)

- Append-only, **hash-chained** journal (each line includes the hash of the
  previous line).
- Proof **revision counter** and optimistic concurrency.
- **Leases + fencing tokens** (`proof-lease-next` compare-and-swap). Neo4j only
  gets the `Move.status='leased'` projection after commit.
- Commit validation per §5 (schema, enums, transitions, deps, cycles, hashes).
- Snapshot/checkpoint digests.

---

## 8. Open schema decision (please resolve before wiring the gate)

- **`:Obstruction` node**: the paper's N108/N109 pattern and the schema doc
  (§4.1 `(:Move)-[:EXPOSES_BARRIER]->(:Obstruction)`) reference an
  `Obstruction` node, but it is **not implemented** — no label, no constraint,
  no replay handler. `seed_demo` currently records the obstruction only as note
  text on a move. Options:
  1. Add `:Obstruction` to `_LABELS` + a UNIQUE constraint + an
     `obstruction_added` event + `add_obstruction`/replay, or
  2. Amend the schema doc to drop `:Obstruction` and keep `EXPOSES_BARRIER`
     targeting a `State`/`Claim`, with obstruction text on the move.
  Recommendation: option 1 if the gate wants typed barrier tracking for §8.9
  "diagnosis of repeated failures"; otherwise option 2.

---

## 9. Handoff verification checklist (ran green 2026-08-14)

- [x] `pytest tests/` → **8 passed**
- [x] `python3 -m proof_proto.seed_demo` → full metagraph, N107 invariant holds
- [x] `python3 -m proof_proto.visualize demo-bypass` → 20 nodes, 35 edges
- [x] wipe + replay reproduces identical node/rel inventory
- [x] constraints: 13 composite `(proof_id, id)` UNIQUE; indexes on State/Move/Claim status
