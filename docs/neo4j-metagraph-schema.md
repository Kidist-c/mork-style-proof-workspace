# Persistent Proof Metagraph — Neo4j Schema (v1)

*Extracted from: "A MORK-Native Multi-Agent Architecture for Long-Horizon Mathematical
Theorem Proving" (OmegaClaw/PeTTa/MORK design, 15 July 2026).*
*Scope: this document covers ONLY the Neo4j (graph) side. The append-only journal,
commit gate, revisions, and leases live in SQL per team decision.*

---

## 1. What the paper asks us to persist

The paper's central principle: **the persistent proof project is the agent**, not the LLM
conversation. Symbolic proof state is organized as **three linked DAGs in one metagraph**:

| DAG | Nodes | Semantics |
|-----|-------|-----------|
| **Search DAG** | `State` (OR point), `Move` (AND point) | A state is closed when *any* proposed move succeeds. A move succeeds only when *all* required subgoals close. Records alternatives, reductions, bypasses, supersessions, contradictions. |
| **Justification DAG** | `Claim` | Reusable propositions with discrete protocol status and exact dependencies. |
| **Provenance DAG** | `Attempt` | Binds one bounded execution of one move to state, route, context, prompt, worker, artifacts, critics, experiments, Lean runs. |
| **Speculative layer** | `Hypothesis` | Hyperon/LLM creative suggestions. Explicitly **not** usable as established premises. |

Plus a light **Concept** layer so Hyperon/PLN can attach uncertain relevance to real IDs.

Three authorities, kept separate:

```
Neo4j metagraph   semantic + query authority   (derived, rebuildable)
SQL event journal durability + replay authority (append-only, hash-chained)
Object store / SQL artifacts  byte-level authority for large blobs
```

Neo4j is **never** the source of truth. It is a *projection* that must be reproducible
from the journal (`wipe_and_rebuild`). The commit gate is the **only writer** of
committed graph state.

---

## 2. Where each concept lives (the boundary)

| Concept | Owner | Why |
|---------|-------|-----|
| Search / justification / provenance DAGs, concepts, speculative hypotheses | **Neo4j** | Graph topology, reachability, taint cones, AND/OR propagation |
| Event journal (append-only, hash-chained), proof revision counter | **SQL** | Durability + deterministic replay |
| Commit gate validation (schemas, revision, status transitions, deps, hashes) | **SQL** | Transactional, deterministic, one writer |
| Leases + fencing tokens (`proof-lease-next` must be atomic) | **SQL** | Compare-and-swap under one lock; Neo4j only gets a status projection (`Move.status='leased'`) |
| Snapshot/checkpoint digests | **SQL** | Compare replay result vs checkpoint |
| Artifact *metadata* (hash, media type, role) | **Neo4j** (`:Artifact`) | Traversable provenance |
| Artifact *bytes* (PDFs, code, results, contexts) | Content-addressed store / SQL | Keep graph lean — paper §4.8 |

---

## 3. Node types

Convention: every node carries `proof_id` (namespace = "one proof project = one MORK
namespace", paper §3.6/4.3), a stable `id`, and `created_in_event` (traceability stamp).

### `:Proof`
```
proof_id, theorem_kernel, theorem_hash, active_revision, policy
```
One per project. Anchor for everything else.

### `:State`  — OR point / goal
```
proof_id, id, kind ('or'|'goal'), status, description, assumptions,
branch_summary, created_in_event
```
`status ∈ { open, closed, tainted, reopened }`

### `:Move`  — AND point / candidate direction
```
proof_id, id, kind ('reduction'|technique family), status, move_summary,
score_components (promise/novelty/info_gain/verif_value/cost), cost_estimate,
repeated_failure_count, note, created_in_event
```
`status ∈ { queued, open, leased, refuted, dominated, exhausted, closed }`
(paper §4.7 frontier algebra: open ∪ reopened − leased − refuted − dominated − exhausted)

### `:Claim`
```
proof_id, id, statement_blob (hash→object store), status, created_in_event,
status_updated_in_event, taint_source
```
`status ∈ { conjectural, empirical, provisional, critic_accepted, lean_verified,
refuted, retracted, stale }`  (paper §2.7 / §4.6)
No numeric "confidence" — protocol status is discrete and evidence-bound.

### `:Attempt`
```
proof_id, id, worker, model_persona, disposition, result_relation
 ('proves'|'reduces-target'|'bypasses'|'strengthens-route'|'generalizes'|'barrier'|'failure'),
 status ('pending'|'supported'|'critic_accepted'|'refuted'|'retracted'),
 result_blob, created_in_event
```

### `:Route`
```
proof_id, id, display_path (e.g. root/strategy-3/lemma-2)
```
Route ≠ identity. Several routes may reach one state (paper §4.4). Attempts record both
state and route.

### `:Artifact`
```
proof_id, id, kind ('python-source'|'pdf'|'log'|'context'|...), media_type,
sha256 (full hash), filename, created_in_event
```

### `:Hypothesis`  — speculative layer
```
proof_id, id, kind ('missing-lemma-abduction'|'analogy-transfer'|'concept-blending'|
 'representation-change'|'obstruction-inversion'|'counterexample'|'random-deterministic-hybrid'|...),
 layer='speculative', abductive_strength, novelty, cost, risk,
 falsification_test, lifecycle_status, created_in_event
```

### `:Context`
```
proof_id, id, packet_hash, compiler_version, token_budget, token_count
```
The manifest that lets us reproduce exactly what a worker saw (paper §5.7).

### `:Critique`, `:Experiment`, `:Verification`
Independent reviewers/checks. `:Verification` gains `lean_name`, `toolchain_hash`,
`mathlib_revision`, `formalization_status`, `source_hash` for the incremental Lean path
(paper §11).

### `:Concept`
```
proof_id, id, name, mechanism_tags
```
For PLN relevance + shared mechanism memory (paper §9.8).

### Optional (SQL-only, not Neo4j): `:Lease` projection
Paper keeps lease atoms in MORK; we keep leases **authoritative in SQL** and only
mirror `Move.status='leased'` into Neo4j after the SQL transaction commits.

---

## 4. Relationship types (grouped by DAG)

### 4.1 Search DAG
```
(:State)-[:PROPOSES {edge_id, event_id}]->(:Move)      OR edge — candidate direction
(:Move)-[:REQUIRES  {edge_id, event_id}]->(:State)     AND edge — mandatory subgoal
(:State)-[:CHILD_OF]->(:State)                          hierarchy/tree projection
(:Move)-[:ALTERNATIVE_TO]->(:Move)                      un-tried option, kept for backtracking
(:Move)-[:SUPERSEDES]->(:Move)                          N105 pattern
(:Move)-[:GENERALIZES]->(:Move)                         conceptual compression
(:Move)-[:BYPASSES {route_id}]->(:State)                N107: route solved, literal target OPEN
(:Move)-[:STRENGTHENS_ROUTE]->(:State)
(:Move)-[:LEAVES_OPEN]->(:State)
(:Move)-[:REDUCES_TARGET]->(:State)                     worker result_relation='reduces-target'
(:Move)-[:CONTRADICTS]->(:State|:Move|:Claim)
(:Move)-[:REFORMULATES]->(:Move|:State)
(:Move)-[:FORMALIZES]->(:State|:Claim)
(:Move)-[:EXPOSES_BARRIER]->(:Obstruction)              N108/N109 pattern
```

### 4.2 Justification DAG
```
(:Claim)-[:DEPENDS_ON]->(:Claim)          exact dependency, acyclic (gate forbids cycles)
(:Claim)-[:SUPPORTED_BY]->(:Attempt|:Artifact)
(:Claim)-[:PROVED_BY]->(:Attempt|:Verification)
(:Claim)-[:CONTRADICTED_BY]->(:Claim|:Attempt)
(:Claim)-[:VERIFIED_BY]->(:Verification)
(:Claim)-[:VERIFIED_BY_EXPERIMENT]->(:Experiment)
(:Claim)-[:INVALIDATES]->(:Claim)
(:Attempt)-[:PRODUCED_CLAIM]->(:Claim)
```

### 4.3 Provenance DAG
```
(:Attempt)-[:ON_STATE]->(:State)
(:Attempt)-[:ON_MOVE]->(:Move)
(:Attempt)-[:VIA_ROUTE]->(:Route)
(:Attempt)-[:USED_CONTEXT]->(:Context)
(:Attempt)-[:PRODUCED_ARTIFACT]->(:Artifact)
(:Attempt)-[:HAD_CRITIQUE]->(:Critique)
(:Attempt)-[:RAN]->(:Experiment)
(:Attempt)-[:HAD_VERIFICATION]->(:Verification)
(:Verification)-[:OF]->(:Claim)
```

### 4.4 Speculative layer
```
(:Hypothesis)-[:TARGETS]->(:State)
(:Hypothesis)-[:SOURCE_CONCEPT]->(:Concept)
(:Hypothesis)-[:SUGGESTS]->(:Move)
(:Hypothesis)-[:REQUIRES]->(:Claim)          prerequisites
(:Hypothesis)-[:EXPECTS]->(:Claim)           expected consequences
(:Hypothesis)-[:FALSIFIED_BY]->(:Experiment)
(:Hypothesis)-[:ELABORATED_INTO]->(:Attempt) outcome tracking (§8.9)
(:Concept)-[:RELATED_TO]->(:Concept)
```

---

## 5. Conventions

1. **Reverse indexes are free.** MORK materializes forward/reverse atom pairs
   (`out s101 proposes m204 e901` / `in m204 proposed-by s101 e901`) as generated
   indexes. In Neo4j a relationship is already traversable both ways — no duplication.
   This is a major simplification vs. the MORK layout.
2. **Edge provenance.** Every relationship carries `edge_id` (the paper's `e901`) and
   `event_id` so it can be traced to the journal event that created it.
3. **Layer property.** `layer: 'committed' | 'speculative'`. Workers read committed;
   the scheduler may *read* speculative but never uses it as a premise (§4.2, §8.8).
4. **Mutation stamps.** Nodes whose status changes carry `status_updated_in_event`.
   Neo4j is a mutable projection; the SQL journal is immutable truth. Any drift is
   caught by replay + digest comparison (§14.7).
5. **Stable IDs, not display paths.** `root/strategy-3/lemma-2` is a `:Route`, never a key.
6. **And/or semantics are structural**, not prose: OR = `PROPOSES` fan-out from a State,
   AND = `REQUIRES` fan-out from a Move.

---

## 6. Constraints & indexes (Cypher DDL)

Namespace-aware uniqueness per label (proof_id + id). Composite UNIQUE is used instead of NODE KEY because NODE KEY requires Neo4j Enterprise Edition — this prototype targets Community. The `proof_id` uniqueness is a one-time migration aid for legacy data and is dropped by `_ensure_constraints()`:

```cypher
CREATE CONSTRAINT state_key IF NOT EXISTS FOR (n:State) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT move_key  IF NOT EXISTS FOR (n:Move)  REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT claim_key IF NOT EXISTS FOR (n:Claim) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT attempt_key IF NOT EXISTS FOR (n:Attempt) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT route_key IF NOT EXISTS FOR (n:Route) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT artifact_key IF NOT EXISTS FOR (n:Artifact) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT hypothesis_key IF NOT EXISTS FOR (n:Hypothesis) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT context_key IF NOT EXISTS FOR (n:Context) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT concept_key IF NOT EXISTS FOR (n:Concept) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT critique_key IF NOT EXISTS FOR (n:Critique) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT experiment_key IF NOT EXISTS FOR (n:Experiment) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT verification_key IF NOT EXISTS FOR (n:Verification) REQUIRE (n.proof_id, n.id) IS UNIQUE;
CREATE CONSTRAINT proof_key IF NOT EXISTS FOR (n:Proof) REQUIRE (n.proof_id, n.id) IS UNIQUE;

CREATE INDEX state_status IF NOT EXISTS FOR (n:State) ON (n.proof_id, n.status);
CREATE INDEX move_status IF NOT EXISTS FOR (n:Move) ON (n.proof_id, n.status);
CREATE INDEX claim_status IF NOT EXISTS FOR (n:Claim) ON (n.proof_id, n.status);
```

---

## 7. Paper atom → Neo4j mapping (representative)

| Paper atom (§4.3 / Appendix A) | Neo4j |
|---|---|
| `(proof p17 layer committed state s101)` | `(:State {proof_id:'p17', id:'s101'})` |
| `(proof p17 state-kind s101 or)` | `State.kind='or'` |
| `(proof p17 state-status s101 open)` | `State.status='open'` |
| `(proof p17 move-status m204 queued)` | `Move.status='queued'` |
| `(proof p17 out s101 proposes m204 e901)` | `(s101)-[:PROPOSES {edge_id:'e901'}]->(m204)` |
| `(proof p17 in m204 proposed-by s101 e901)` | *same rel, reverse traversal — free* |
| `(proof p17 out m204 requires s102 e902)` | `(m204)-[:REQUIRES {edge_id:'e902'}]->(s102)` |
| `(proof p17 claim-status c308 provisional)` | `Claim.status='provisional'` |
| `(proof p17 claim-statement-blob c308 b3_CLAIM)` | `Claim.statement_blob='b3_CLAIM'` |
| `(proof p17 depends-on c310 c308)` | `(c310)-[:DEPENDS_ON]->(c308)` |
| `(proof p17 attempt-at-state a411 s101)` | `(a411)-[:ON_STATE]->(s101)` |
| `(proof p17 attempt-via-route a411 r52)` | `(a411)-[:VIA_ROUTE]->(r52)` |
| `(proof p17 context-manifest a411 ctx411)` | `(a411)-[:USED_CONTEXT]->(ctx411)` |
| `(proof p17 produced-claim a411 c308)` | `(a411)-[:PRODUCED_CLAIM]->(c308)` |
| `(proof p17 artifact-blob art92 b3_4VQ7ZJ6)` | `Artifact.sha256='b3_4VQ7ZJ6'` |
| `(proof p17 produced-artifact a411 art92)` | `(a411)-[:PRODUCED_ARTIFACT]->(art92)` |
| `(proof p17 layer speculative hypothesis h701)` | `(:Hypothesis {layer:'speculative'})` |
| `(proof p17 targets-subgoal h701 s103)` | `(h701)-[:TARGETS]->(s103)` |
| `(proof p17 source-concept h701 concept17)` | `(h701)-[:SOURCE_CONCEPT]->(concept17)` |
| `(proof p17 prerequisite h701 c211)` | `(h701)-[:REQUIRES]->(c211)` |
| `(proof p17 lease-active m204 lease771)` | **SQL only** + `Move.status='leased'` projection |
| `(proof p17 move-fence m204 14)` | **SQL only** — fencing token |

---

## 8. The payoff queries (why graph persistence)

**Eligible frontier** (paper §4.7):
```cypher
MATCH (m:Move {proof_id:$pid})
WHERE m.status IN ['open','reopened']          // minus leased/refuted/dominated/exhausted
  AND NOT exists { (m)-[:REQUIRES]->(:State {status:'refuted'}) }
RETURN m;
```

**Taint cone on refute/retract** (paper §4.10 — already implemented in adapter):
```cypher
MATCH (root:Claim {id:$cid})<-[:DEPENDS_ON*1..]-(d:Claim)
SET d.status='tainted' RETURN d.id;            // then reopen dependent closed states
```

**State solved? (OR)** / **Move complete? (AND)**:
```cypher
// OR: state closed if any proposed move is complete
MATCH (s:State {id:$sid})-[:PROPOSES]->(m:Move {status:'closed'}) RETURN true;
// AND: move complete iff every REQUIRES subgoal is closed
MATCH (m:Move {id:$mid})-[:REQUIRES]->(sg:State)
WHERE all(sg2 IN collect(sg) WHERE sg2.status IN ['closed','reopened']) RETURN true;
```

**Dependency closure / reachability** (MORK PathMap / MM2 reachability equivalent):
```cypher
MATCH (c:Claim {id:$cid})-[:DEPENDS_ON*1..]->(anc:Claim) RETURN DISTINCT anc;
```

**Replay audit**: wipe namespace, replay journal events, recompute digests, diff against
SQL checkpoint — deterministic rebuild is the core guarantee (§14.7).

---

## 9. Gap analysis vs. paper — as of the commit-gate handoff

All 13 labels are implemented with composite `(proof_id, id)` UNIQUE
constraints + status indexes. The full relationship set of §4 is whitelisted
in the adapter (`_REL_WHITELIST`) and exercised by `seed_demo`. Full paper
status enums (state: `tainted/reopened`; move: `leased/refuted/dominated/
exhausted`; claim: `empirical/stale/lean_verified`) are enforced. `edge_id` +
`event_id` stamp every relationship; `Hypothesis.layer='speculative'` is
forced; AND/OR closure, frontier, taint cone, and BYPASSES (N107) queries are
implemented and verified deterministic via `wipe_and_rebuild`.

Since the 2026-08-14 handoff, three previously unjournaled mutations
(`state_reopened`, `claim_updated`, `taint_propagated`) now have journal
events + replay handlers + `ProofProject` methods, so the graph is fully
reconstructible from the journal. The exact event set, payload schemas, and
gate validation rules live in **`docs/event-contract.md`**.

Remaining open items:
- `:Obstruction` node (paper N108/N109, §4.1 `EXPOSES_BARRIER`) — referenced
  but not implemented; see `docs/event-contract.md` §8 for the two options.
- Committed graph state currently mirrors the journal as-is; the SQL commit
  gate (next task) adds hash-chaining, leases, fencing, and validation so the
  gate becomes the single writer.
