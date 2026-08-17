# ADR 0012 — The API stops constructing and holding a compiled graph and an LLM client

- **Status:** **Accepted, 2026-08-17.** Not built. Depends on
  [ADR 0011](0011-the-human-gate-resume-moves-to-the-worker.md) landing first.
  **Corrected on acceptance** — the checkpoint-read inventory below was written before
  [ADR 0013](0013-reviewer-gate-payload-view.md) shipped `GET /jobs/{job_id}/gate`, and named one
  read where there are now two. The decision itself is unchanged
- **Date:** 2026-08-16
- **Affects:** `app.py` (`_production_graph`, `_build`) · `routes/api.py` (`RouteDeps`, `_phase`,
  `_live_state`, `_gate_visit`, `read_gate`, `_reconcile_status`) · `config.py` ·
  `tests/test_config.py` · `docs/ARCHITECTURE.md` §10, §13, §19 ·
  `docs/engineering-guidelines.md` §12 · `CLAUDE.md` commands
- **Found by:** The Phase 3 readiness audit of 2026-08-16 (decision D7, risk G.4)
- **Relates to:** [ADR 0011](0011-the-human-gate-resume-moves-to-the-worker.md) removes the only
  reason the API *executes* a graph; this removes the reason it *holds* one.
  [ADR 0007](0007-reviewer-decision-idempotency-and-gate-resume-failure.md) invariant 4 is what makes
  decision 2 below sound

---

## Context

`app.py::_production_graph` builds the full graph for the API process:

```python
checkpointer = postgres_checkpointer(database_url).__enter__()
client = LLMClient(config, client=OpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key))
return build_graph(config=config, llm=client, db=engine, checkpointer=checkpointer)
```

So the API imports every agent, the tool boundary and the LLM client, and constructs a real `OpenAI`
client at startup. `config.load_config()` makes `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` and
`TAVILY_API_KEY` required, so the process will not start without them. CLAUDE.md's Commands section
records this and why: *"It needs `DATABASE_URL`, `AUTH_KEYS`, and the `LLM_*` credentials, because the
API resumes the graph at the gate itself until the Phase 3 worker takes that over."*

Two documents say this must not be true from Phase 3:

| Document | Statement |
|---|---|
| §19, API container | **Never** — *"Runs the graph, calls the LLM, calls a tool, or fetches a web page"* |
| §13, Least privilege | API task role: *"Read/write its Postgres schema, send to the job queue, presign objects under its S3 prefix, read the auth secret"* — the LLM and Tavily secrets belong to the **worker** role alone |

### What the API actually needs from the graph, measured against the code

**Corrected 2026-08-17.** This table originally listed four call sites and was written before
[ADR 0013](0013-reviewer-gate-payload-view.md) added `GET /jobs/{job_id}/gate`. There are five, and
after ADR 0011 **three** remain:

| Call site | Needs | Still needed after ADR 0011? |
|---|---|---|
| `_phase` | `get_state(...).next` — the node about to run | Yes |
| `_gate_visit` | `values["llm_calls_used"]` — ADR 0007's visit key and ADR 0006's budget input | Yes |
| `read_gate` | `values` — the whole `ResearchState`, for `reviewer_payload()` (ADR 0013) | **Yes** |
| `_reconcile_status` | `get_state(...).interrupts` | **No** — ADR 0011 decision 4 moves it to the worker |
| `_resume` | `invoke(Command(resume=...))` | **No** — ADR 0011 decision 1 |

So the API keeps **two checkpoint reads and no graph dependency**: a scalar for the gate-visit key
and the full channel values for the gate view. Both are plain reads of what the worker wrote, and
neither needs the topology.

The one thing that *did* need the compiled graph is `_phase`'s `next`, and decision 2 below removes
that need rather than satisfying it.

**Neither read weakens the decision.** Reading durable state is not executing a graph:
`reviewer_payload()` is a pure function of `ResearchState` that ADR 0013 moved to `graph/state.py`
precisely so the API could call it without importing an agent, the LLM client, or the tool boundary.

### Why `next` is the awkward one

`StateSnapshot.next` is computed by `CompiledStateGraph` from the checkpoint's pending tasks. Getting
it requires either the compiled graph — which drags in the agents, the tool boundary and an
`LLMClient` — or reimplementing LangGraph's task-preparation logic in the API, which would be a second
copy of a decision the framework already owns.

---

## Decision

### 1. The API constructs no graph, no `LLMClient`, and no `OpenAI` client

`_production_graph` is deleted. `RouteDeps.graph` is replaced by a checkpoint reader:

```python
@dataclass(frozen=True)
class RouteDeps:
    config: Config
    engine: Engine
    checkpoints: BaseCheckpointSaver[Any]   # read-only from here; the worker owns setup()
    keys: dict[str, Identity]
```

`routes/api.py` and `app.py` import no agent, no tool, and no LLM client — a property a test asserts
by import rather than by review, in the same way `tests/test_config.py` already pins the config
contract.

### 2. `phase` is derived from `jobs.status`, not from the checkpoint

**This is sound because of ADR 0007 invariant 4**, which is an *if and only if*:
`jobs.status = 'awaiting_approval'` exactly when the checkpoint holds a pending interrupt at
`human_gate`. The row is a faithful projection of the checkpoint, derived by the process that holds
it. The API reads the projection instead of re-deriving it — which **removes** a duplicated
derivation rather than adding one.

```text
jobs.status = 'queued'             ->  "queued"
jobs.status = 'running'            ->  "running"
jobs.status = 'awaiting_approval'  ->  "human_gate"
terminal                           ->  "approved" | "rejected" | "failed"
```

`awaiting_approval` renders as its node name because that is the one node a caller can act on, and it
keeps a node name in the vocabulary gl §12 documents.

**gl §12's vocabulary sentence is corrected**, from *"a graph node name"* to this list. The field is
kept rather than collapsed into `status`, because gl §12 designates it as the one that widens if a UI
ever needs real progress — and decision 6 below names the mechanism that widening would use.

**The trade, stated plainly.** The API can no longer say *which* node a running job is in. That
capability was never actionable: gl §12 already calls `phase` *"a coarse progress label, not a
stream"*, and the three states a caller can do anything about are "not started", "waiting for me", and
"ended". Per-node progress is bought back where it belongs — LangSmith's per-job run tree in Phase 4
(step 24) — and the job's milestones are already in `audit_events`: `plan_produced`,
`subtopic_researched`, `revision`, `gate_opened`, `export_attempted`, `export_result`.

**`GET /jobs/{id}` becomes a single-row read.** It currently does a database read plus a checkpoint
read; after this it does one query.

### 3. Two checkpoint reads remain, both through the checkpointer and neither through a graph

**Corrected 2026-08-17**, which is the only substantive correction this record needed: it originally
said "the one remaining checkpoint read". ADR 0013 added a second one the same week.

Both go through one helper on the API's own pool — `get_tuple(run_config(job_id))` — and differ only
in how much of the snapshot they use.

```python
def checkpoint_state(checkpoints, job_id: str) -> ResearchState | None:
    """The job's durable state, or None if it has never run. No graph, no topology, no LLM."""
    stored = checkpoints.get_tuple(run_config(job_id))
    return None if stored is None else cast(ResearchState, stored.checkpoint["channel_values"])
```

| Read | Used by | Takes |
|---|---|---|
| **The gate-visit key** | `POST /jobs/{id}/approve` | `state["llm_calls_used"]` — one integer. ADR 0007's visit key, and ADR 0006's live budget input |
| **The gate view** | `GET /jobs/{id}/gate` | the whole state, handed to `graph.state.reviewer_payload()` (ADR 0013) |

A job that has never run answers `None`, which the gate-visit key reads as `0` — exactly as
`_gate_visit` answers today, and for the reason its docstring already gives: no gate visit can carry
that key, because reaching the gate costs at least a Planner call. `GET /jobs/{id}/gate` refuses the
same case with `409`, per ADR 0013 decision 6.

**Why the second read changes nothing architecturally.** The boundary this record draws is *the API
does not execute the graph and does not own the LLM client* — not *the API does not read state*. It
has always read state; §13's least-privilege table gives the API task role read/write on its
Postgres schema, and the checkpoint tables are in it. What ADR 0013 was careful about is the part
that would have broken this decision: it moved `reviewer_payload()` out of `graph/build.py`, which
imports all five agents, so the gate view costs a checkpoint read and a pure function rather than an
import of the agent stack.

`graph.build.state_serde()` is reused unchanged — it exists precisely because *"the answer does not
depend on where the bytes go"*, and using it is what keeps the API able to read what the worker wrote.

**The worker owns `setup()`.** `postgres_checkpointer()` calls it today, and the API must not: it is
DDL, and §13's least-privilege table does not give the API a reason to run migrations for tables
LangGraph owns. The API builds a `PostgresSaver` on its own pool without it. The pool stays small for
the same reason the worker's does — the API's reads are one row.

### 4. `LLM_*` and `TAVILY_API_KEY` leave the API process

**The architectural requirement:** the API process starts, serves every route, and passes its health
check with no LLM or Tavily credential present in its environment. That is what makes §13's
least-privilege table describe reality rather than an intention, and it is the decision here.

**The recommended shape**, kept separate from the requirement because it is an implementation detail:
`llm_base_url`, `llm_model`, `llm_api_key` and `tavily_api_key` become `str | None` on `Config`,
`load_config()` stops requiring them, and **the worker validates them once at startup** with the same
loud failure `config.py` already gives a missing variable — narrowing them to `str` before it builds
the `LLMClient`. `scripts/check_model.py` and `scripts/measure_jobs.py` do the same. This extends the
pattern `app.py` already uses for `DATABASE_URL`, which `load_config` reads as optional and the
entrypoint refuses to start without.

**The cost, admitted:** `mypy --strict` will then require narrowing at every consumer of those four
fields. That is a wide diff and it is also the point — it names every place that assumes an LLM. If
the diff turns out to be disproportionate in practice, the alternative is a `require_llm: bool = True`
keyword on `load_config()`, which keeps the types `str` and moves the choice to the two entrypoints;
it is smaller and slightly less honest, and it is the fallback rather than the plan.

### 5. What changes on each route

| Route | Change |
|---|---|
| `POST /jobs` | None from this record. (ADR 0010 changes what it returns and adds the enqueue) |
| `GET /jobs/{id}` | `phase` from decision 2. One query instead of a query plus a checkpoint read. `status`, `revision_count`, `quality_flag` and `report` are untouched |
| `GET /jobs/{id}/gate` | The checkpoint read moves to decision 3's helper. **The payload, its ordering, the authorization and the preconditions are unchanged** — ADR 0013 §10 wrote this route expecting exactly this substitution, and called it two lines |
| `POST /jobs/{id}/approve` | Still reads the checkpoint, for `llm_calls_used` only, through decision 3. `_reconcile_status` and `_resume` are gone (ADR 0011). `refuse_edit()` and `_clean_decision` are untouched |
| `GET /jobs/{id}/report` | None from this record. (ADR 0009 keys it on `exported_at`) |
| `GET /health` | Unchanged: `db`, plus `redis` when step 21 provides it. It has never touched the graph |

**ADR 0006's live-count rule survives exactly.** `refuse_edit()` is still fed the checkpoint's
`llm_calls_used` and never `jobs.llm_calls_used` — decision 3 changes how that value is fetched, not
which value it is. The column is still written only by `finalize` and still reads `0` for the whole
time a job waits at the gate, which is why a check against it would allow every edit silently.

### 6. If per-node `phase` is ever genuinely wanted, the shape is a column

Not built, and written down so the next decision starts from here: the worker writes the completed
node name to a `jobs.phase` column as it goes. It needs no LangGraph internals, no graph in the API,
and one small `UPDATE` per node.

It is **not** built now because no caller has asked for it, ADR 0005 decision 1 keeps one transaction
per node *event* rather than adding a second write per node, and gl §12 already argues the field is
coarse on purpose. This is what gl §12's *"`phase` is the field that widens"* would mean in practice.

---

## Consequences

- **§19's API container table and §13's least-privilege table become true of the code**, not just of
  an intended deployment.
- **The API's blast radius shrinks.** A change to an agent, a prompt, or the tool boundary can no
  longer stop the API process from starting.
- **The API image still carries agent code it never executes** — §20 row 25's one image, two
  entrypoints, and its stated trade-off, unchanged. What changes is that the code is no longer
  *imported* at runtime.
- **CLAUDE.md's Commands section changes:** `uvicorn app:app` needs `DATABASE_URL`, `AUTH_KEYS`,
  `SQS_QUEUE_URL` and `S3_BUCKET`, and no LLM credential.
- **`GET /jobs/{id}` loses per-node resolution.** Nothing consumes it today; Phase 4 tracing is where
  it properly lives.
- **`GET /jobs/{id}/gate` keeps working, unchanged in contract.** ADR 0013's route swaps one state
  read for another and keeps its payload, ordering, authorization and preconditions. A reviewer's
  view of an open gate is not something this record takes away.
- **`tests/test_config.py` moves four names out of its required set**, which is a visible, deliberate
  narrowing of what a process must be given.
- **`postgres_checkpointer()` grows two callers with different needs** — the worker's, which sets up
  the tables, and the API's, which must not.
- **The API reads durable state and executes nothing.** Worth stating as a boundary rather than as a
  count, because the count moved once already: two reads today, and a third would be no more of a
  violation than the first two. What would violate this record is invoking a graph, running a node,
  or constructing an LLM client.

## Alternatives rejected

| Option | Why not |
|---|---|
| **Keep the compiled graph, pass a placeholder API key** | Least privilege would be satisfied on a technicality while the process still constructs an `OpenAI` client from a credential-shaped lie. "The API holds a graph it never runs, wired to a fake key, so one status field can name a node" is not a sentence worth defending in a review |
| **Make `llm` optional on `build_graph()`** | Spreads `None` through `NodeDeps` and into every node signature, to serve one caller that never runs a node |
| **A second, node-less topology for the API to call `get_state` on** | Two topologies to keep in step, and §3's diagram is checkable against `build_graph` today precisely because there is one |
| **Reimplement `next` from the raw checkpoint** | A second copy of LangGraph's task preparation, in our code, tracking a framework version we do not control |
| **Derive `phase` from `ResearchState`'s fields** (`plan is None` → planning, …) | A second copy of the Supervisor's routing rule. `graph/build.py` already names this failure mode: *"two copies of one decision, and the copies would drift"* |
| **Derive `phase` from the last `audit_events` row** | Lossy — `subtopic_researched` cannot distinguish "more research to do" from "ready to synthesise" — and it would re-derive routing to disambiguate |
| **Add `jobs.phase` now** | Decision 6: no caller has asked, and it adds a write per node that ADR 0005's transaction rule deliberately avoids |
| **Leave the API as it is and accept the contradiction** | It is the last thing making §19's container boundary a description of an intention. It is also cheap to fix — this record deletes more code than it adds |

## What a test has to prove before this ships

1. `routes/api.py` and `app.py` import no agent module, no `tools` module, and no `LLMClient` — asserted by import. `graph/state.py` already has this test (ADR 0013), and this extends it to the route layer.
2. `create_application()` builds and **every route answers** with no LLM or Tavily variable set — `GET /jobs/{id}/gate` included, which is the route that would fail first if the payload projection ever reacquired an agent import.
3. `phase` returns the decision-2 value for each of the six statuses, including `human_gate` for `awaiting_approval`.
4. The gate-visit read returns the same number `_gate_visit` returns today, against the same checkpoint — including `0` for a job that has never run.
5. **`GET /jobs/{id}/gate` returns byte-for-byte what it returns today**, read through the checkpointer instead of the graph. ADR 0013's twenty tests are the regression suite for this substitution and none of them should need changing — a test that has to be edited here means the contract moved, which this record does not permit.
6. ADR 0007's four gate cases still hold with the call count read through the checkpointer.
7. ADR 0006's edit refusals still fire on the checkpoint's live count, not on `jobs.llm_calls_used`.
8. The API's checkpointer does not create or migrate the checkpoint tables — the worker's does.
