# ARCHITECTURE — Multi-Agent Competitive Research Assistant

> **Status: Phase 1 is built.** The graph runs locally, end to end, in memory: `config.py`,
> `schemas.py`, `llm_client.py`, `tools/`, the five agents in `agents/`, the reflection node and the
> LangGraph wiring in `graph/`, `scripts/check_model.py`, and the `tests/` suite all exist and pass.
>
> **Phases 2–5 are not built.** No database, no API, no worker, no Redis, no S3, no container, no
> migration, no AWS. §1's "built vs planned" table is the per-capability answer, and every section
> below marks what is implemented where the distinction matters.
>
> This document is the **blueprint the implementation is written against**. If code exists and this
> document disagrees with it, this document is wrong and must be corrected.

**Source of truth.** `CLAUDE.md` states what the project is and what must never break.
`docs/engineering-guidelines.md` states how each part is built and why. This file adds nothing new;
it turns those two into an implementable plan and points at the section that authorises each choice.
Citations look like *(gl §7)* for the guidelines and *(CLAUDE.md)* for the project file.

**`docs/engineering-guidelines.md` is not published in this repository.** It is a gitignored local
working document, so every *(gl §N)* citation below resolves on a local checkout and nowhere else.
That is also why those citations are plain text rather than links.

Where the guidance is silent and a choice had to be made, the choice is marked **[derived]** and
recorded in §20. Where the guidance is genuinely ambiguous, nothing was invented — it is listed in
§22 instead.

**This document has been through one architecture review.** Nine of the ten questions it raised are
decided and applied; §22 lists what is still open. Where a decision made a statement in `CLAUDE.md` or
`docs/engineering-guidelines.md` untrue — the `/health` auth rule, the revision inequality, the
call-budget wording — that statement was corrected in place, because two documents disagreeing is the
failure mode all three files were written to avoid.

---

## 1. System Overview

### What it does

You ask a competitive-research question — *"compare TCS and Infosys on cloud strategy"*. The system
plans the research, searches the web, writes a report, checks every claim against the page it came
from, scores the result, and stops for a human to approve it. Only then is the report exported. Every
claim in an exported report traces back to a URL.

### The problem it solves

A single LLM call answering that question produces confident prose with no sources. You cannot tell
which sentence came from a real page and which the model made up. This system makes that
distinguishable: a claim is stored with the URL and the verbatim quote it came from, and a report
with an uncited claim cannot be exported at all (gl §9).

### The five agents and the three control-flow nodes

**Agents** — five, each with one job (gl §2):

1. **Supervisor** — decides which agent runs next, reading state only.
2. **Planner** — turns the question into 3–5 researchable subtopics.
3. **Researcher** — searches and extracts findings for one subtopic.
4. **Synthesizer** — writes the report from findings only.
5. **Fact-Checker** — verifies each claim against its cited source text.

**Control-flow nodes** — not agents. They have no tools, no persona, and no goal of their own:

- **Reflection** — an LLM-powered evaluation and routing node. It scores the report on five
  dimensions and routes a targeted retry to the specialist that can fix the failing dimension
  (CLAUDE.md, gl §6).
- **Human approval gate** — a LangGraph `interrupt()` that pauses the graph until an authenticated
  reviewer decides (gl §10).
- **Export** — the claim-to-URL gate plus the S3 write (gl §9).
- **Finalize** — the single terminal node that records the outcome (gl §5 names it as a
  Supervisor routing target).

Reflection is **not** a sixth agent. It is LangGraph control flow that happens to use an LLM to
score. Giving it tools, research, or a goal changes the architecture and needs an ADR first
(CLAUDE.md invariant 8).

### Major execution stages

```text
intake → plan → research (per subtopic) → synthesize → fact-check → reflect
       → [targeted retry, bounded] → human gate → export gate → artifact stored
```

### End-to-end request lifecycle

1. `POST /jobs` with a question. The API authenticates the caller, validates the question, writes a
   `jobs` row, enqueues a **pointer** message to SQS, and returns `202` with a `job_id` (gl §12).
2. A worker receives the message, loads or creates the checkpoint for `thread_id = job_id`, and runs
   the LangGraph graph.
3. The Supervisor routes to the Planner, then to the Researcher once per pending subtopic, then to
   the Synthesizer, then to the Fact-Checker.
4. A fixed graph edge carries the Fact-Checker's output to the reflection node. Reflection scores
   five dimensions and either routes one targeted retry or passes the report to the gate.
5. The gate `interrupt()`s. The checkpoint is persisted, the worker is released, `status` becomes
   `awaiting_approval`.
6. `POST /jobs/{id}/approve` records the reviewer's identity and decision. The graph resumes from
   the checkpoint — completed nodes are not re-executed (gl §10).
7. On approve, the export gate checks that every claim has at least one `claim_sources` row. All
   cited → the artifact is written to S3 and an audit event is recorded. Any uncited → **export
   fails, loudly** (gl §9).
8. `GET /jobs/{id}/report` returns a 15-minute presigned S3 URL. Report bytes never stream through
   the API.

### Phase 1 today

**One Python process, no infrastructure.** A job is a call to `build_graph()` and `invoke()`: the
Supervisor routes, the five agents run, the reflection node scores, the graph pauses at the human
gate with `interrupt()`, and a resume decision carries it through the export gate to `finalize`.
State lives in an in-memory checkpointer keyed on `thread_id = job_id`, so it dies with the process —
which is acceptable only because there is no API yet to resume a job from (CLAUDE.md phase plan).

The lifecycle above is the Phase 3+ shape. Steps 1–2 and 5–8 of it — the API, the queue, the
database, and the presigned URL — do not exist yet; the graph in the middle does.

### Eventual AWS production shape (Phase 5)

API Gateway → FastAPI on ECS Fargate → SQS → worker on ECS Fargate → RDS PostgreSQL, ElastiCache
Redis, S3, an OpenAI-compatible LLM endpoint, Tavily behind the tool boundary, LangSmith for agent
traces, CloudWatch for infrastructure health. Detail in §18. No service appears there that is not in
the CLAUDE.md stack table. **None of it is deployed.**

### Built vs planned

| Capability | Phase | Status |
|---|---|---|
| Five agents, reflection node, graph routing, in-memory checkpointer | 1 | **Built** — `agents/`, `graph/` |
| Tool boundary: search, fetch, SSRF, size caps, untrusted wrapper | 1 | **Built** — `tools/` |
| LLM client: structured output, retries, 429 backoff, per-job budget | 1 | **Built** — `llm_client.py` |
| Per-request telemetry: one record per request, carrying the calling node | 1 | **Built** — `llm_client.py`; the sink is injected, and a durable one arrives in Phase 2 (gl §14) |
| Human gate node and export gate, as graph nodes | 1 | **Built** — `graph/build.py`; the endpoint and the audit rows are Phase 2 |
| Test suite: unit, agent contract, graph, integration, injection | 1 | **Built** — `tests/`, zero network calls |
| MCP protocol client or server | — | **Not built, and not scheduled.** The boundary is in-process (§7) |
| Postgres checkpointer, audit tables, `POST /jobs/{id}/approve`, API + API-key auth | 2 | Planned |
| Docker Compose, async worker, SQS/S3 via LocalStack, CI | 3 | Planned |
| Redis: shared rate limiter, caches, URL dedupe | 3 | Planned — the interfaces exist and are wired (§7) |
| LangSmith tracing, eval dataset, eval as a release gate | 4 | Planned |
| AWS deployment, Cognito JWT, CloudWatch alarms | 5 | Planned |

---

## 2. High-Level Architecture

Two containers, one graph, four stores, two external services. Every box below is in the CLAUDE.md
stack table; nothing has been added.

```mermaid
flowchart TD
    CLIENT["Client - submitter or reviewer"] --> APIGW["API Gateway<br/>auth entry and throttling - Phase 5"]
    APIGW --> API["API container - FastAPI<br/>authn, authz, job creation, status, approval"]

    API -->|"write jobs row, read status"| PG[("PostgreSQL<br/>jobs, findings, claims,<br/>claim_sources, audit_events,<br/>checkpoints")]
    API -->|"enqueue pointer message"| SQS["SQS job queue"]
    API -->|"presign report URL"| S3[("S3<br/>exported report artifacts")]

    SQS --> WORKER["Worker container<br/>LangGraph runtime"]
    SQS -->|"3 failed deliveries"| DLQ["Dead-letter queue"]

    WORKER -->|"checkpoint per node, persist facts"| PG
    WORKER -->|"cache, URL dedupe, shared rate limiter"| REDIS[("Redis")]
    WORKER -->|"validated tool calls"| MCP["MCP tool layer<br/>search - Tavily, fetch"]
    WORKER -->|"one OpenAI-compatible client"| LLM["LLM endpoint<br/>LLM_MODEL and LLM_FAST_MODEL"]
    WORKER -->|"write artifact after export gate"| S3
    WORKER -->|"graph, agent, tool, token traces"| LS["LangSmith<br/>what did the agents do"]

    MCP -->|"untrusted content in, never instructions"| WEB["Public web"]

    API -->|"structured JSON logs, metrics"| CW["CloudWatch<br/>is the infrastructure healthy"]
    WORKER -->|"structured JSON logs, metrics"| CW
    SQS -->|"queue depth"| CW
    DLQ -->|"DLQ depth alarm"| CW
```

Reading the diagram:

- **The API never runs the graph.** It validates, persists, enqueues, and reads. §19 explains why.
- **The message is a pointer.** `job_id`, `user_id`, `idempotency_key`, `attempt` — never the state
  (gl §12). State lives in Postgres, so a redelivered message resumes instead of restarting.
- **Only the worker talks to the LLM, MCP, and LangSmith.** That is where the call budget and the
  rate limiter live.
- **Web content enters at exactly one place** — the tool layer (`tools/`, in-process today; §7) — and
  is data from there on (gl §8).
- **The two observability layers do not overlap.** Agent reasoning goes to LangSmith. Infrastructure
  health goes to CloudWatch (gl §14).

---

## 3. LangGraph Architecture

### Node inventory

| Node | Kind | LLM | Tools |
|---|---|---|---|
| `supervisor` | **Agent** | `LLM_FAST_MODEL` | none |
| `planner` | **Agent** | `LLM_MODEL` | none |
| `researcher` | **Agent** | `LLM_MODEL` | `search`, `fetch` |
| `synthesizer` | **Agent** | `LLM_MODEL` | none |
| `fact_checker` | **Agent** | `LLM_MODEL` | `fetch` (re-fetch only) |
| `reflection` | **Control flow** | `LLM_FAST_MODEL` | none |
| `human_gate` | **Control flow** | none | none |
| `export` | **Control flow** | none | none |
| `finalize` | **Control flow** | none | none |

Five agents. Four control-flow nodes. `reflection` scores and routes; it never researches, never
writes, and never holds a tool.

> **Status — built, and this section describes the code.** All nine nodes and every edge below are
> wired in `graph/build.py`, compiled with an **in-memory** checkpointer (`InMemorySaver`) whose
> serializer is told the five Pydantic types that travel in state — a model it was not told about
> comes back as a plain dict and fails later on a resumed job. `run_config(job_id)` sets
> `thread_id = job_id`.
>
> Three nodes route by returning `Command(goto=..., update=...)` — `supervisor`, `reflection`, and
> `human_gate` — and each declares its targets, so the compiled graph draws the shape below and
> reports those edges as conditional. Everything else is a fixed `add_edge`.
>
> `tests/test_graph_build.py` reads the topology back off the compiled graph, including the two
> assertions that are invariants rather than wiring details: nothing reaches `export` except through
> `human_gate`, and the Supervisor cannot name `reflection`. `tests/test_graph_integration.py` then
> runs whole jobs through it with the real agents.
>
> **Phase 2 replaces one line:** `InMemorySaver` becomes the Postgres checkpointer, keeping the same
> serde — what a checkpoint may rebuild does not depend on where it is stored.

### The graph

```mermaid
flowchart TD
    START(["START - worker picks up the job"]) --> SUP{"supervisor<br/>AGENT - routes on state only"}

    SUP -->|"plan is None"| PLAN["planner<br/>AGENT"]
    SUP -->|"a subtopic is pending"| RES["researcher<br/>AGENT - one subtopic"]
    SUP -->|"subtopics resolved, report is None"| SYN["synthesizer<br/>AGENT"]
    SUP -->|"draft has claims with no verdict"| FC["fact_checker<br/>AGENT"]
    SUP -->|"hop, call, or validity guard tripped"| FIN["finalize<br/>CONTROL FLOW - terminal"]

    PLAN --> SUP
    RES --> SUP
    SYN --> SUP
    FC --> REFL{"reflection<br/>CONTROL FLOW<br/>scores 5 dimensions"}

    REFL -->|"completeness or sources weakest - DRAFT INVALIDATED"| RES
    REFL -->|"citation coverage or report quality weakest"| SYN
    REFL -->|"factual consistency weakest"| FC
    REFL -->|"passes, or revision cap reached"| GATE["human_gate<br/>CONTROL FLOW - interrupt"]
    REFL -->|"scoring call failed, report kept - quality_flag unscored"| GATE

    GATE -->|"approve"| EXP["export<br/>CONTROL FLOW - claim-to-URL gate"]
    GATE -->|"edit"| SYN
    GATE -->|"reject, or gate expired"| FIN

    EXP -->|"every claim cited - artifact written"| FIN
    EXP -->|"any uncited claim - export fails"| FIN

    FIN --> DONE(["END"])
```

### Entry point

`START → supervisor`. There is no other entry. A resumed job re-enters at whatever node the
checkpoint stopped at, which for the gate is `human_gate`.

### Normal execution path

`supervisor → planner → supervisor → researcher (×N subtopics, one at a time) → supervisor →
synthesizer → supervisor → fact_checker → reflection → human_gate → export → finalize`.

The Supervisor is visited between agents. That is what makes routing a state decision rather than an
agent handing off to another agent (CLAUDE.md invariant 5).

### Conditional routing

Two conditional edges, and only two:

1. **After `supervisor`** — target comes from `SupervisorDecision.next`, but only after a plain
   Python check against the gl §5 transition table. The model proposes; the code disposes.
2. **After `reflection`** — target comes from the failed-dimension table in gl §6, computed in code
   from the five dimension scores. See §6 of this document.

Every other edge is fixed: `planner → supervisor`, `researcher → supervisor`, `synthesizer →
supervisor`, and `fact_checker → reflection`.

**`fact_checker → reflection` is a fixed edge, not a Supervisor decision.** `reflection` is absent
from the `SupervisorDecision.next` literal on purpose. The Supervisor cannot choose it, so reflection
stays control flow and never becomes a delegation target (gl §5).

### Revision path and counting semantics

**A revision is one automatic report-improvement cycle triggered by reflection after the initial
report** (gl §6). `MAX_REVISIONS` = 2 therefore means at most two automatic improvement cycles, and at
most three report-producing passes in total.

| | |
|---|---|
| `revision_count` counts | Improvement **cycles**, not passes. It starts at **0** |
| The initial report | Pass 1, at `revision_count = 0`. It is not a revision |
| Who increments it | The reflection node, when it decides to start a cycle — whichever specialist it routes to |
| Cap check | `revision_count >= MAX_REVISIONS` → route to the gate instead of starting another cycle |
| A reviewer `edit` | Human-triggered, not automatic. **Not a revision**, does not increment the counter (gl §10) |

Worked through, with `MAX_REVISIONS = 2`:

```text
pass 1  synthesize → fact-check → reflect   (revision_count 0)
        fails  → start cycle, revision_count = 1
pass 2  synthesize → fact-check → reflect   (revision_count 1)
        fails  → start cycle, revision_count = 2
pass 3  synthesize → fact-check → reflect   (revision_count 2)
        fails  → 2 >= 2, cap reached → human gate, quality_flag = "below_threshold"
```

Three passes, two revisions, and the cap is a visible outcome rather than a silent pass (CLAUDE.md
invariant 2).

### Targeted retry path

Only the **lowest failing dimension** is acted on per revision (gl §6). Fixing three things at once
makes it impossible to tell which fix helped.

| Lowest failing dimension | Routes to | What that pass does differently | Draft |
|---|---|---|---|
| Research completeness | `researcher` | Only the subtopics that scored thin — not all five | **Invalidated** |
| Source correctness | `researcher` | Same subtopics, with the failing URLs excluded | **Invalidated** |
| Citation coverage | `synthesizer` | Attach a source to each uncited claim, or delete the claim | Rewritten |
| Factual consistency | `fact_checker` | Re-verify the disputed claims | Unchanged |
| Report quality | `synthesizer` | Rewrite from the same findings — no new research | Rewritten |

#### New findings never bypass synthesis

When reflection routes to the Researcher it makes two state changes before the edge is taken:

1. **`report = None`** — the existing draft is invalidated. It was written without the evidence that
   is about to be gathered, so it is stale by definition.
2. **`subtopic_status[targeted] = "pending"`** — for the specific subtopics that scored thin.

The gl §5 transition table then carries the job the rest of the way with no new rule:

```text
reflection → researcher            (one targeted subtopic)
           → supervisor            (another pending subtopic? → researcher again)
           → supervisor → synthesizer     because report is None
           → supervisor → fact_checker    because the new draft's claims have no verdicts
           → reflection                   fixed edge
```

**Newly retrieved findings cannot reach the human gate without passing through the Synthesizer and
the Fact-Checker.** Without the invalidation, the Supervisor would match *"`report is not None`, no
verdicts this revision → `fact_checker`"* and the new findings would never enter the report — the
completeness retry, the most important targeted retry in the system, would change nothing.

The invalidation also means `subtopic_status` doubles as the retry scope, so no extra state field is
needed to say *which* subtopics to redo. Failing URLs are excluded using the unsupported verdicts
already in state plus the per-job `job:{id}:urls` dedupe set, which already prevents re-fetching a
page this job has seen (§7).

**[derived] "No verdicts this revision" means: some claim in the current `report` has no matching
`Verdict`.** `Verdict` is keyed by `claim_id` (gl §2.5) and a re-synthesized report carries fresh
claim ids, so this is a pure state comparison needing no new field and no revision tag. It also gives
the `edit` path the right behaviour for free: an edited claim is a new claim, so it has no verdict,
so the Supervisor routes to the Fact-Checker — exactly what gl §10 requires.

### Approval path

`human_gate` is a LangGraph `interrupt()` placed **before** `export`. There is no edge from any node
to `export` that skips it (CLAUDE.md invariant 6). Approve resumes into `export`. Edit resumes into
`synthesizer` for one pass and then returns to the gate — an edited claim is a new claim and is
fact-checked like any other (gl §10).

### Rejection path

`reject` goes straight to `finalize` with `status=rejected` and the reason recorded. Nothing is
exported. The state is retained for the retention window (gl §9). A gate with no decision after 7
days is closed the same way with reason `gate_expired` (gl §10, §17).

### Terminal states

Every path ends at `finalize`, which writes the terminal status, sets `completed_at`, emits the audit
event, and hands to `END`.

| Terminal `status` | Meaning |
|---|---|
| `approved` | Reviewer approved **and** the export gate passed. The artifact exists in S3 |
| `rejected` | Reviewer rejected, or the gate expired after 7 days. Nothing exported |
| `failed` | A guard tripped, an agent failed terminally, the export gate blocked the report, or the artifact write was exhausted. `failure_reason` says which |

There is no separate `exported` status — gl §4 fixes the literal at five values. Whether a report is
downloadable is answered by the artifact existing, which is why `GET /jobs/{id}/report` returns `404`
when it does not (gl §12).

**Export failure is explicit inside `failed`, not a sixth status.** The terminal state is
`status=failed` + `failure_reason="export_write_failed"` + the report, claims, `claim_sources`, and
audit rows all preserved. Expressing it this way keeps gl §4's five-value literal and the API contract
unchanged while still being unambiguous to a caller, who branches on the stable `code` in the error
body rather than on the status alone (gl §12).

### Failure states

| Trigger | Where detected | Result |
|---|---|---|
| `hop_count >= MAX_SUPERVISOR_HOPS` | Supervisor guard | `finalize`, `status=failed` |
| `llm_calls_used >= MAX_LLM_CALLS_PER_JOB` | Supervisor guard | `finalize`, `status=failed` |
| `SupervisorDecision.next` outside the transition table | Code check after validation | `finalize`, `status=failed` |
| Empty or invalid plan after one retry | Planner | `finalize`, `status=failed` — never research an unplanned question |
| `Report.sources` empty | Synthesizer schema validation | `finalize`, `status=failed` — never an unsourced report |
| 429 after 3 attempts | LLM client | `finalize`, `status=failed`, reason `rate_limited` |
| 20-minute job limit | Worker | `finalize`, `status=failed`, reason `job_timeout` |
| Rate-limiter token unavailable after 2 retries | LLM client | `finalize`, `status=failed`, reason `rate_limiter_unavailable` |
| Any uncited claim at export | Export gate | `finalize`, `status=failed`, uncited claims listed |
| S3 artifact write exhausted after 2 retries | Export node | `finalize`, `status=failed`, reason `export_write_failed`, **report and audit trail preserved** |

`failure_reason` is never left `None` on a failure (gl §4).

**Two things that are deliberately not failure states:**

- **A failed reflection scoring call when a report exists.** The report is kept, `quality_flag`
  becomes `"unscored"`, the failure is written to `audit_events`, and the job routes to the human
  gate. A complete, fact-checked report is too expensive to discard because the scorer broke — and
  the gate is the backstop that exists for exactly this (§6, gl §6).
- **A subtopic that could not be researched.** It is marked `unresearched`, the job continues, and
  the gap is reported to the reviewer and scored down by reflection (gl §2.3).

---

## 4. Agent Responsibility Boundaries

Five agents. Each is a module with a function, not a class with inheritance. There is no `BaseAgent`
— five different contracts share nothing but a name (gl §20).

Schema field names below that the guidance does not fix are marked *illustrative*; the shapes are
taken from gl §2 and gl §9.

### 4.1 Supervisor — routing

| | |
|---|---|
| **Purpose** | Name the next agent from state. The route is computed in code; the LLM proposal is advisory ([ADR 0001](adr/0001-supervisor-llm-routing-is-advisory.md)) |
| **Input** | `ResearchState` — nothing else |
| **Output** | `SupervisorDecision`, whose `next` is always `allowed_target(state)` |
| **Tools** | None |
| **Model** | `LLM_FAST_MODEL` |
| **Budget** | 1 call per hop, `MAX_SUPERVISOR_HOPS` = 12 |
| **Timeout** | Fast tier — a fixed 30s, 2 retries at 1s and 4s (gl §17) |
| **On failure** | A disagreeing proposal, `invalid_output`, or `llm_call_failed` → logged, routing continues on state. `rate_limited` / `budget_exceeded` → `finalize`, `status=failed`, reason recorded |

```python
class SupervisorDecision(BaseModel):
    next: Literal["planner", "researcher", "synthesizer", "fact_checker", "finalize"]
    reason: str
```

**Responsibilities:** read structured state fields — is there a plan, which subtopics are pending,
does a draft exist, are there verdicts for this revision, how many hops and calls have been spent —
and name the next node.

**Non-responsibilities.** It never reads fetched page content, raw search results, `Finding.evidence`,
or report body text. It never scores quality — that is the reflection node. It never calls a tool.
It never routes to `reflection`; that literal is deliberately absent from its output type (gl §5).

**Budget note:** 12 fast-model calls in the worst case, which is 12 of the 60-call job ceiling. That
is why `hop_count` exists as its own guard rather than relying on the total budget.

### 4.2 Planner — the research plan

| | |
|---|---|
| **Purpose** | Turn the question into 3–5 researchable subtopics with success criteria |
| **Input** | The user's question |
| **Output** | `ResearchPlan` |
| **Tools** | None |
| **Model** | `LLM_MODEL` |
| **Budget** | 2 calls (1 + 1 validation retry) |
| **Timeout** | Main tier — `LLM_MAIN_TIMEOUT_S`, 2 retries at 2s and 8s (gl §17) |
| **On failure** | Empty or invalid plan after the retry → fail the job |

```python
class Subtopic(BaseModel):          # field names illustrative
    id: str
    question: str                   # what the extraction prompt answers against
    search_query: str               # what reaches the search tool, <= 120 chars

class ResearchPlan(BaseModel):
    subtopics: list[Subtopic]       # 3-5, enforced by validation
    success_criteria: list[str]
```

**Responsibilities:** decompose the question, state what a good answer would contain, and write
the search query for each subtopic.

**Why the query is a separate field.** The Researcher used to send `question` to Tavily verbatim.
Step 12 measured what that costs: a 150-character natural-language question returned dictionary
definitions of the word *"what"*, and the subtopic came back `unresearched`. Finding sources and
extracting from them are different jobs and want differently shaped text.

It is written **in the Planner's existing call**, so query reconstruction costs no extra LLM call,
adds no agent, and adds no node — the Planner already knows the subtopic, and a query is part of a
plan. The 120-character cap is enforced by validation rather than truncation, because cutting a
query mid-word makes a different, worse query; an over-long one goes through the same single
bounded retry as any other output that misses its contract.
`success_criteria` is what reflection and the offline eval later score against.

**Non-responsibilities.** It never researches. It never searches. It produces a plan and stops.

**Why an empty plan fails the job:** researching an unplanned question produces a report nobody can
evaluate, because there is no stated success criterion to evaluate it against (gl §2.2).

### 4.3 Researcher — evidence retrieval

| | |
|---|---|
| **Purpose** | Search and extract findings for **exactly one** subtopic |
| **Input** | One `Subtopic` |
| **Output** | `list[Finding]` |
| **Tools** | `search(query)`, `fetch(url)` |
| **Model** | `LLM_MODEL` |
| **Budget** | 3 calls per subtopic, at most 5 subtopics — **45 main-model calls worst case**: 5 subtopics × 3 calls × 3 passes, because a Researcher-routed revision returns subtopics to `pending` (§16) |
| **Timeout** | Main tier — `LLM_MAIN_TIMEOUT_S` per extraction call; **and** 120s per subtopic (`SUBTOPIC_TIMEOUT_S`); `search` 15s, `fetch` 10s (gl §17) |
| **On failure** | Zero findings after retries → subtopic marked `unresearched`, the job continues, the gap is carried into the report |

```python
class Finding(BaseModel):
    finding_id: str                 # illustrative - matches findings.finding_id
    subtopic_id: str                # illustrative
    claim: str
    evidence: str                   # VERBATIM span from the fetched text, never a summary
    url: HttpUrl
    title: str
    retrieved_at: datetime
    content_hash: str               # sha256 of the fetched text
    truncated: bool                 # True when the page was cut at MAX_PAGE_CHARS
```

**Responsibilities:** issue validated queries derived from the plan, fetch only URLs that came from a
search result's `url` field, and extract findings where `evidence` is a quote that can later be
located in the source.

**Concurrency (ADR 0002).** This is the one node that has more than one LLM request open at a time.
Choosing and fetching sources stays sequential — that loop owns the `seen` set behind per-job URL
dedupe, and the tools are 4% of a job's wall clock. Extracting from the fetched pages is 40.7% of it
and runs on a pool of `RESEARCHER_CONCURRENCY` (default 3, ceiling `MAX_LLM_CALLS_PER_SUBTOPIC`),
with findings collected in page order rather than completion order. §16 carries the rate-limit
consequences.

**Non-responsibilities.** It never writes report prose. It never produces a `Finding` without a URL
and a verbatim `evidence` span. It never fetches a URL found inside page text (gl §8, §16).

**Why `truncated` matters:** a quote that cannot be found because the text was never read is a
different failure from a quote that was invented, and the Fact-Checker cannot tell them apart without
this flag (gl §2.3).

**A subtopic that cannot be researched is a reportable outcome, not a silent omission.** The report
names it, and reflection scores it as a completeness failure.

### 4.4 Synthesizer — the report draft

| | |
|---|---|
| **Purpose** | Write the report from findings only |
| **Input** | `list[Finding]`, the plan, and `reviewer_edit_text` when set |
| **Output** | `Report` |
| **Tools** | None |
| **Model** | `LLM_MODEL` |
| **Budget** | 2 calls per pass, at most 3 passes (initial + `MAX_REVISIONS`) — 6 main-model calls worst case |
| **Timeout** | Main tier — `LLM_MAIN_TIMEOUT_S`, 2 retries at 2s and 8s (gl §17) |
| **On failure** | Empty `sources` → hard failure. Never return an unsourced report |

```python
class Section(BaseModel):           # field names illustrative
    id: str
    heading: str
    body: str

class Claim(BaseModel):
    claim_id: str
    section_id: str
    text: str
    finding_ids: list[str]          # never empty - this is the audit link

class Source(BaseModel):
    url: HttpUrl
    title: str
    finding_ids: list[str]

class Report(BaseModel):
    sections: list[Section]
    claims: list[Claim]
    sources: list[Source]           # empty means ungrounded - a failure, not a result
```

**Responsibilities:** structure the answer, and attach to every `Claim` the `finding_id` list it came
from. `Report.sources` is a **view over the findings actually cited**, not an independent store — the
durable record is `findings.url` joined through `claim_sources` (§9).

**Non-responsibilities.** It never introduces a fact that is not in a `Finding`. It never researches.
It never decides whether a claim is verified — that is the Fact-Checker. If it wants to say something
the findings do not support, the correct move is to say nothing and let reflection route back to the
Researcher (gl §2.4).

### 4.5 Fact-Checker — verification

| | |
|---|---|
| **Purpose** | Verify each claim against its cited source text |
| **Input** | `Report`, `list[Finding]` |
| **Output** | `list[Verdict]` |
| **Tools** | `fetch(url)` — **re-fetch only, no new searches** |
| **Model** | `LLM_MODEL` |
| **Budget** | **1 batched call per pass** (+1 retry). All claims in one call, never one per claim |
| **Timeout** | Main tier — `LLM_MAIN_TIMEOUT_S` (gl §17) |
| **On failure** | Source unreachable → `supported=false`, `note="source unreachable"` |

```python
class Verdict(BaseModel):
    claim_id: str
    supported: bool
    quote: str | None               # required when supported is True
    note: str
```

**Responsibilities:** for each claim, either produce a verbatim `quote` from the source that supports
it, or mark it unsupported with a note.

**Non-responsibilities — the strictest contract in the system.** It never infers. "The source implies
this" is not support. "This is common knowledge" is not support. It never searches for a better
source. It never edits the report. A `Verdict` with `supported=true` and `quote=None` is a schema
violation and fails validation (gl §2.5, gl §18).

**Why batching is architectural, not an optimisation:** one call per claim on a 20-claim report is 20
calls against a 60-call job ceiling and a 40 RPM tier. It is the single easiest way to blow the
budget (gl §13).

### 4.6 The boundary rules, in one place

| Agent | Owns | Must never absorb |
|---|---|---|
| Supervisor | Routing | Quality judgement, tool use, reading page text |
| Planner | Decomposition | Research, retrieval, writing |
| Researcher | Retrieval and evidence | Report prose, verification, routing |
| Synthesizer | Writing | Retrieval, verification, unsourced facts |
| Fact-Checker | Verification | New search, editing the report, inference |

---

## 5. ResearchState Design

`ResearchState` is the contract between agents. Agents never call each other; they read and write
state, and the Supervisor decides who runs next (CLAUDE.md invariant 5).

### The state, as gl §4 defines it

| Field | Type | Owner (writes) | Accumulated or current | Why it belongs in state |
|---|---|---|---|---|
| `job_id` | `str` | API at creation | Current (immutable) | Ties state, traces, audit rows, and the checkpointer thread together. `thread_id = job_id` |
| `user_id` | `str` | API at creation | Current (immutable) | Single tenant today; present so tenant scoping is additive later |
| `question` | `str` | API at creation | Current (immutable) | The original request, **never rewritten** |
| `plan` | `ResearchPlan \| None` | Planner | Current (overwritten) | `None` is the Supervisor's first routing test |
| `subtopic_status` | `dict[str, Literal["pending","done","unresearched"]]` | Researcher | Current (per-key overwrite) | Drives routing **without inspecting findings** — keeps page text out of the Supervisor |
| `findings` | `Annotated[list[Finding], operator.add]` | Researcher | **Append-only** | A step that times out and retries would otherwise drop the first attempt's findings |
| `report` | `Report \| None` | Synthesizer **writes**, reflection node **invalidates** | Current (overwritten) | The current draft. One draft exists at a time. Set to `None` when reflection routes back to the Researcher, so new findings cannot bypass synthesis |
| `verdicts` | `Annotated[list[Verdict], operator.add]` | Fact-Checker | **Append-only** | Accumulated across passes, so "did revision 2 fix it?" is answerable |
| `reflection_scores` | `list[ReflectionScore]` | Reflection node | **Append-only** (one per pass) | The history is what makes "did it improve?" answerable |
| `failed_dimensions` | `list[str]` | Reflection node | **Current** (overwritten each pass) | The dimensions failing *right now*. The gate payload and the targeted retry both read it without recomputing from scores and weights |
| `revision_count` | `int` | Reflection node | Current (counter) | Improvement cycles, not passes. Compared against `MAX_REVISIONS` with `>=` |
| `quality_flag` | `str \| None` | Reflection node | Current (overwritten) | `None`, `"below_threshold"`, or `"unscored"`. Carries the cap and scoring-failure outcomes to the gate, the API, and `jobs.quality_flag` |
| `hop_count` | `int` | Supervisor | Current (counter) | Compared against `MAX_SUPERVISOR_HOPS` |
| `llm_calls_used` | `int` | Every LLM caller | Current (counter) | Compared against `MAX_LLM_CALLS_PER_JOB` |
| `reviewer_edit_text` | `str \| None` | Approval endpoint sets, Synthesizer clears | Current (**set once, consumed once**) | The `edit` decision's text. It must reach the Synthesizer, and it must not be re-applied on a later pass |
| `status` | `Literal["running","awaiting_approval","approved","rejected","failed"]` | Gate, finalize | Current | The externally visible job state |
| `failure_reason` | `str \| None` | Whichever guard trips | Current | Set whenever `status=failed`; never left `None` on a failure |

**Reducers.** Exactly two fields use `operator.add`: `findings` and `verdicts`. Everything else is
last-write-wins. That is the whole reducer design — no custom merge functions.

**`reflection_scores` is append-only in practice** but gl §4 types it as a plain `list`. Whether it
needs an `operator.add` reducer depends on whether reflection can be retried mid-node; it is written
once per pass by a single node, so a plain list is correct as specified.

### The three fields added to gl §4

These were flagged as gaps at architecture review and are now accepted. gl §4's table has been
updated to match. **Nothing else was added** — everything else the design needs is derivable from
state that already exists.

#### `quality_flag: str | None`

| | |
|---|---|
| **Owner** | The reflection node. Nothing else writes it |
| **Lifecycle** | `None` while the job runs → set on the last reflection pass → read at the gate and by `GET /jobs/{id}` → persisted to `jobs.quality_flag` by `finalize` |
| **Values** | `None` (scored and passed) · `"below_threshold"` (revision cap hit with a failing score) · `"unscored"` (the scoring call failed and the report was kept) |
| **Why in state** | It has to survive the interrupt and travel from the reflection node to the gate payload and the API. Recomputing it from `reflection_scores` would not work for `"unscored"`, where there is no score to recompute from |

**`"unscored"` is not `None`.** A `None` flag means the rubric ran and the report passed. `"unscored"`
means the rubric never ran. The two must never be collapsed — see §6.

#### `reviewer_edit_text: str | None`

| | |
|---|---|
| **Owner** | Written by the approval endpoint on an `edit` decision; **cleared by the Synthesizer** after the pass that consumes it |
| **Lifecycle** | `None` → set at the gate → read by the next Synthesizer pass → set back to `None` by that same pass |
| **Why in state** | gl §2.4 lists reviewer edits as Synthesizer input, and the edit is written at the API while the Synthesizer runs in the worker minutes later. State is the only channel between them (CLAUDE.md invariant 5) |
| **Why cleared** | Set-once-consume-once. Left in place, a later pass would silently re-apply an edit the reviewer made to an older draft |

The reviewer's text is stored verbatim and is **also** an `audit_events` row, so "what did the
reviewer actually ask for?" survives the clear.

#### `failed_dimensions: list[str]`

| | |
|---|---|
| **Owner** | The reflection node, overwritten on every pass |
| **Lifecycle** | `[]` → recomputed each reflection pass → read by the targeted-retry routing and by the gate payload |
| **Current, not accumulated** | It describes the report as it stands. The per-pass history is recoverable from `reflection_scores`, so keeping a second history here would be duplication |
| **Why in state** | The gate payload and the API show the reviewer *which* dimensions failed. Deriving it on read would mean re-implementing the weights and the threshold in two places |

**`failed_dimensions == []` with `quality_flag == "unscored"` means unknown, not clean.** Any
consumer that reads the list must read the flag first.

### What was deliberately *not* added

The review also flagged "which subtopics or URLs a targeted retry should act on". **No field was
added, because the state already carries it:**

- **Which subtopics** — reflection sets `subtopic_status[targeted] = "pending"`. The pending set *is*
  the retry scope, and the Supervisor's existing transition table already routes on it.
- **Which URLs to exclude** — the unsupported `verdicts` already in state identify the failing
  sources, and the per-job `job:{id}:urls` dedupe set already prevents re-fetching a page this job
  has seen (§7).

### Where the other requested concepts live

| Concept | Where it lives | Note |
|---|---|---|
| `draft_report` | `report` | Same thing, gl §4's name |
| `claims`, `sources` | inside `report` | Not top-level state. A claim only exists as part of a draft |
| `reflection_score` | `reflection_scores[-1]` | The list is kept; the latest is the current score |
| `approval_state` | `status` | `awaiting_approval` / `approved` / `rejected` are three of its five values |
| `errors` | `failure_reason` + `audit_events` | One reason on state; the full history in Postgres |
| `timestamps` | `jobs.created_at`, `jobs.completed_at`, `audit_events.created_at`, `findings.retrieved_at` | Timestamps are durable facts, not routing inputs. Keeping them out of state keeps the checkpoint small |
| Approver identity | `audit_events.actor` | The gate decision is an audit fact, not a routing input (gl §9) |
| `idempotency_key` | `jobs.idempotency_key` + the SQS message | Never in `ResearchState`. It governs job *creation*, and the graph never reads it (§11) |

### Where the requested concepts actually live

The brief listed some field names that gl §4 does not define. They exist, but not as separate state
fields — mapping them here rather than adding state:

| Concept | Where it lives | Note |
|---|---|---|
| `draft_report` | `report` | Same thing, gl §4's name |
| `claims`, `sources` | inside `report` | Not top-level state. A claim only exists as part of a draft |
| `reflection_score` | `reflection_scores[-1]` | The list is kept; the latest is the current score |
| `failed_dimensions` | inside `ReflectionScore` | Per-revision, so it belongs with the score that produced it |
| `approval_state` | `status` | `awaiting_approval` / `approved` / `rejected` are three of its five values |
| `errors` | `failure_reason` + `audit_events` | One reason on state; the full history in Postgres |
| `timestamps` | `jobs.created_at`, `jobs.completed_at`, `audit_events.created_at`, `findings.retrieved_at` | Timestamps are durable facts, not routing inputs. Keeping them out of state keeps the checkpoint small |
| Approver identity | `audit_events.actor` | The gate decision is an audit fact, not a routing input (gl §9) |

**Three fields the guidance requires elsewhere but gl §4's table does not list** — flagged, not added
(see §22):

- `quality_flag` — gl §6 sets it at the revision cap, `jobs` stores it, and `GET /jobs/{id}` returns
  it.
- Reviewer edit text — gl §2.4 lists "any reviewer edits" as Synthesizer input, and gl §10's `edit`
  decision writes "the reviewer's text into state".
- Which subtopics or URLs a targeted retry should act on — gl §6 says "only the specific subtopics
  scored thin" and "with the failing URLs excluded", which the Researcher must read from somewhere.

### Persistence of state

`thread_id = job_id`. One job, one thread, one checkpoint history. Checkpoints are written **per
node**, which is what makes both the human gate and a worker crash survivable (gl §4, gl §12).

Phase 1 uses the in-memory checkpointer, because there is no gate yet. Phase 2 switches to the
Postgres checkpointer — not for scale, but because the gate can hold a job for days and in-memory
state dies with the worker, which would mean re-billing every LLM call for work already done (gl §4).

LangGraph's Postgres checkpointer manages its own tables through `setup()`. Alembic does not touch
them (gl §19).

---

## 6. Supervisor and Reflection Routing

Two routers, deliberately different. The Supervisor decides **what runs next in the normal path**
using state only. The reflection node decides **what to retry** using the report it just scored.

### 6.1 Supervisor routing

The Supervisor returns a structured decision, never free text. The transition table is then enforced
in plain Python:

| Current state | Next | Condition |
|---|---|---|
| `plan is None` | `planner` | Always the first hop |
| Any `subtopic_status == "pending"` | `researcher` | Picks the first pending subtopic |
| All subtopics resolved, `report is None` | `synthesizer` | Findings are ready |
| `report is not None`, some claim has no `Verdict` | `fact_checker` | A draft exists but is unchecked |
| Every claim in `report` has a `Verdict` | *(fixed graph edge)* | Falls through to the reflection node |
| `hop_count >= MAX_SUPERVISOR_HOPS` | `finalize` | Loop guard |
| `llm_calls_used >= MAX_LLM_CALLS_PER_JOB` | `finalize` | Budget guard, `status=failed` |

**State-based routing.** Every condition above reads a structured field: a `None` check, a dict of
statuses, a set comparison over claim ids, three integers. None of them reads text a third party
wrote. This is the single most important boundary in the system (gl §2.1, gl §8).

**"Unchecked" is defined by claim ids, not by a revision tag** *(derived — §3 explains why)*. A
`Verdict` carries a `claim_id` (gl §2.5); a re-synthesized or edited report carries fresh claim ids;
so "some claim has no verdict" is exact, needs no extra field, and correctly re-verifies an edited
claim.

> **"Fresh claim ids" is now enforced, and was not.** It was an assumption about model behaviour
> stated as an invariant. The model numbers its claims from `c1` on every pass, so a redraft with
> *fewer* claims than the last produces ids that are a subset of the already-verdicted set — every
> claim looks verified, `allowed_target()` returns `None`, and the job ends `no_valid_transition`
> having done all its work. A real step-12 job did exactly that: 16 claims, then 3. An earlier run
> that redrafted 12 → 16 survived only because ids `c13`–`c16` happened to be new.
>
> The Synthesizer now mints `claim_id` in Python after validating the model's output, the same way
> the Researcher mints `finding_id`. Nothing inside a report points at a claim id, so `section_id`,
> `finding_ids`, and the derived `sources` are untouched, and the audit trail is unchanged. The
> reducer on `verdicts` is unchanged and no revision tag was added — the property the router needs
> is now true by construction rather than by hope.

**The table is unchanged by the reflection fix.** Invalidating the draft (`report = None`) and
returning subtopics to `pending` makes the *existing* rows fire in the right order. That was the point
of choosing invalidation over adding a sixth row: the router stays as simple as it was.

**"Resolved" means not `pending`.** `done` and `unresearched` are both terminal — `unresearched` means
the subtopic was attempted and produced nothing, so it is finished and only reflection can return it
to `pending` (§6.2). Always true of the code; not said out loud until ADR 0001, and a real job routed
on the other reading.

**Invalid routing behaviour.** *(changed by [ADR 0001](adr/0001-supervisor-llm-routing-is-advisory.md)
at step 12.)* `allowed_target(state)` is the sole authority for the route. A proposal outside the
table is logged and ignored, and the job continues on the state's own route; so are `invalid_output`
after its one retry, and `llm_call_failed` after its transport retries. `rate_limited` and
`budget_exceeded` remain job-fatal, unchanged.

The previous rule routed a disagreeing proposal to `finalize` with `failure_reason="invalid_route"`.
It killed the first two real jobs — two different fast models, two different states, 2 of 10 calls
disagreeing — while the route returned was always `allowed_target(state)` regardless. `next` is a
`Literal` of the five node names, so a wrong target is schema-valid and the validation retry never
fires: **wrong-but-valid output is not caught by Pydantic and cannot be fixed by strengthening it.**

**`MAX_SUPERVISOR_HOPS` = 12.** It catches routing oscillation — A → B → A → B — which the call
budget alone would catch too slowly and the revision cap would not catch at all.

> **Raised from 12 to 24 on 2026-08-13.** Only a `supervisor` visit costs a hop — `fact_checker →
> reflection` and reflection's own `Command(goto=…)` bypass it — so hops are `N + 3` for the first
> pass and `k + 1` per Researcher-routed revision. At the documented maximums (`N` = 5,
> `MAX_REVISIONS` = 2, `k` = 5) that is `8 + 6 + 6 = ` **20**, and the formula reproduces three real
> jobs exactly. 12 stopped a job doing ordinary forward progress.
>
> The default sits **above** its own ceiling on purpose: a guard set at the maximum fires on the first
> legitimate addition, and the reviewer `edit` path costs +1 hop per edit and is not bounded yet. The
> extra 4 is **temporary margin for that path** and should be removed once Phase 2 bounds it. The
> guard's purpose, position, and `>=` semantics are unchanged. Development overrides to 30 (gl §5).

Three guards exist because they fail for different reasons (gl §5):

| Guard | Catches | Limit |
|---|---|---|
| `hop_count` | Routing oscillation | 12 |
| `revision_count` | A reflection loop that never converges | `MAX_REVISIONS` = 2 |
| `llm_calls_used` | Everything else, including one agent burning calls internally | 60 |

Each guard sets `failure_reason` and routes to `finalize`. None fails silently.

### 6.2 Reflection routing

**Reflection is an LLM-powered evaluation and routing node, not an agent.** It has no tools, no
persona, and no goal of its own. It is reached by a fixed edge from `fact_checker` and cannot be
selected by the Supervisor.

#### The rubric

Scored 1–5 on the same five dimensions the offline evaluation uses (gl §15) — one vocabulary, used
inline as a gate and offline as a measurement:

| Dimension | Weight | What a 5 looks like |
|---|---|---|
| Research completeness | 0.30 | Every planned subtopic has findings from more than one source |
| Source correctness | 0.20 | Sources are reachable, relevant, and not obviously low quality |
| Citation coverage | 0.20 | Every claim carries at least one source |
| Factual consistency | 0.20 | No claim contradicts its source or another claim |
| Report quality | 0.10 | Structured, readable, answers the question asked |

#### Score calculation and threshold

```text
weighted = 0.30*completeness + 0.20*source_correctness + 0.20*citation_coverage
         + 0.20*factual_consistency + 0.10*report_quality

pass  =  weighted >= REFLECTION_PASS_THRESHOLD (3.5)  AND  citation_coverage == 5
```

Citation coverage is a **hard gate**, not a weighted contribution, because gl §9 makes it an export
invariant. A report can be beautifully written and still not exportable.

**[derived] The model scores; the code decides.** The LLM returns five integers and a short
rationale. The weighted sum, the threshold comparison, and the route selection are plain Python. This
mirrors "the model proposes; the code disposes" from gl §5, and it shrinks what an injected page can
influence to five integers rather than a routing string.

```python
class ReflectionScore(BaseModel):       # emitted by a control-flow node, never a SupervisorDecision
    research_completeness: int          # 1-5
    source_correctness: int
    citation_coverage: int
    factual_consistency: int
    report_quality: int
    rationale: str
    # computed in code, not by the model:
    weighted_score: float
    failed_dimensions: list[str]
    route: Literal["researcher", "synthesizer", "fact_checker", "human_gate"]
```

A `ReflectionScore` missing a dimension, or naming a route outside the table, is rejected (gl §18).

#### Failed dimensions and targeted routing

`failed_dimensions` = every dimension scoring below the pass bar. It is written to state (§5) so the
gate payload and the retry both read one value. **Only the lowest failing dimension is routed on, one
per revision** — fixing three things at once makes it impossible to tell which fix helped (gl §6). The
routing table, and what each route does to the draft, are in §3.

Reflection **routes**; it does not delegate. It never instructs an agent, never calls one directly,
and never carries out the fix itself. It writes a score, a route, and — on a Researcher route — the
draft invalidation into state. The graph edge does the rest.

#### The state changes reflection is allowed to make

This is the complete list. A control-flow node with a longer list would be an agent:

| Route | Writes |
|---|---|
| any | `reflection_scores` (append), `failed_dimensions`, `revision_count` when it starts a cycle |
| `researcher` | **`report = None`**, `subtopic_status[targeted] = "pending"` |
| `synthesizer` | nothing extra — the Synthesizer overwrites the draft itself |
| `fact_checker` | nothing extra |
| `human_gate` | `quality_flag` when the cap was hit or scoring failed |

It never writes `findings`, `verdicts`, claim text, or `status`.

#### `MAX_REVISIONS` and the cap

`MAX_REVISIONS` = 2 — two automatic improvement cycles, at most 3 passes (§3 works the counting
through). When `revision_count >= MAX_REVISIONS` and the score is still failing (gl §6):

- The job continues with `quality_flag = "below_threshold"`.
- The score breakdown is attached to the report and shown at the human gate.
- **The reviewer decides. Reflection does not.**
- Hitting the cap is a visible outcome carried in the response, never a silent pass (CLAUDE.md
  invariant 2).

**One exception that is not reviewer-overridable:** if citation coverage is still failing at the cap,
export is blocked regardless of what the reviewer says. That is the gl §9 invariant, enforced at the
export node.

#### When the scoring call itself fails

The rubric is a quality gate, not the report. If the reflection call times out or returns an invalid
`ReflectionScore` after its bounded retry (gl §17: fast model, 2 retries at 1s and 4s), and **a report
exists**:

| | |
|---|---|
| The report | **Kept.** Not discarded, not regenerated. It is complete and fact-checked; only the score is missing |
| `quality_flag` | `"unscored"` |
| `revision_count` | Unchanged. A failed scorer does not consume an improvement cycle |
| `failed_dimensions` | Left empty — **which means unknown, not clean** |
| Audit trail | An `audit_events` row: `action="reflection_failed"`, `detail` carrying the error and the pass number |
| Route | `human_gate` |

> **`unscored` does not mean passed.** It means the automated quality gate did not run for this job,
> so the reviewer is the only judgement in the loop. The gate payload shows it with the same
> prominence as `below_threshold`, next to the unsupported claims and unresearched subtopics (§12).

Two things are still true for an unscored report, which is what makes keeping it safe:

1. **The export gate still runs on approval.** It is a database check over `claim_sources`, not a
   score, so an unscored report still cannot carry an uncited claim (gl §9).
2. **A human still reads it before anything is exported** (CLAUDE.md invariant 6).

If **no** report exists when reflection fails, there is nothing to gate and nothing to preserve: the
node fails per gl §17 and the job fails with the reason recorded.

#### Calibration

An uncalibrated LLM judge is decoration. Before the rubric is trusted as a gate, 20 reports are
scored by hand and compared. A dimension where judge and human disagree by more than one point gets
its prompt fixed before the rubric gates anything. Re-calibrate when the model changes (gl §6).

---

## 7. Tool Boundary

Two tools: `search` (Tavily) and `fetch`. The boundary earns its place by being **one place** for
argument validation, timeouts, rate limiting, caching, and the injection boundary — not because it is
modern (gl §7).

> **Implementation status: the boundary is built; the protocol is not.** `tools/` is that one place
> today — `search.py`, `fetch.py`, `validation.py`, `untrusted.py`, `contracts.py` — and it is
> in-process. **There is no MCP client, no MCP server, and no protocol hop**, and none is scheduled.
> Everything below this line describes `tools/` as it exists, except the Redis cache and the URL
> dedupe set, whose interfaces are defined and wired but whose implementation is Phase 3.
>
> `fetch` in particular is deliberately ours rather than delegated: gl §16 defines it as a request
> made from inside our network, and the SSRF check, the byte cap, and the redirect re-validation are
> only meaningful if we are the process making the request. Where a diagram below or in §18 says "MCP
> tool layer", read it as the planned production shape of this same boundary.

```mermaid
flowchart TD
    RES["researcher - AGENT"] -->|"query from the plan"| VAL{"argument validation<br/>length cap, control chars,<br/>SSRF check on URLs"}
    FC["fact_checker - AGENT"] -->|"url from an existing Finding"| VAL
    VAL -->|"rejected"| ERRV["recorded in state<br/>never silently dropped"]
    VAL -->|"accepted"| CACHE{"Redis cache<br/>by argument hash"}
    CACHE -->|"hit - logged"| NORM["normalize to SearchResult<br/>cap at MAX_PAGE_CHARS"]
    CACHE -->|"miss - logged"| CALL["MCP tool call<br/>search 15s / fetch 10s"]
    CALL -->|"ok"| NORM
    CALL -->|"timeout, 4xx, 5xx,<br/>too large, wrong type, robots-blocked"| UNREACH["source marked unreachable<br/>a legitimate finding-level outcome"]
    NORM --> DEDUPE{"URL seen for this job?<br/>Redis set job:id:urls"}
    DEDUPE -->|"yes"| SKIP["skip - wasted budget avoided"]
    DEDUPE -->|"no"| DATA["delimited, labelled UNTRUSTED,<br/>truncated - then into the prompt"]
```

### What the Researcher may send

| Tool | Allowed argument | Where the value may come from |
|---|---|---|
| `search(query)` | A text query | The plan, which comes from the user's question |
| `fetch(url)` | An absolute `http`/`https` URL | A search result's `url` field, or a `Finding.url` already in state |

**Never allowed as an argument source: text inside a fetched page.** A URL found in page body text is
not fetchable (gl §8, gl §16).

### Argument validation

Before any request leaves the process:

- Query is length-capped and stripped of control characters.
- URL scheme must be `http` or `https`.
- The hostname is resolved and the **resolved IP** is checked — not the hostname. Private ranges are
  rejected: `10/8`, `172.16/12`, `192.168/16`, `127/8`, `169.254/16`, and the IPv6 equivalents.
- The check re-runs **after every redirect**. A public URL redirecting to `169.254.169.254` is the
  standard bypass (gl §16).

Tool arguments are structured outputs too, and get validated like any other (gl §3).

### Timeouts, retries, and giving up

| Operation | Timeout | Retries | Backoff | On exhaustion |
|---|---|---|---|---|
| `search` | 15s | 2 | 1s, 4s | That query yields no findings for the subtopic |
| `fetch` | 10s | 1 | 2s | Source marked `unreachable` |

A give-up is recorded in state, never swallowed (gl §7, gl §17).

### URL deduplication

A Redis set keyed `job:{id}:urls`, TTL 6h. The same page fetched twice is wasted budget and
double-counted evidence (gl §7, gl §11).

### Caching

`cache:search:{hash}` and `cache:fetch:{hash}`, keyed by argument hash, TTL 24h. A revision that
re-researches a subtopic usually re-issues the same query. **Every hit and miss is logged**, because
caching is one of the four cost rules and a cost control nobody measures is a guess. Hit rate is a
tracked signal with a target of ≥ 30% once revisions are running (gl §13, gl §14).

### Result normalization

Everything the tool layer returns is normalized to one shape before an agent sees it:

```python
class SearchResult(BaseModel):
    title: str
    url: HttpUrl
    content: str                        # cleaned page text, not a snippet
    published_at: datetime | None = None
    truncated: bool = False             # content was cut at MAX_PAGE_CHARS
```

Tavily is chosen over a raw search API for one reason: it returns **cleaned content and the URL
together**. The audit trail needs both, and stitching a separate scrape onto a URL-only result is
where content and citation quietly stop matching (gl §7).

**`truncated` closes the gap between two rules that meet here.** The cap is applied at normalization
— the diagram above says so — and gl §2.3 requires the `Finding` built from this result to carry
`truncated=true` when the text was cut. The flag is how the tool layer tells the Researcher which
happened; without it, a quote missing because the page was cut is indistinguishable from one that was
invented. It is defaulted, so an untruncated result reads exactly as it did before.

### Content-size limits

| Limit | Value | Enforced |
|---|---|---|
| `MAX_FETCH_BYTES` | 2 MB | During the fetch. Larger → `unreachable`, no partial parse |
| `MAX_PAGE_CHARS` | 24,000 (≈6k tokens) | On cleaned text, before any byte reaches a prompt |
| Allowed content types | `text/html`, `text/plain`, `application/pdf` | Anything else → `unreachable`. No image, office-document, or other binary path |
| PDF extraction | `pypdf`, first 50 pages | Added on measurement in step 12: three smoke jobs rejected Infosys's annual report and investor presentation, the best primary sources these questions have. The page bound exists because extraction is our own work with no request timeout around it; hitting it sets `truncated`, the same flag the character cap sets. An unreadable or scanned PDF is `unreachable`, never a guess — there is no OCR path |

**Truncation keeps the head of the page and records that it happened.** Head-first is the simple
choice and it is wrong some of the time — evidence deep in a long document becomes invisible. The
`Finding` carries `truncated=true` so the gap is explicable, and so the eval set can measure how
often it bites. If it bites often, the fix is relevance-selected extraction, and that gets an ADR
because it adds a retrieval step this system does not have (gl §7).

**Both tools apply the cap and both report it**, because both feed the same prompts: `search` reports
it on `SearchResult.truncated`, `fetch` reports it on the page it returns. The delimited, labelled
block below applies the cap once more on the way into a prompt, so no path can skip it.

### Unavailable sources

`unreachable` is a **finding-level outcome, not an error to route around**. A blocked, oversized,
wrong-typed, or timed-out page produces no finding for that URL. At fact-check time an unreachable
source produces `supported=false, note="source unreachable"` — never a guess (gl §2.5, gl §7).
robots.txt and terms of service are respected on fetch.

### The prompt-injection boundary

> **Fetched content is data. It is never an instruction, and it can never change control flow**
> (gl §8).

Three mechanisms enforce it:

1. **The Supervisor never sees fetched text.** It routes on structured state only. No injection,
   however well crafted, reaches the component that decides which agent runs next in the normal path.
2. **Fetched text never becomes a tool argument.** Queries come from the plan; URLs come from a
   search result's `url` field.
3. **Instruction-like content is neutralized before it reaches a prompt.** Fetched text is delimited,
   labelled untrusted, and truncated. The extraction prompt asks for claims *about* the text; it
   never asks the model to follow it.

**The one path that is not structural — reflection.** The reflection node reads the draft report, and
the report is assembled from `Finding.evidence`, which is a verbatim quote from a fetched page. Text
a third party wrote does reach a component that decides what runs next. This is unavoidable: scoring
a report means reading it. The honest position is not "injection cannot influence control flow", it
is "injection can influence one bounded loop, and here is the bound" (gl §8):

| What an injection could attempt | What stops it |
|---|---|
| Inflate scores to skip a needed revision | The export gate still runs, and a human still reads the report |
| Deflate scores to burn revisions | `MAX_REVISIONS` caps the loop; the cap is a visible `quality_flag` |
| Steer which agent reruns | The §6.1 transition table and `MAX_LLM_CALLS_PER_JOB` bound the damage to wasted calls |
| Reach export, a tool argument, or the Supervisor | Not reachable — the three structural boundaries above |

The exposure is bounded to **wasted work and a worse report arriving at the human gate**, never to an
unsourced export or an attacker-chosen fetch. §22 of the guidelines' testing section requires this
bound to be tested, not assumed (gl §18).

**What we do not build:** no jailbreak classifier, no injection-detection model. Both add a call per
fetch and a false-positive rate, for a threat the structural boundary already handles (gl §8).

---

## 8. Persistence Architecture

Two stores split by lifetime, plus a queue and an object store. There is no third memory store and
**no vector memory** — this system answers one question per job from freshly retrieved sources, and
there is no cross-session recall requirement. If one appears, it gets an ADR (gl §11).

### PostgreSQL — durable facts and the audit trail

Holds `jobs`, `findings`, `claims`, `claim_sources`, `audit_events`, and LangGraph's own checkpoint
tables. Anything that must survive the job lives here. It is also what makes the human gate
resumable: the gate can hold a job for days, and in-memory state dies with the worker (gl §4, gl §11).

Retention (gl §9, `RETENTION_DAYS` = 365):

| Data | Retention |
|---|---|
| `jobs`, `claims`, `claim_sources`, `audit_events` | 12 months |
| `findings.evidence` | 12 months — a claim is not explicable without the quote it was made from |
| Checkpoints for closed jobs | 30 days after close |

One sweep job enforces retention and gate expiry on the same schedule, and **every deletion is itself
an `audit_events` row**.

### Redis — short-lived operational state

| Key | Contents | TTL |
|---|---|---|
| `job:{id}:scratch` | Working state for the running job | 6h |
| `job:{id}:urls` | URLs already fetched, for dedupe | 6h |
| `ratelimit:llm` | **Shared** token bucket across all workers | rolling 60s |
| `cache:search:{hash}` | Search results by argument hash | 24h |
| `cache:fetch:{hash}` | Fetched page text by URL hash | 24h |

The rate limiter is **shared and global, not per worker**. Two workers each politely limiting
themselves to 40 requests per minute produce 80 (gl §11).

Nothing in Redis is a source of truth. Everything here can be lost without losing a job's facts. But
the keys do not all matter equally, so **Redis being unavailable is handled two different ways**:

| Redis responsibility | Policy | What happens | Why |
|---|---|---|---|
| `cache:search:*`, `cache:fetch:*` | **Fail open** | Treat as a miss. Do the search or fetch again, log the miss | A cache miss costs one call. Refusing to work because a cache is down trades a real outage for an optimisation |
| `job:{id}:urls` dedupe | **Fail open** | Allow the fetch. The same page may be fetched twice | The cost is a wasted call and a duplicate finding, both bounded by `MAX_LLM_CALLS_PER_JOB` |
| `ratelimit:llm` token bucket | **FAIL CLOSED** | No token, no LLM call. Bounded retry, then the node fails with reason `rate_limiter_unavailable` | **A limiter that fails open is not a limiter.** With the bucket gone, every worker would discover the tier's real ceiling simultaneously, by hitting 429s together |

The bound on the closed path is gl §17's new row: 5s timeout, 2 retries at 2s and 8s, then fail the
node. Those values are borrowed from existing rows in that table — 5s from the database-query row, the
2s/8s backoff from the main LLM-call row — rather than invented, so §17 stays the single place the
numbers live.

**Fail-closed here is not a hidden stall.** The give-up is loud: `status=failed` with a named reason,
an audit row, and `/health` already reporting `redis` unhealthy, which takes the task out of the
target group so new jobs stop arriving in the first place (gl §12).

### SQS — asynchronous execution

One job queue and one dead-letter queue. The message is a pointer, never state. Detail in §11.

### S3 — exported artifacts

Exported reports only, written by the export node after the gate passes, read by the API as a
15-minute presigned URL. Report bytes never stream through the API, so a 20-minute job's output never
occupies a worker (gl §12).

#### Phase 2, before S3 exists

The export **gate** ships in Phase 2; S3 does not arrive until Phase 3. So in Phase 2 the export node
runs the claim-to-URL check and, when it passes, writes the approved report body to
`jobs.report_json` and stamps `jobs.exported_at`. Retrieval uses the route that already returns a
report body — `GET /jobs/{id}`, whose contract has always included `report?` (§10). The artifact route
`GET /jobs/{id}/report` returns `404 not exported` until Phase 3 wires S3.

**No stand-in for S3 is built.** No storage abstraction, no local-filesystem artifact writer, no
interface with one implementation. Phase 3 adds the `PutObject` and the presigned URL to the same
export node; `report_json` stays as the durable body the artifact is rendered from. The bounded-retry
and `export_write_failed` behaviour above describes the S3 write and therefore begins in Phase 3 — a
Phase 2 write failure is an ordinary database error on the §17 database bound.

**A failed write is infrastructure, not a failed report.** The two are handled differently on purpose:

| | Uncited claim at the gate | S3 write refused |
|---|---|---|
| What it means | The report is defective | Storage is having a bad day |
| Retry | **None.** It is an invariant, not an error | 2 retries at 2s and 8s (gl §17) |
| On exhaustion | `status=failed`, uncited claims listed | `status=failed`, reason `export_write_failed` |
| Re-run research or synthesis? | No | **No** — the report was already correct when the gate passed |
| Preserved | Report, claims, `claim_sources`, audit trail | Report, claims, `claim_sources`, audit trail |

Recovering from `export_write_failed` is a re-export of work that is already finished and already
approved. **No endpoint for that exists in gl §12's API surface**, and none has been invented here —
see §22.

### The lifecycle: claim → exported artifact

```mermaid
flowchart LR
    Q["question"] --> P["plan - subtopics"]
    P --> S["search result<br/>url + cleaned content"]
    S --> F["findings row<br/>evidence quote, url, title,<br/>retrieved_at, content_hash, truncated"]
    F --> C["claims row<br/>text, section, supported, verdict_note"]
    F --> CS["claim_sources row<br/>claim_id + finding_id"]
    C --> CS
    CS --> G{"export gate<br/>every claim has at least<br/>one claim_sources row?"}
    G -->|"no"| FAILX["export fails, uncited claims listed<br/>+ audit_events row"]
    G -->|"yes"| PUT{"write artifact to S3<br/>10s, 2 retries at 2s and 8s"}
    PUT -->|"written"| ART["artifact stored<br/>+ audit_events row"]
    PUT -->|"retries exhausted"| WFAIL["status failed, export_write_failed<br/>report and audit trail preserved"]
    C --> R["report in state"]
    R --> G
```

**`claim_sources` is the whole point.** It is a many-to-many join that turns "which URL supports this
sentence?" into a query rather than an investigation (gl §9).

### How traceability is preserved

- Every `Finding` carries `retrieved_at` and `content_hash` (SHA-256 of the fetched text). A page
  changes; a claim made against it in March must still be explicable in June. The hash answers
  whether the page you are reading now is the page the claim was made from (gl §9).
- Every claim carries the `finding_id` list it came from, written by the Synthesizer and persisted as
  `claim_sources` rows.
- `audit_events` records every transition worth reconstructing: job created, plan produced, each
  subtopic researched, each revision, gate opened, reviewer decision, export attempted, export
  result, and every retention deletion.
- `audit_events.actor` is the authenticated caller — `system` for transitions the graph made on its
  own, and the reviewer's identity for anything at the human gate. **An approval row with
  `actor = 'unknown'` is a bug, not a tolerable gap** (gl §9).

---

## 9. Database Model

Conceptual schema only — gl §9's sketch, with the keys, relationships, and indexes made explicit. **No
migration is written in Phase 0.** Alembic migrations arrive in Phase 2 (CLAUDE.md phase plan).

```sql
jobs          (job_id PK, user_id, question, idempotency_key UNIQUE, status, quality_flag,
               revision_count, llm_calls_used, report_json, exported_at,
               created_at, completed_at)

findings      (finding_id PK, job_id FK, subtopic, claim, evidence,
               url, title, retrieved_at, content_hash, truncated)

claims        (claim_id PK, job_id FK, section, text, supported, verdict_note)

claim_sources (claim_id FK, finding_id FK, PRIMARY KEY (claim_id, finding_id))

audit_events  (event_id PK, job_id FK, actor, action, detail JSONB, created_at)
```

### `jobs`

| Field | Notes |
|---|---|
| `job_id` | **PK**, UUID. Also the LangGraph `thread_id` and the LangSmith trace tag |
| `user_id` | Owner. Single tenant today; every table carries it so tenant scoping is additive |
| `question` | The original text, validated and length-capped at the API |
| `idempotency_key` | **UNIQUE, NOT NULL.** `sha256(user_id + question + date)`, derived server-side. See below |
| `status` | Lifecycle: `running` → `awaiting_approval` → `approved` / `rejected` / `failed` |
| `quality_flag` | `NULL`, `below_threshold` (revision cap hit with a failing score), or `unscored` (the scoring call failed and the report was kept) |
| `revision_count`, `llm_calls_used` | Persisted so budget behaviour is auditable after the job ends |
| `report_json` | **JSONB, nullable.** The approved `Report` body, written by the export node **only after the gate passes**. `NULL` means nothing was ever exported. This is what makes the report retrievable in Phase 2, before S3 exists (§8) |
| `exported_at` | Nullable. Set alongside `report_json`. `NULL` here and `status=approved` means the export did not complete |
| `created_at`, `completed_at` | `completed_at` is set by `finalize` only |

**Indexes:** PK on `job_id`; **unique on `idempotency_key`**; `(user_id, created_at desc)` for the
owner's job list; `(status)` for the gate-expiry and retention sweeps.

#### `idempotency_key` — the full design

gl §12 requires the key to be unique in `jobs`; gl §9's original schema sketch omitted the column, so
it has been added there as part of this review.

| Question | Answer |
|---|---|
| **Where stored** | A column on `jobs`. It is **not** in `ResearchState` — it governs job creation, and the graph never reads it |
| **How derived** | `sha256(user_id + question + date)`, computed **server-side** at `POST /jobs`. No client header, so a caller cannot weaken it by sending a random value |
| **Constraint** | `UNIQUE NOT NULL`. The database is the arbiter, not an application-level "check then insert", which races between two API tasks |
| **Duplicate request** | The insert violates the unique constraint. The API catches it, looks up the existing row, and returns **`409` with the existing `job_id`** (gl §12). No second job, no second queue message |
| **Scope** | `user_id` + question + **date**. The same question tomorrow is a new job — deliberate, because competitive research goes stale and re-asking is the normal way to refresh it |

**Idempotency and worker retries solve different problems, and both are needed:**

| Layer | Prevents | Mechanism |
|---|---|---|
| `idempotency_key` unique on `jobs` | A duplicate **request** creating a second job | Database constraint at `POST /jobs` |
| Checkpoint keyed `thread_id = job_id` | A duplicate **delivery** redoing finished work | LangGraph resumes at the last completed node |

SQS is at-least-once, so a redelivered message is expected, not an incident. It carries the same
`job_id`, so the worker loads the same checkpoint and continues. The key never has to be re-checked in
the worker — by the time a message exists, the job row already won or lost the uniqueness race.

### `findings`

| Field | Notes |
|---|---|
| `finding_id` | **PK** |
| `job_id` | **FK → jobs**, cascade with retention |
| `subtopic` | Which planned subtopic produced it |
| `claim`, `evidence` | `evidence` is the **verbatim quote**, not a summary |
| `url`, `title` | The citation |
| `retrieved_at`, `content_hash` | Reproducibility (gl §9) |
| `truncated` | The page was cut at `MAX_PAGE_CHARS` |

**Indexes:** PK; `(job_id)`; `(job_id, url)` supports per-job URL dedupe verification and is the
natural lookup when reflection excludes failing URLs.

**Relationship:** one job → many findings. A finding belongs to exactly one job — evidence is never
shared between jobs, because `retrieved_at` and `content_hash` are per-retrieval facts.

### `claims`

| Field | Notes |
|---|---|
| `claim_id` | **PK** |
| `job_id` | **FK → jobs** |
| `section` | Which report section it appears in |
| `text` | The claim sentence |
| `supported` | Fact-Checker verdict, current value |
| `verdict_note` | Why, including `source unreachable` |

**Indexes:** PK; `(job_id)`; `(job_id, supported)` so "show me the unsupported claims first" at the
gate is one query.

### `claim_sources`

| Field | Notes |
|---|---|
| `claim_id` | **FK → claims** |
| `finding_id` | **FK → findings** |
| — | **PK is the composite `(claim_id, finding_id)`** |

The many-to-many join that makes the audit trail real. One claim may rest on several findings; one
finding may support several claims.

**Indexes:** the composite PK covers `claim_id` lookups; add `(finding_id)` for the reverse question
— "which claims rest on this source?" — which is what you ask when a source turns out to be wrong.

**This table is what the export gate queries.** A claim with zero rows here blocks the export.

### `audit_events`

| Field | Notes |
|---|---|
| `event_id` | **PK** |
| `job_id` | **FK → jobs** |
| `actor` | `system`, or the authenticated identity from §13. Never `unknown` |
| `action` | `job_created`, `plan_produced`, `subtopic_researched`, `revision`, `gate_opened`, `reviewer_decision`, `export_attempted`, `export_result`, `retention_delete` |
| `detail` | `JSONB` — the payload that makes the event reconstructable |
| `created_at` | Append-only, never updated |

**Indexes:** PK; `(job_id, created_at)` — the audit trail is always read as a per-job timeline.

**This table is append-only.** No row is ever updated or deleted except by the retention sweep, which
writes its own row saying so.

### What is deliberately absent

- **No `sources` table.** A source *is* a finding's `url`. `Report.sources` is a view over the
  findings actually cited. A separate table would let a report cite a URL that no finding retrieved,
  which is exactly the drift the audit trail exists to prevent.
- **No `subtopics` table.** The plan lives in state and in the checkpoint; `findings.subtopic` is the
  durable link.
- **No LangGraph checkpoint tables here.** The checkpointer owns them through its own `setup()`
  (gl §19).
- **No vector table, no embeddings** (gl §11).

---

## 10. API Architecture

Five routes. This is the outermost contract in the system, so it gets the same typed treatment as
every internal boundary. **Every route except `/health` requires authentication** (gl §12, gl §16).

| # | Method | Path | Purpose |
|---|---|---|---|
| 1 | `POST` | `/jobs` | Submit a research question |
| 2 | `GET` | `/jobs/{id}` | Poll status, and read the report once it exists |
| 3 | `POST` | `/jobs/{id}/approve` | Decide at the human gate — approve, reject, or edit |
| 4 | `GET` | `/jobs/{id}/report` | Get a presigned URL for the exported artifact |
| 5 | `GET` | `/health` | Liveness and dependency check |

### 1. `POST /jobs`

- **Request:** `{question}`
- **Response:** `{job_id, status}`
- **Codes:** `202` accepted · `400` invalid question · `401` unauthenticated · `409` duplicate
  idempotency key, returns the existing `job_id` · `429` throttled
- **Auth:** required. **Authz:** `submitter` or `reviewer`.
- **Behaviour:** validate and length-cap the question, strip control characters, derive
  `idempotency_key = sha256(user_id + question + date)`, insert the `jobs` row, enqueue the pointer
  message, return `202`. The key is **derived server-side** and the **unique constraint on
  `jobs.idempotency_key` is what decides** — a violation is caught and turned into `409` with the
  existing `job_id`, and no queue message is sent (§9, gl §12).

### 2. `GET /jobs/{id}`

- **Response:** `{job_id, status, phase, revision_count, quality_flag, report?}`
- **Codes:** `200` · `401` · `403` not the owner · `404`
- **Auth:** required. **Authz:** the caller must own the job.
- **Behaviour:** `phase` is a **coarse progress label, not a stream**. No SSE, no websockets. Polling
  a 20-minute job every few seconds is cheap, and streaming partial research to a caller who cannot
  act on it buys nothing. If a UI ever needs real progress, `phase` is the field that widens (gl §12).

### 3. `POST /jobs/{id}/approve`

- **Request:** `{decision: "approve" | "reject" | "edit", note?, edits?}`
- **Response:** `{job_id, status}`
- **Codes:** `200` · `401` · `403` **not a reviewer** · `404` · `409` job is not `awaiting_approval`
- **Auth:** required. **Authz:** role `reviewer` only.
- **Behaviour:** record the decision and the reviewer identity as an `audit_events` row, then resume
  the graph from the checkpoint.

**There is no separate `/reject` route.** Rejection is a `decision` value on this endpoint, as gl §12
defines it. Approving a report is an authorization decision and it is the backstop the whole
injection defense leans on, so one authenticated endpoint owns all three outcomes (gl §16).

### 4. `GET /jobs/{id}/report`

- **Response:** `{url, expires_at}` — presigned S3 URL, 15-minute expiry
- **Codes:** `200` · `401` · `403` · `404` not exported
- **Auth:** required. **Authz:** the caller must own the job.
- **Behaviour:** **report bytes never stream through the API.** The API stays a control plane
  (gl §12).
- **Phase 2:** always `404 not exported`, because there is no artifact until S3 arrives in Phase 3.
  The approved report body is read from `GET /jobs/{id}`, which already carries `report?` (§8).

### 5. `GET /health`

- **Response:** `{status, checks: {db, redis}}` — booleans only
- **Codes:** `200` healthy · `503` degraded
- **Auth:** **none. This is the one unauthenticated route.**
- **Behaviour:** not decoration. The ECS target group and API Gateway need a liveness answer, and a
  task that cannot reach Postgres should fail its health check rather than keep accepting jobs it
  cannot checkpoint (gl §12).

**Why it is unauthenticated.** An ALB target-group health check cannot present a bearer token.
Requiring auth would mean the health check always fails, which means an unhealthy task is never
replaced — the opposite of what the route is for.

**Why that is safe: the response is minimal by design.** It carries a status and one boolean per
dependency. It must never carry a secret, a connection string, a hostname, a version, a job id, a
count, a queue depth, or an error message. If a check needs to explain *why* it failed, that
explanation goes to the structured logs, not to an anonymous caller. Everything an attacker could
learn here is "the service is up" or "the service is degraded" — which they would learn from the
other routes' behaviour anyway.

This is the one place the architecture asked for a change to the authoritative guidance: gl §16
previously said "no anonymous access to anything". It now names `/health` as the single exception,
with the minimal-body rule written alongside it.

### Asynchronous job semantics

`POST /jobs` returns `202`, not the report. The job runs for minutes — five subtopics, several
searches and fetches each, three LLM-heavy stages, possibly three passes. An HTTP request cannot hold
that connection (gl §12). The caller polls `GET /jobs/{id}`.

```text
POST /jobs  → validate, persist, enqueue, return 202 + job_id
                                 ↓
                         SQS (LocalStack locally)
                                 ↓
                         worker: run the graph
                                 ↓
GET /jobs/{id} → status, and the report when it exists
```

### Error response shape

One shape, everywhere:

```json
{"error": {"code": "job_not_awaiting_approval", "message": "...", "job_id": "uuid"}}
```

`code` is a stable string callers may branch on. `message` is for humans and may change freely.
**Nothing else appears in an error body** — no stack traces, no internal paths (gl §12, gl §16).

### Actor identity and authorization boundaries

| Role | May | May not |
|---|---|---|
| `submitter` | `POST /jobs`; read its **own** jobs and reports | Decide anything at the gate |
| `reviewer` | Everything a submitter may, plus approve, reject, or edit at the gate | — |

Every authenticated identity is written to `audit_events.actor`. That is what turns "the report was
approved" into "this person approved it", which is the only version worth auditing (gl §9, gl §16).

---

## 11. Async Worker Architecture

### The flow

```mermaid
flowchart TD
    A["POST /jobs - API container"] --> B["validate question<br/>derive idempotency_key"]
    B --> C["insert jobs row<br/>unique on idempotency_key"]
    C -->|"duplicate key"| DUP["409 - return the existing job_id"]
    C -->|"inserted"| D["enqueue pointer message"]
    D --> E["202 + job_id to the caller"]
    D --> Q["SQS - visibility 25 min"]
    Q --> W["worker: receive message"]
    W --> LOAD["load checkpoint for thread_id = job_id"]
    LOAD --> RUN["run the LangGraph graph<br/>checkpoint written per node"]
    RUN --> INT["interrupt at human_gate<br/>status = awaiting_approval"]
    INT --> DEL["delete the message - worker released"]
    DEL --> APPROVE["POST /jobs/id/approve<br/>records actor + decision"]
    APPROVE --> Q2["enqueue resume message"]
    Q2 --> W2["worker: resume from checkpoint"]
    W2 --> EXPORT["export gate, then S3 write"]
    EXPORT --> FINAL["finalize - terminal status"]
    Q -->|"3 failed deliveries"| DLQ["dead-letter queue<br/>CloudWatch alarm on depth > 0"]
```

### Message structure

```json
{
  "job_id": "uuid",
  "user_id": "uuid",
  "idempotency_key": "sha256(user_id + question + date)",
  "attempt": 1
}
```

**Identifiers only, never the state** (gl §12). State lives in Postgres. A message is a pointer, so a
redelivered message resumes rather than restarts. It also keeps the question — untrusted user text —
out of the queue payload.

### Idempotency and duplicate delivery

**Two different duplicates, two different mechanisms.** Both are needed, and neither substitutes for
the other:

| Duplicate | Where it is stopped | How |
|---|---|---|
| The same **request** submitted twice | `POST /jobs`, in the API | `UNIQUE` on `jobs.idempotency_key`. The insert fails, the API returns `409` with the existing `job_id`, and **no second message is enqueued** |
| The same **message** delivered twice | The worker | The message carries `job_id`; the worker loads the checkpoint for `thread_id = job_id` and resumes at the last completed node |

SQS guarantees **at-least-once** delivery, so duplicate delivery is expected behaviour, not an
incident. Because the message is a pointer rather than state, a redelivery cannot restart a job — it
can only resume one. The key itself is never re-checked in the worker: by the time a message exists,
that job row already won the uniqueness race.

**The failure this prevents:** without the constraint, an impatient client double-submitting would
create two jobs, two threads, and two full sets of LLM calls for one question — 60 calls of budget
spent to produce a duplicate the reviewer then has to read twice.

### Visibility timeout vs job runtime

Visibility timeout **25 minutes**; hard job limit **20 minutes**. The timeout must exceed the job
limit, or SQS redelivers a job that is still running and two workers process it at once. The 5-minute
margin covers worker startup and checkpoint writes.

> **If the job limit ever rises, the visibility timeout rises first** (gl §12).

### Retries and the DLQ

Three deliveries, then the dead-letter queue. A DLQ message means something is broken that a retry
will not fix, and a CloudWatch alarm on `ApproximateNumberOfMessagesVisible > 0` on the DLQ says so
(gl §12, gl §14).

### Worker crash — what happens if a worker dies halfway

1. The message was never deleted, so after the 25-minute visibility timeout SQS makes it visible
   again.
2. A worker receives it, loads the checkpoint for `thread_id = job_id`, and resumes.
3. **Completed nodes are not re-executed.** Checkpoints are written per node, so the most that is
   lost is the single node that was in flight — at worst a few LLM calls, not a whole job.
4. `findings` and `verdicts` use `operator.add` reducers, so the re-executed node appends rather than
   overwriting. That is precisely the "lost writes on retry" case the reducers exist for (gl §4).
5. After three failed deliveries the message goes to the DLQ and the alarm fires.

### Graceful shutdown

On SIGTERM the worker stops taking new messages, **finishes the current node**, writes the
checkpoint, and exits. Fargate's 30-second grace period is enough for a node, not for a whole job —
which is exactly why checkpointing is per-node (gl §12).

### Worker concurrency

**Worker count is bounded by the LLM rate limit, not by queue depth.** A single job can consume a
full minute of a 40 RPM tier, so two concurrent jobs saturate development. Four workers against that
tier do not double throughput — they produce 429s, backoff, and jobs slower than two workers would
have finished (gl §12, gl §13).

| Environment | Workers | Bound |
|---|---|---|
| Local / dev | 1 | The free tier. A second worker mostly waits on the shared Redis bucket |
| AWS (Phase 5) | 2, fixed | Raise only after the production tier's real RPM has been measured |

**The queue-depth alarm is not an autoscaling trigger.** It means "demand exceeds what the rate limit
allows". The two honest responses are to raise the LLM tier or accept the backlog. Adding workers is
the one response that makes things worse. ECS service autoscaling stays off until a measurement says
otherwise (gl §12).

---

## 12. Human Approval Flow

### Where execution pauses

At the `human_gate` node, which sits **between `reflection` and `export`**. LangGraph's `interrupt()`
stops the graph there. No edge reaches `export` without passing through it (CLAUDE.md invariant 6).

### What is persisted at the pause

The full checkpoint for `thread_id = job_id` in Postgres — plan, findings, report, verdicts,
reflection scores, all counters. `status` becomes `awaiting_approval`, the SQS message is deleted,
and **the worker is free**. A job can sit at the gate for days without holding any compute (gl §10).

### What the reviewer sees

Phase 2 is **API-only. The reviewer gets JSON, not a web UI** (CLAUDE.md phase plan).

Ordered deliberately (gl §10):

1. **Any claim marked unsupported by the Fact-Checker** — first.
2. **Any subtopic marked `unresearched`** — first.
3. **`quality_flag`, when it is set** — with the same prominence as the two items above.
4. The reflection score breakdown and `failed_dimensions`.
5. The report, rendered.
6. Every claim with its source URLs and the supporting quote.

The problems come first because **a reviewer who has to hunt for them will approve past them.**

The two `quality_flag` values say different things and the payload must not blur them:

| `quality_flag` | What the reviewer is being told |
|---|---|
| `"below_threshold"` | The rubric ran, the report failed it, and both improvement cycles are spent. `failed_dimensions` says which dimensions |
| `"unscored"` | **The rubric never ran.** The report is complete and fact-checked, but nothing scored it. `failed_dimensions` is empty because it is *unknown*, not because it is clean. You are the only quality judgement on this job |
| `None` | The rubric ran and the report passed |

**`unscored` is never rendered as a pass.** A UI or client that treats an empty `failed_dimensions`
as "no problems" without reading the flag is a bug (§5, §6).

### Decisions

| Decision | Effect |
|---|---|
| `approve` | Resume → export gate → artifact stored, or export fails loudly if any claim is uncited |
| `reject` | `finalize` with `status=rejected`. Nothing is exported. The reason is recorded |
| `edit` | The reviewer's text is written to `reviewer_edit_text`, routed to the Synthesizer for **one** pass, then back to the gate |

**`edit` re-enters the graph. It does not bypass the Fact-Checker** — an edited claim is a new claim,
so it has no `Verdict`, so the Supervisor's existing "some claim has no verdict" row routes it to the
Fact-Checker like any other (gl §10). It still respects `MAX_LLM_CALLS_PER_JOB`.

**An `edit` is not a revision.** A revision is an *automatic* improvement cycle triggered by
reflection (§3). An edit is human-triggered, so it does not increment `revision_count` and cannot be
starved by the cap — a reviewer can still ask for a fix on a job that already spent both cycles.

**[derived] The edit pass is scored but not re-routed.** `fact_checker → reflection` is a fixed edge,
so an edited draft passes through reflection and the reviewer gets a fresh score. But gl §10 says the
edit goes "to the Synthesizer for one pass, **then back to the gate**", so on this path reflection
records its score and routes to `human_gate` regardless of the result. Letting it start an automatic
cycle here would contradict "one pass, then back to the gate" and would let one reviewer edit trigger
open-ended rework.

**`reviewer_edit_text` is consumed exactly once.** The Synthesizer clears it at the end of the pass
that applies it. Left set, a later pass would silently re-apply an edit written against an older
draft. The text survives in `audit_events` regardless (§5).

> **Not built yet — the one Phase-1 gap on this path.** The gate node writes `reviewer_edit_text` on
> an `edit` decision, but the Synthesizer neither reads it nor clears it: it is absent from the
> prompt and absent from `SynthesizerUpdate`. So an edit today routes a Synthesizer pass that has not
> been told what to change, and the field stays set. Both halves — applying the text and clearing it —
> belong with the gate's Phase 2 work (§21 step 17), where the endpoint that produces the text also
> arrives. Recorded here rather than fixed silently, because the field's contract above is the
> specification and the code is what is behind.

### Resuming without re-executing completed work

Resume replays from the checkpoint, not from the start. The Planner, the Researcher, and the
Synthesizer are not re-run for an `approve`; only `export` and `finalize` execute. **Approval after
two days costs nothing beyond the export.** This is the concrete reason for the Postgres
checkpointer — without it, every approval would re-run the entire research pipeline and re-bill every
LLM call (gl §4, gl §10).

**[derived] The approval endpoint records the decision and enqueues a resume message; the worker
resumes the graph.** The API stays a control plane and there is one resume path for all three
decisions. This matters because `edit` is not cheap — it is a Synthesizer pass on the main-tier timeout
(`LLM_MAIN_TIMEOUT_S`, 180s in development) plus a fact-check, which must not run inside an HTTP
request. The alternative (resume inline for
`approve`, enqueue only for `edit`) is faster for the common case but gives the system two resume
paths to test. Confirmed at architecture review; recorded in §20.

A resume message reuses the job's existing `idempotency_key` and `job_id`, so it is subject to exactly
the same at-least-once handling as the original — a redelivered resume replays from the checkpoint
rather than approving twice (§11).

### Expiry

A gate with no decision after **7 days** is closed by the sweep job with `status=rejected`, reason
`gate_expired`. State is retained (gl §10, gl §17).

### How approval identity enters the audit trail

```text
Authorization: Bearer <key>
   ↓  key is hashed, looked up in Secrets Manager, mapped to (user_id, role)
role must be `reviewer`, or 403
   ↓
audit_events row: actor = <the reviewer's identity>, action = "reviewer_decision",
                  detail = {decision, note}, created_at
   ↓
graph resumes
```

`actor = 'unknown'` on an approval row is a bug, not a tolerable gap. A gate that cannot say who
opened it provides accountability theatre rather than accountability (gl §9, gl §16).

---

## 13. Security Architecture

### Authentication

**No route that touches job data is public.** The sharpest reason is `POST /jobs/{id}/approve`:
approving a report is an authorization decision, and it is the backstop the entire injection defense
leans on. A gate anyone can open is not a gate (gl §16).

**`/health` is the single unauthenticated route**, and it is safe because it is minimal: a status and
one boolean per dependency, with no secret, connection string, hostname, version, job data, count, or
error text. A load-balancer health check cannot present a token, and a health check that always fails
means an unhealthy task is never replaced (§10).

| Phase | Mechanism |
|---|---|
| 2 | **Static API keys.** Stored **hashed** in Secrets Manager under `AUTH_KEYS_SECRET_ID`, presented as `Authorization: Bearer <key>`, mapped to a role plus a `user_id` |
| 5 | **API Gateway + Cognito JWT.** The role becomes a token claim; `user_id` comes from `sub` |

The Phase 5 change is confined to **one dependency in the route layer**, because everything
downstream already reads a `user_id` and a role rather than a key.

*Why not JWT from the start?* One user, no signup. Self-hosted issuing, rotation, and revocation is
machinery serving nobody. *Why not defer auth to Phase 5?* The approval endpoint ships in Phase 2;
shipping an unauthenticated gate would make the injection backstop fictional for three phases (gl
§16).

### Authorization

Two roles, because there are exactly two things a caller can do — submit, and decide at the gate. The
table is in §10. A `submitter` presenting a valid key to `/approve` gets `403`, and gl §18 requires a
test for exactly that.

### Secrets

Environment variables locally, Secrets Manager in AWS. **Never in code, never in logs, never in a
trace.** `gitleaks` runs in CI as a merge blocker (gl §16, gl §19).

### Least privilege

One task role per service (gl §16):

| Service | May |
|---|---|
| API task role | Read/write its Postgres schema, send to the job queue, presign objects under its S3 prefix, read the auth secret |
| Worker task role | Receive/delete from its queue, read/write its Postgres schema, write its S3 prefix, read the LLM and Tavily secrets |

Neither role gets anything else. The worker cannot presign; the API cannot consume the queue.

### SSRF — the highest-risk surface

`fetch` takes a URL and makes a request from inside our network. Unrestricted, that is a request
forgery primitive pointed at the cloud metadata endpoint. The four checks are in §7 and run **before
any request and again after every redirect**. Only URLs from a search result's `url` field are ever
fetched (gl §16).

### Untrusted web content and prompt injection

Covered in full in §7. The one-line statement: **fetched content is data, never an instruction**, and
the one place it can influence control flow — the reflection node — is bounded, and the bound is
tested rather than assumed.

### Tool argument validation

Every tool argument is a structured output and is validated before execution: length caps, control
characters stripped, SSRF rules on URLs. Validation failure is recorded in state, never swallowed
(gl §3, gl §7).

### Input validation at the edge

The question is length-capped and stripped of control characters **before it reaches a prompt or the
database** (gl §16).

### Output filtering

Reports are checked for leaked keys and internal paths **before export** (gl §16). Error bodies never
carry stack traces or internal paths (gl §12).

### Audit actor identity

Every authenticated identity is written to `audit_events.actor`; `system` for graph-made transitions.
Detail in §8 and §12.

### PII

**No detection and no redaction is built.** Fetched pages can contain personal data, and LangSmith
receives fetched content. This is acceptable while this is one person researching public company
information, and it is written down so it is a decision rather than an oversight.

Three things would make it unacceptable: a real user who is not the author, a source that is not a
public web page, or a customer contract. The lever, in that order, is redaction before storage and
redaction before the trace is sent — **not turning the audit trail off, because the audit trail is
the feature** (gl §9, gl §14).

### What is deliberately not added

No WAF, no jailbreak classifier, no injection-detection model, no secrets vault beyond Secrets
Manager, no third-party security product. Each would add cost and a false-positive rate for a threat
the structural boundaries already handle (gl §8, gl §16).

---

## 14. Observability

Two layers, one question each. **A signal goes in exactly one of the two** (gl §14).

### LangSmith — "what did the agents and the LLM workflow do?"

One trace per job. One child run per agent invocation, nested under the graph run.

Required metadata on every run, because a trace you cannot find is not observability:

| Tag / field | Why |
|---|---|
| `job_id` | Joins the trace to the database rows and the eval result |
| `agent` | Which agent contract was executing. **The reflection node is tagged `node:reflection`**, so control-flow work stays distinguishable from agent work |
| `model` | Which model produced this — the first question after any quality change |
| `revision` | Which pass; makes "did revision 2 improve anything?" answerable |

Captured: prompts, structured outputs, tool calls **and their arguments**, tokens, cost, latency,
retries, graph runs, agent runs.

### CloudWatch — "is the infrastructure healthy?"

ECS task metrics, SQS queue metrics, and the application's structured JSON logs. Alarms (gl §14):

| Alarm | Threshold | Means |
|---|---|---|
| Queue depth | > 20 for 5 min | Demand exceeds the LLM rate limit. Raise the tier or accept the backlog — **do not add workers** |
| DLQ messages | > 0 | Something is broken that retries will not fix |
| Task restarts | > 3 in 15 min | Crash loop |
| Job error rate | > 10% over 15 min | Systemic failure |

### Targets — the numbers a regression is measured against

Timeouts are not targets. §17's 20-minute limit is when a job is killed, which says nothing about
what normal looks like (gl §14):

| Signal | Target | Alarm | Measured (n=20, 2026-08-13) |
|---|---|---|---|
| Job latency, p50 | ≤ 6 min | — | **13m48s** — 2.3× over |
| Job latency, p95 | ≤ 15 min | > 18 min | **22m01s** — over, and past the alarm |
| Cost per job | ≤ $0.50 | > $2 on any single job | **$0.14** *(derived)* |
| Daily spend | — | > $20 in a day | not measured; NIM spend is $0 |
| Search + fetch cache hit rate | ≥ 30% once revisions run | < 10% for a day | **25%** (157 of 621) |
| Revision rate | ≤ 40% of jobs need one | > 70% | **20%** (4 of 20) |

**Measured by step 12** — 20 real jobs against the real endpoint and the real web, sequential, on
2026-08-12/13; 16 reached `approved`. **Targets are not overwritten**: a target is the aim, the
measurement is the baseline a regression shows up against, and both stay visible.

**Not a production-default benchmark.** The run used the NIM development overrides in `.env` —
`MAX_REVISIONS=3`, `MAX_SUPERVISOR_HOPS=30`, `LLM_MAIN_TIMEOUT_S=180`, `MAX_JOB_RUNTIME=1800` — while
the documented defaults are 2, 24, 60, and 1200 and are unchanged. Two of those four could have moved
a number and two could not; the table that says which is gl §14 "Measurement context", and it is
the one place that claim is maintained.

Every figure above is **NIM development on 2026-08-13**, not a property of the system. That endpoint
generates at ~15–20 output tokens/second (gl §17) and latency follows from it. The latency targets
were written for a production API, are **not met on this hardware**, and are left unchanged rather
than relaxed to fit a development tier — Phase 5 re-baselines them against real hardware.

Caveats that travel with the numbers: **cache hit rate is process-local reuse within one job**, not a
Redis or cross-worker benchmark, and its ≥30% target assumes revisions are running, which only 20% of
jobs did; **revision rate comes from an uncalibrated rubric** (§6, calibration is Phase 4), so it is a
regression baseline and not a quality claim; **p95 at n=20 is one extreme observation**; and **cost is
derived** from measured tokens × assumed production prices, not provider spend (gl §14).

**Where that latency goes, per node, lives in gl §14 and only there** — including the correction that
the Researcher's share is ~90% LLM extraction and ~9% search, fetch, and robots.txt, not the roughly
even split this document's earlier reading of it assumed. It is the evidence
[ADR 0002](adr/0002-concurrent-page-extraction-in-the-researcher.md) rests on. **All of it describes
the pre-ADR-0002 sequential Researcher**, and the re-baseline that would replace it has not run.

### The rule that stops duplicate telemetry

Agent reasoning never goes to CloudWatch. Infrastructure health never goes to LangSmith. When you
cannot decide, ask which question the signal answers — *what did the agents do*, or *is the
infrastructure healthy* — and put it there. `job_id` is the join key between the two, so nothing needs
to be duplicated to be correlatable (gl §14).

### Security note

LangSmith is a hosted third-party service. Prompts, model outputs, and **fetched page content** leave
our infrastructure. That is an acceptable trade for this project, recorded as a conscious choice. If
it stops being acceptable, the lever is redaction before the trace is sent, not turning tracing off
(gl §14).

---

## 15. Failure Scenarios

Every number below comes from gl §17, gl §13, gl §3, or gl §12. **No new retry schedule has been
invented** — the two rows gl §17 previously lacked (rate-limiter acquisition and the S3 artifact
write) were added to that table at architecture review, reusing values already in it, so the numbers
still live in exactly one place.

| Scenario | Detection | Retry / fallback | Terminal behaviour |
|---|---|---|---|
| **LLM timeout (main)** | `LLM_MAIN_TIMEOUT_S`, default 60s — **one value for every main-tier caller** | 2 retries, backoff 2s, 8s | Fail the node, record the reason → `finalize`, `status=failed` |
| **LLM timeout (fast)** | 30s timeout | 2 retries, backoff 1s, 4s | Same |
| **LLM 429** | HTTP 429 | 3 attempts; `Retry-After` if present, else 2s, 8s, 30s | Fail the **job**, reason `rate_limited`. A rate-limited job fails visibly; it never silently produces a shorter report |
| **Malformed structured output** | Schema validation fails | **1 retry, with the validation error in the prompt** | Fail explicitly. **Never substitute a default** — a wrong value survives into the report and looks deliberate |
| **Search failure** | 15s timeout, or tool error | 2 retries, backoff 1s, 4s | That query yields no findings for the subtopic; the Researcher may still have budget for another query |
| **Empty search results** | Zero usable results | Remaining subtopic budget (3 calls) may try another query | Zero findings after retries → subtopic `unresearched`, job **continues**, gap carried into the report, reflection scores completeness down |
| **Source unavailable** | Fetch 10s timeout, non-2xx, > `MAX_FETCH_BYTES`, disallowed content type, robots-blocked | 1 retry, backoff 2s | Source marked `unreachable`. At fact-check: `supported=false`, `note="source unreachable"` — never a guess |
| **Page over `MAX_PAGE_CHARS`** | Cleaned length check | none — head is kept | `Finding.truncated = true`. Never a silently shortened finding |
| **Reflection scoring call fails, report exists** | Timeout, or invalid `ReflectionScore` after its retry | Fast-model policy: 2 retries at 1s, 4s | **The report is kept.** `quality_flag="unscored"`, `revision_count` unchanged, `audit_events` records `reflection_failed`, route to the human gate. **`unscored` is not a pass** — the export gate and the reviewer both still apply |
| **Reflection scoring call fails, no report yet** | Same | Same | Nothing to gate: fail the node per gl §17 → `finalize`, `status=failed` |
| **Revision limit reached** | `revision_count >= MAX_REVISIONS` with a failing score | none — the loop is over | Job continues with `quality_flag="below_threshold"`, breakdown attached, **the reviewer decides**. Citation coverage still blocks export regardless |
| **Supervisor guard trips** | `hop_count >= 12` or `llm_calls_used >= 60` | none | `finalize`, `status=failed`, `failure_reason` set |
| **Whole job exceeds 20 min** | Worker job timer | none | `finalize`, `status=failed`, reason `job_timeout` |
| **SQS retry / duplicate delivery** | Message redelivered after 25-min visibility timeout | 3 deliveries | Then DLQ + CloudWatch alarm. A duplicate resumes from the checkpoint; it never restarts the job |
| **Worker crash** | Visibility timeout expires without a delete | Redelivery, resume from the last per-node checkpoint | At most the in-flight node is re-executed. After 3 deliveries → DLQ |
| **PostgreSQL failure** | 5s query timeout | **0 retries** | Fail loudly. `/health` reports `db` unhealthy → `503` → the ECS target group takes the task out of service |
| **Redis failure — cache or dedupe** | Connection error on `cache:*` or `job:{id}:urls` | **Fail open.** Treat as a miss; log it | The job continues. Cost is a repeated call or a duplicate fetch, both bounded by `MAX_LLM_CALLS_PER_JOB` |
| **Redis failure — rate limiter** | Connection error on `ratelimit:llm` | 5s timeout, 2 retries at 2s, 8s (gl §17) | **Fail closed.** No token, no LLM call → `finalize`, `status=failed`, reason `rate_limiter_unavailable`. A limiter that fails open is not a limiter |
| **S3 write fails at export** | Error from the artifact write **after** the gate passed | 10s timeout, 2 retries at 2s, 8s (gl §17) | `finalize`, `status=failed`, reason `export_write_failed`. **Report, claims, `claim_sources`, and audit trail preserved. Research and synthesis are never re-run** — the report was already correct |
| **Export gate blocks** | Any claim with zero `claim_sources` rows | none — this is an invariant, not an error | **Export fails, listing the uncited claims.** Runs even when the reviewer approved, because approval is a judgement about quality and this is a structural invariant |
| **Human rejection** | `decision="reject"` | none | `finalize`, `status=rejected`, reason recorded, nothing exported, state retained |
| **Gate expiry** | No decision in 7 days | none | Sweep job closes it: `status=rejected`, reason `gate_expired` |

Every row has an exhaustion behaviour. **A retry policy without one is an infinite loop with extra
steps** (gl §17).

---

## 16. Cost and Rate-Limit Architecture

### The constraint

The NIM free tier allows roughly **40 requests per minute**, with per-model ceilings NVIDIA does not
publish. One job's calls (gl §13):

| Stage | Calls | Model |
|---|---|---|
| Planner | 1 | main |
| Supervisor | 1 per hop, up to 24 | fast |
| Researcher | 3 per subtopic-pass, up to 15 passes | main |
| Synthesizer | 1 per pass × up to 3 passes | main |
| Fact-Checker | 1 batched per pass × up to 3 passes | main |
| Reflection *(control-flow node)* | 1 per pass × up to 3 passes | fast |
| **Total** | **p50 26 measured (n=20, 2026-08-13); caps sum to 79** | |

`MAX_LLM_CALLS_PER_JOB` = 60 — **below** the sum of the caps, so it is the binding guard rather than
headroom, and below the point where a runaway job goes unnoticed. **A single job can consume a full minute of the entire rate budget, so two concurrent jobs
saturate the development tier.** That is a development constraint to plan around, not a bug.

### Where each control lives

| Control | Lives in | Enforced how |
|---|---|---|
| **Per-job LLM call budget** | `ResearchState.llm_calls_used`, checked by the Supervisor | Every LLM caller increments; the Supervisor routes to `finalize` at 60 |
| **Shared rate limiter** | Redis `ratelimit:llm`, rolling 60s token bucket at `LLM_RPM_LIMIT` | **One bucket across all workers.** Two workers each politely limiting to 40 RPM produce 80 |
| **Batching** | Fact-Checker: all claims in **one** call per pass | A contract in gl §2.5, tested as a contract |
| **Model tiers** | `LLM_FAST_MODEL` for the Supervisor and the reflection node; `LLM_MODEL` for everything else | Config, not code |
| **Caching** | Redis `cache:search:*`, `cache:fetch:*`, 24h TTL | In the MCP tool layer, so both agents get it for free. Hits and misses logged |
| **429 backoff** | The single OpenAI-compatible LLM client | `Retry-After`, else 2s/8s/30s, 3 attempts, then fail the job |
| **Input size cap** | `MAX_PAGE_CHARS` = 24,000, applied at fetch | Caps the token cost of a Researcher call before the call is made |
| **In-job concurrency** | `RESEARCHER_CONCURRENCY` = 3, checked at startup (ADR 0002) | The Researcher's extraction pool. **The only bound on how many requests one job holds open until the shared limiter arrives in Phase 3** |

### Concurrency inside one job

**Measured (n=20, 2026-08-13): one job ran at 1.76 requests/minute — 4.4% of `LLM_RPM_LIMIT` — with
one request in flight at a time.** The "a single job can consume a full minute of the entire rate
budget" line above describes the worst case the call caps permit, not the jobs this system runs.

ADR 0002 spends part of that headroom: a subtopic's extraction calls overlap, so a job holds up to
`RESEARCHER_CONCURRENCY` requests open and runs at roughly 5.3 requests/minute — about 13% of the
development tier. Three consequences, all of them Phase 3 work to close properly:

- Until the shared Redis bucket exists, `RESEARCHER_CONCURRENCY` is the *only* thing bounding in-job
  concurrency. It is refused outside 1..`MAX_LLM_CALLS_PER_SUBTOPIC` at startup rather than clamped.
- **Two concurrent jobs plus this change is the combination to avoid** on the development tier. §19's
  worker table was written against a job holding one request open and stays at one worker locally.
- The 429 path is now reachable from inside a single job. Each request retries on gl §13's schedule,
  and a job that exhausts it still fails visibly with `rate_limited`.

The call budget is unaffected: every request spends from the same `MAX_LLM_CALLS_PER_JOB`, and
`CallBudget.spend()` is guarded so the ceiling holds when several threads reach it together.

### Calls are the rate unit; tokens are the cost unit

The table above counts **calls**, because the free tier limits requests per minute. **Spend does not
work that way.** A Researcher call carrying a 24,000-character page costs roughly ten times one
carrying a 2,000-character snippet, and the call count is identical either way.

Counting only calls means `MAX_LLM_CALLS_PER_JOB` can be satisfied by a job that cost ten times what
it should have. So both units are tracked and both have a ceiling (gl §13):

| Unit | Ceiling | Enforced by | Measured (n=20, 2026-08-13) |
|---|---|---|---|
| LLM calls per job | 60 | `MAX_LLM_CALLS_PER_JOB`, checked in state | **p50 26, max 44** |
| Input characters per page | 24,000 | `MAX_PAGE_CHARS`, applied at fetch | — |
| Tokens per job | **p50 115k, max 253k**; alarm at 600k | Recorded per job from LangSmith | **p50 114,967, max 252,503** |
| Cost per job | target ≤ $0.50, alarm at $2 | Recorded per job | **$0.14 derived** |

**Measured by step 12.** The old "~250k typical" was about 2× too high; the measured p50 is 115k, and
the max of 253k means a heavy job does reach the old estimate, so the 600k alarm stays. Cost is
**derived** from measured tokens × assumed production-model prices — an assumption, not provider
spend. NIM development spend is $0 (gl §13).

### Be honest about the two-tier win

The cheap model runs the Supervisor and the reflection node and nothing else — roughly **7 of the 26
typical calls**. The Researcher alone can be 45 main-model calls at its cap. **Most
calls are on the expensive model, and the two-tier split trims the edges rather than the bulk.** The
bulk is trimmed by caching and by `MAX_PAGE_CHARS` (gl §13).

### Budget arithmetic — the caps do not sum below the budget

**Corrected 2026-08-13.** Two units must be kept apart, and the previous version of this table did
not: a **logical call** is one agent decision (`call_structured`), while a **request** is one HTTP
call and is what `llm_calls_used` counts — `CallBudget.spend()` runs at the top of every attempt in
`LLMClient._send`, so validation, transport, and 429 retries each cost one.

| Component | Executions | Logical calls each | Max logical calls |
|---|---|---|---|
| Planner | 1 | 1 | 1 |
| Supervisor | ≤ 24 hops | 1 | 24 |
| Researcher | ≤ 15 subtopic-passes | 3 | **45** |
| Synthesizer | 1 initial + 2 revisions | 1 | 3 |
| Fact-Checker | 1 per report-producing pass | 1 batched | 3 |
| Reflection *(control-flow node)* | 1 per report-producing pass | 1 | 3 |
| **Job total — automatic workflow** | | | **79** |

**The old figures — 41 across five agents, 44 in total — were wrong in two independent ways.** They
mixed the two units, allowing the Planner, Synthesizer, and Fact-Checker a validation retry each while
allowing the Supervisor and reflection none. And they counted the Researcher as `3 × 5 = 15`, **as if
each subtopic were researched once**: a Researcher-routed revision returns up to all five subtopics to
`pending` (§6.2, `_thin_subtopics`), so with `MAX_REVISIONS` = 2 the Researcher runs up to **15
subtopic-passes and 45 calls**.

**79 is the automatic workflow's cap — no reviewer edits — and it is what describes the system as
built.** A reviewer `edit` re-enters at the Synthesizer (§12) and adds one Synthesizer, one
Fact-Checker, and one reflection pass each: 3 logical calls per edit, the Supervisor hop already
counted inside the 24.

| Case | Reviewer edits | Cap | Status |
|---|---|---|---|
| Automatic workflow only | 0 | **79** | **What exists today.** Phase 1 has no external route to the gate |
| Planned bound | 3 (`MAX_REVIEWER_EDITS`) | **88** | **Planned, not built** — arrives with the Phase 2 gate (§21 step 17) |
| Current hop margin at its limit | 4 | 91 | An artefact of `MAX_SUPERVISOR_HOPS` = 24 allowing 4 edits at 1 hop each — **not a design target** |

`total = 1 + 24 + 45 + 3 × (3 + E)`. **91 must not be quoted as the production worst case:** the
reviewer-edit path is neither bounded nor fully implemented — `reviewer_edit_text` is written by the
gate and read by nobody (§12, §22 item 3), and no edit counter exists anywhere.

**All three exceed `MAX_LLM_CALLS_PER_JOB` = 60, so the budget is the binding guard in every case**
rather than headroom above a worst case. That is the role gl §5 gives it — the guard that catches
everything the hop and revision caps do not — and it means a job running every component to its cap
ends with `budget_exceeded`, loudly, which is the designed outcome. **Bounding reviewer edits does
not require revisiting 60.**

**Measured (n=20, 2026-08-13): p50 26 requests, max 44.** The old estimate of ~24 typical was close.
The measured max of 44 happens to equal the old worst-case figure; that is a coincidence and confirms
nothing. If a job that is genuinely doing useful work ever ends on `budget_exceeded`, the choice is to
raise 60 or lower a component cap — a decision to take when it is observed, not before.

---

## 17. Local Architecture — Phase 1 today, Phases 2–3 as it gets built

### Phase 1 — today

**A Python process, and nothing else.** No container, no database, no queue. What runs:

| Command | What it does | Needs |
|---|---|---|
| `pytest` | The whole suite. **Zero network calls** — the LLM, Tavily, and DNS are all replaced (gl §18) | Nothing but the dev dependencies |
| `python scripts/check_model.py` | The preflight: does the configured endpoint answer, support JSON mode and tool calling, and what throughput does it show | Real `LLM_*` credentials |
| A graph run | `build_graph()` then `invoke()`, from a Python session or a test | Real `LLM_*` and `TAVILY_API_KEY` |

There is no `uvicorn app:app` and no `python -m worker` yet — those arrive with Phase 2 and Phase 3.
`docker compose up` has nothing to start.

### What actually needs to run, phase by phase

| Phase | What runs locally | What is **not** needed yet |
|---|---|---|
| **1** (today) | Python process only. In-memory checkpointer, in-memory state. Needs `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` and `TAVILY_API_KEY` | No Postgres, no Redis, no queue, no S3, no API. The gate node exists and pauses, but nothing outside the process can resume it yet |
| **2** | + PostgreSQL 16 (checkpointer, audit tables, Alembic) and the FastAPI app via `uvicorn` | No queue and no S3 yet. The export gate writes the approved body to `jobs.report_json` until Phase 3 wires S3 (§8) |
| **3** | + Redis 7, + LocalStack (SQS and S3), + the worker process, all via Docker Compose | Nothing else. This is the full local shape |

### Logical component → local mapping (Phase 3)

| Logical | Local |
|---|---|
| PostgreSQL / RDS | PostgreSQL 16 container |
| Redis / ElastiCache | Redis 7 container |
| SQS | LocalStack |
| S3 | LocalStack |
| API container | `uvicorn app:app --reload`, or a container |
| Worker container | `python -m worker`, or a container |
| API Gateway | **Not emulated.** It is throttling and auth entry; the API's own auth covers local |
| CloudWatch | **Not emulated.** Structured JSON logs go to stdout |
| LangSmith | The real hosted service, enabled by `LANGSMITH_TRACING` |
| LLM endpoint | The real NIM endpoint — there is no local model |

Two things are deliberately not faked locally: **the LLM endpoint and LangSmith**. A local stub would
not exercise the rate limit, the 429 path, or the trace shape, which are the three things development
most needs to get right. For **tests**, the opposite is true, and this is built: graph tests use a
`FakeLLM` and recorded tool responses and make zero network calls (gl §18).

`scripts/check_model.py` is the local preflight, and it exists: it confirms the configured endpoint
answers, supports tool calling and JSON mode, and reports the observed rate limit. Run it after any
model change (CLAUDE.md).

Container files are **not** written yet. Docker Compose arrives in Phase 3.

---

## 18. Production Architecture — Phase 5

Every service below appears in the CLAUDE.md stack table. Nothing has been added.

```mermaid
flowchart TD
    USER["Client - submitter or reviewer"] --> AGW["API Gateway<br/>throttling, Cognito JWT authorizer"]
    AGW --> APISVC["ECS Fargate - API service<br/>2 tasks behind a target group"]

    APISVC --> RDS[("RDS PostgreSQL<br/>jobs, findings, claims,<br/>claim_sources, audit_events,<br/>checkpoints")]
    APISVC --> SQSQ["SQS job queue<br/>visibility 25 min"]
    APISVC --> S3B[("S3 bucket<br/>report artifacts")]
    APISVC --> SM["Secrets Manager<br/>hashed API keys, LLM and Tavily keys"]

    SQSQ --> WSVC["ECS Fargate - worker service<br/>2 tasks, fixed, no autoscaling"]
    SQSQ --> DLQ2["Dead-letter queue<br/>after 3 deliveries"]

    WSVC --> RDS
    WSVC --> EC[("ElastiCache Redis<br/>cache, URL dedupe,<br/>shared rate limiter")]
    WSVC --> S3B
    WSVC --> SM
    WSVC --> NIM["LLM endpoint<br/>OpenAI-compatible"]
    WSVC --> TAV["Tavily, via the tool boundary"]
    WSVC --> LSMITH["LangSmith<br/>agent and LLM traces"]

    ECR["ECR<br/>one image, tagged by commit SHA"] --> APISVC
    ECR --> WSVC

    APISVC --> CWL["CloudWatch<br/>logs, metrics, alarms"]
    WSVC --> CWL
    SQSQ --> CWL
    DLQ2 --> CWL
    RDS --> CWL
```

### Control flow

1. The client calls API Gateway with a Cognito JWT. API Gateway throttles and validates the token.
2. The API service reads `user_id` from `sub` and the role from a token claim, then behaves exactly
   as it did with API keys — **everything downstream already reads a `user_id` and a role**.
3. The API writes the `jobs` row to RDS and enqueues the pointer message to SQS.
4. A worker task receives it, runs the graph, checkpoints to RDS per node, uses ElastiCache for the
   shared rate limiter and caches, calls the LLM endpoint and Tavily-via-MCP, and traces to
   LangSmith.
5. At the gate the graph interrupts, the message is deleted, and the worker is free.
6. On approval the graph resumes, the export gate runs, the artifact is written to S3, and the audit
   event is recorded.
7. `GET /jobs/{id}/report` returns a 15-minute presigned S3 URL.

### Data flow

Untrusted web content flows **in** through the MCP tool layer only. Durable facts flow to RDS.
Artifacts flow to S3. Agent telemetry flows to LangSmith. Infrastructure telemetry flows to
CloudWatch. Nothing flows from a fetched page into a routing decision, a tool argument, or an
authorization check (§7).

### Deployment posture

- ECS **rolling deploy**, one service revision at a time. No canary, no blue/green — one service, one
  region, a handful of jobs a day (gl §19).
- `alembic upgrade head` runs as a **one-off ECS task that must exit 0 before the new service
  revision starts**.
- Every migration is **backward compatible for one release**, which is what makes rollback = redeploy
  the previous task definition. That is why images are tagged with a commit SHA and never only
  `latest`.
- Autoscaling is **off**. Worker count is fixed at 2 and bounded by the LLM rate limit, not queue
  depth (§11).

---

## 19. Deployment Boundaries

**One image, two entrypoints. [derived]** gl §19's pipeline builds one image and pushes it to ECR;
the repository is flat and both processes share the same schemas, state, and database layer. The API
runs `uvicorn app:app`, the worker runs `python -m worker`. Two images would double the build, the
scan, and the tag bookkeeping to separate code that is already separated by module. Flagged in §22.

### API container

| | |
|---|---|
| **Runs** | `uvicorn app:app` |
| **Owns** | HTTP request handling, authentication, authorization, question validation, job creation, status and result reads, the approval endpoint, `/health` |
| **Talks to** | Postgres (read/write), SQS (send), S3 (presign), Secrets Manager (read) |
| **Never** | Runs the graph, calls the LLM, calls a tool, or fetches a web page |
| **Scaling** | On HTTP demand. Requests are milliseconds |
| **Shutdown** | Drain in-flight requests |

### Worker container

| | |
|---|---|
| **Runs** | `python -m worker` |
| **Owns** | SQS consumption, LangGraph execution, all five agents, the reflection node, the tool layer, checkpointing, persistence of findings and claims, the export gate and the S3 write |
| **Talks to** | SQS (receive/delete), Postgres (read/write), Redis, MCP/Tavily, the LLM endpoint, S3 (write), LangSmith, Secrets Manager |
| **Never** | Serves HTTP or makes an authorization decision |
| **Scaling** | **Fixed.** 1 locally, 2 in AWS, bounded by the LLM rate limit |
| **Shutdown** | SIGTERM → finish the current node → checkpoint → exit |

### Why they must be separate

1. **Different scaling bounds.** The API scales on HTTP demand. The worker is capped by a 40 RPM LLM
   tier and **must not** scale on queue depth. One process cannot honour both rules.
2. **A 20-minute job must never occupy an HTTP worker.** That is the reason the queue exists at all.
3. **Different IAM roles.** Least privilege means the worker's role reads its queue and writes its
   bucket prefix and nothing else; the API's role cannot consume the queue. One container would need
   the union of both.
4. **Different shutdown semantics.** The API drains requests in milliseconds; the worker must finish
   a node and write a checkpoint within Fargate's 30-second grace period.
5. **Different blast radius.** A worker crash loop must not take the status API down with it — the
   status API is how anyone finds out there is a crash loop.

---

## 20. Architecture Decisions

Decisions already established by CLAUDE.md and the engineering guidelines, recorded here so the
blueprint carries its reasoning. **No ADR files are created in Phase 0.** Rows marked **[derived]**
are choices this document had to make; they are listed again in §22 for confirmation.

| # | Decision | Reason | Trade-off |
|---|---|---|---|
| 1 | **Five agents; reflection is a control-flow node** | Reflection has no tools, no persona, and no goal of its own. Calling it an agent would break the rule that every agent has exactly one job, and would make the count wrong | The word "node" needs explaining in an interview; the honesty is worth it |
| 2 | **Supervisor routes on structured state only** | It is the structural defense against prompt injection reaching control flow | Routing cannot use nuance that only appears in the text |
| 2a | **[ADR 0001]** The Supervisor's LLM call is **advisory**; `allowed_target(state)` is authoritative | The route returned was always `allowed_target(state)`, so the model could only agree or kill the job — and it killed the first two real jobs, with two different fast models. Strengthens row 2: the one component an injected page could influence now has no authority | 6–12 fast-tier calls per job that cannot change behaviour, kept deliberately so the disagreement rate can be measured before deciding to remove them |
| 3 | **Reflection routes by failed dimension, not a blind rerun** | Rerunning the whole pipeline wastes correct work and burns the call budget | A scoring bug sends work to the wrong specialist; capped by `MAX_REVISIONS` |
| 4 | **One OpenAI-compatible client, no provider classes** | NIM and the production API are both OpenAI-compatible. Swapping a model is a config change plus a preflight plus an eval run | If a non-compatible provider is ever needed, that is a real change — accepted |
| 5 | **Two model tiers** | Routing and scoring do not need the main model | Trims the edges, not the bulk. Stated openly rather than oversold |
| 6 | **LangGraph with a Postgres checkpointer** | The human gate can hold a job for days; in-memory state dies with the worker and re-bills every LLM call | A database dependency for a graph that would otherwise be in-process |
| 7 | **SQS between the API and the worker** | Research takes minutes; an HTTP request cannot hold that connection | At-least-once delivery, so idempotency is mandatory |
| 8 | **The message is a pointer, never state** | A redelivered message resumes instead of restarting; untrusted question text stays out of the queue | Every consumer must read Postgres to do anything |
| 9 | **Worker count bounded by the LLM rate limit, not queue depth** | Four workers on a 40 RPM tier produce 429s and slower jobs, not throughput | Backlog under load is accepted. Autoscaling stays off |
| 10 | **Fact-checking is one batched call per pass** | One call per claim triples job cost and is the easiest way to blow the 60-call budget | A single malformed batch response costs more than a single claim would |
| 11 | **Claim-to-URL audit trail via `claim_sources`** | "Which URL supports this sentence?" becomes a query, not an investigation | Every claim path must carry `finding_id`s; the Synthesizer contract enforces it |
| 12 | **Export gate is code, not a score** | Coverage is arithmetic. A judge would add variance to a number that should be exact | A correct claim whose citation was dropped blocks the whole export — intended |
| 13 | **Human gate before export, authenticated** | It is the backstop for everything the automated checks miss, and a gate anyone can open is not a gate | Jobs stall on a human. Mitigated by 7-day expiry |
| 14 | **API keys in Phase 2, Cognito JWT in Phase 5** | The approval endpoint ships in Phase 2; an unauthenticated gate would make the injection backstop fictional for three phases | Key rotation is manual until Phase 5 |
| 15 | **Two observability layers, one question each** | A signal in both places is a signal nobody trusts | You must know which question you are asking before you log |
| 16 | **LangSmith Evaluation only; no second eval framework** | Two evaluation tools mean two dashboards that disagree | No pytest-based offline eval in CI. Revisit with an ADR if needed |
| 17 | **No vector memory** | One question per job from freshly retrieved sources. No cross-session recall requirement exists | Cross-job recall would need an ADR and new machinery |
| 18 | **Tavily specifically, behind one tool boundary** | It returns cleaned content **and** the URL together, which the audit trail needs; one boundary is one place for validation, timeouts, and rate limiting. **Built in-process as `tools/`; the MCP protocol hop was not added** — with two tools in one process it would add a hop and a failure mode without changing what is enforced, and `fetch` has to be ours (§7) | If a third-party tool server ever has to be consumed, MCP returns as a real requirement |
| 19 | **Reflection's injection exposure is bounded and tested, not eliminated** | Scoring a report means reading it. Claiming immunity would be false | An injected page can waste a revision. The bound is what gets tested |
| 20 | **Head-first truncation at `MAX_PAGE_CHARS`** | Simple and cheap, and `truncated=true` makes the gap explicable and measurable | Evidence deep in a long document is invisible. Relevance-selected extraction needs an ADR |
| 21 | **Two roles only: `submitter` and `reviewer`** | There are exactly two things a caller can do | No per-job delegation. Additive later |
| 22 | **Rejection is a decision value, not a separate route** | One authenticated endpoint owns every gate outcome, so one place records the actor | A slightly busier request body |
| 23 | **[derived]** Reflection's weighted score, threshold check, and route are computed **in code**; the model returns five integers | Mirrors "the model proposes; the code disposes" and shrinks what an injected page can influence | The model cannot express "this is bad for a reason the rubric misses" |
| 24 | **[derived]** The approval endpoint enqueues a resume message; the worker resumes | Keeps the API a control plane, and `edit` is a Synthesizer pass on the main-tier timeout (180s in development) that must not run in an HTTP request | Approval takes one queue hop before the export runs |
| 25 | **[derived]** One image, two entrypoints | The CI pipeline builds one image; the processes share schemas, state, and the database layer | The API image carries agent code it never executes |

### Decisions taken at architecture review

Ten open questions were resolved before implementation. These are the ones that changed the design.

| # | Decision | Reason | Trade-off |
|---|---|---|---|
| 26 | **A Researcher route invalidates the draft** — reflection sets `report = None` and returns targeted subtopics to `pending` | Without it the Supervisor routes a re-researched job straight to the Fact-Checker and the new findings never enter the report. New evidence must pass through synthesis and verification | One extra Synthesizer pass per completeness retry — 2 calls, already inside the 60-call budget |
| 27 | **Three state fields added and no more:** `quality_flag`, `reviewer_edit_text`, `failed_dimensions` | Each carries something across the interrupt that cannot be recomputed on the far side | Three more fields in every checkpoint. Retry *scope* was deliberately not added — `subtopic_status` already carries it |
| 28 | **A failed reflection score keeps the report** — `quality_flag="unscored"`, route to the gate | A complete, fact-checked report is too expensive to discard because the scorer broke, and the human gate exists precisely to catch what automation misses | A job can reach a reviewer with no quality score. Mitigated by making `unscored` as prominent as `below_threshold` and never treating it as a pass |
| 29 | **Redis fails open for caches, closed for the rate limiter** | A cache miss costs one call; an unlimited limiter costs the whole tier at once, across every worker | A Redis outage stops LLM work entirely. Acceptable at 1–2 workers, and loud rather than silent |
| 30 | **A failed S3 write is bounded, then terminal — and never re-runs research** | The report was already correct when the gate passed. Regenerating it would re-bill the whole pipeline for a storage error | The job ends at `failed` with a finished report and no artifact. A re-export path is still unresolved (§22) |
| 31 | **`/health` is unauthenticated and minimal** | A load-balancer health check cannot present a token, so requiring auth means an unhealthy task is never replaced | One anonymous route. Bounded by a body that carries a status and two booleans and nothing else |
| 32 | **A revision is one automatic improvement cycle after the initial report;** `revision_count` is 0-based and the cap check is `>=` | "Revision" was being used for both a pass and a retry. Naming that costs an off-by-one in a loop guard is worth fixing before code exists | The guidance's budget tables now say "per pass" — a wording change in three places |
| 33 | **~~Worst-case call count is stated as exactly 44 (41 across the five agents)~~ — superseded 2026-08-13** | The 44 was wrong twice over: it mixed logical calls with requests, and counted the Researcher as if each subtopic were researched once, ignoring re-research on revisions. The caps actually sum to **79**, which is above `MAX_LLM_CALLS_PER_JOB`, so the budget is the binding guard (§16) | None. No limit changed; 60 still stands, now as a bound rather than headroom |
| 34 | **`idempotency_key` is a `UNIQUE NOT NULL` column on `jobs`, derived server-side** | The database is the arbiter. A check-then-insert races between two API tasks, and a client-supplied key can be weakened by the client | The same question on the same day cannot be re-run deliberately until the date rolls over |
| 35 | **[derived]** "Unchecked draft" means *some claim has no `Verdict`*, matched by `claim_id` | Needs no revision tag and no new field, and gives the `edit` path correct re-verification for free | Depends on the Synthesizer minting fresh claim ids on every pass — a contract test, not an assumption |
| 36 | **Phase 2 stores the approved report body in Postgres (`jobs.report_json`); S3 arrives in Phase 3** | The export *gate* is the invariant worth shipping in Phase 2, and it does not need object storage. Pulling LocalStack forward would add infrastructure to the phase whose point is persistence and the gate. No stand-in for S3 is built — no storage abstraction, no local artifact writer | `GET /jobs/{id}/report` answers `404 not exported` for one phase; the body is read from `GET /jobs/{id}` instead. Phase 3 adds the `PutObject` to the same node |
| 37 | **[ADR 0002]** A subtopic's page extractions run concurrently; choosing and fetching sources stays sequential | 94.3% of a job's wall clock was time inside an LLM request, with one in flight and 4.4% of the rate budget used. The Researcher's 45.2% is 90% extraction calls and 9% tools, and 75 of 89 subtopic passes sent all three extraction requests | A job holds up to 3 requests open with no shared limiter to bound it until Phase 3; more findings reach the Synthesizer, whose latency tracks claim count |

---

## 21. Implementation Order

Dependency-aware, and aligned to the CLAUDE.md phase plan. Each step names what it needs from the
step before, so a step cannot be started early by accident.

### Phase 1 — the graph runs locally, in memory

**Phase 1 is complete. All twelve steps are done**, step 12 on 2026-08-13. The next work is Phase 2,
starting at step 13.

| # | Step | Depends on | Status · why here |
|---|---|---|---|
| 1 | **Architecture review** — this document | CLAUDE.md, engineering guidelines | **Done.** Ten of ten review questions decided, plus the Phase 2 export target (§22) |
| 2 | **Project skeleton** — the flat directories in CLAUDE.md, plus pinned dependencies with a comment naming the requirement for each | 1 | **Done.** `pyproject.toml`; no directory holds one small file |
| 3 | **Configuration** — env-var loading, defaults from CLAUDE.md's table, no config framework | 2 | **Done.** `config.py` |
| 4 | **Pydantic schemas** — `SupervisorDecision`, `ResearchPlan`, `Finding`, `Report`, `Verdict`, `ReflectionScore`, `SearchResult` | 3 | **Done.** `schemas.py`, plus `FetchedPage` for the fetch path |
| 5 | **`ResearchState`** + reducers + `thread_id = job_id` | 4 | **Done.** `graph/state.py` |
| 6 | **LLM client** — one OpenAI-compatible client, structured output + one validation retry, timeouts, 429 backoff, per-job call counting | 3, 4 | **Done.** `llm_client.py`, with `scripts/check_model.py` as the preflight |
| 7 | **Tavily tool boundary** — argument validation, SSRF checks, timeouts, retries, size limits, normalization. **Cache and dedupe interfaces defined, Redis wired in step 21** | 4, 6 | **Done.** `tools/`, in-process — no MCP protocol hop (§7) |
| 8 | **The five agents**, one module each, in contract order: Planner → Researcher → Synthesizer → Fact-Checker → Supervisor | 5, 6, 7 | **Done.** `agents/` |
| 9 | **Reflection node** — rubric prompt, code-side weighting, threshold, targeted route selection, **draft invalidation on a Researcher route**, and the `unscored` failure path | 5, 8 | **Done.** `graph/reflection.py` |
| 10 | **LangGraph wiring** — nodes, the two conditional edges, the three loop guards, in-memory checkpointer | 8, 9 | **Done.** `graph/build.py` — the first point at which a whole job runs end to end |
| 11 | **`FakeLLM` + graph tests** — routing, loop guards, reducers, revision counting; zero network calls | 10 | **Done.** `tests/harness.py`, plus whole-job integration and graph-level injection tests (gl §18) |
| 12 | **First 20 real jobs** — replace the estimated latency, token, and cost baselines with measurements | 10 | **Done (2026-08-13).** `scripts/measure_jobs.py`; 20 sequential real jobs, 16 approved. gl §14 and §14 above now carry a measured column; gl §13's token and cost rows are measured. Ran under the NIM development overrides, **not** the documented defaults — gl §14 "Measurement context". Produced [ADR 0001](adr/0001-supervisor-llm-routing-is-advisory.md), PDF support, Planner query reconstruction, and minted claim ids — each from a real failure |

**Rate limiting is not deferred to step 21.** The shared Redis bucket needs Redis, which Phase 1 does
not run — but the **per-job call budget, the 429 backoff, and the batched fact-check** all live in
steps 6 and 8 because they are load-bearing on a 40 RPM tier from the first real job (gl §13). This is
the one place the suggested ordering had to change.

### Phase 2 — persistence, the gate, and the API

| # | Step | Depends on | Why here |
|---|---|---|---|
| 13 | **Database layer** — schema, Alembic migrations, queries for jobs / findings / claims / claim_sources / audit | 4, 12 | Schema follows the schemas; writing it earlier means migrating it twice |
| 14 | **Postgres checkpointer** replaces the in-memory one; `setup()` owns its own tables | 13 | The gate is not resumable without it |
| 15 | **Persistence integration** — findings, claims, `claim_sources`, and audit events written as the graph runs | 13, 10 | The audit trail must be written *during* the job, not reconstructed after |
| 16 | **Export gate + export node** — the claim-to-URL check, the write to `jobs.report_json`, and the test that an uncited claim blocks it | 15 | The project's first invariant. It gates everything after it. The S3 write and its bounded retry join this same node in Phase 3 (§8) |
| 17 | **Human gate** — `interrupt()` before export, the reviewer payload with problems first, approve / reject / edit | 14, 16 | Needs a resumable checkpoint and an export node to gate |
| 18 | **FastAPI routes + API-key auth on every route** — all five endpoints, two roles, the one error shape | 13, 17 | Auth ships with the routes; the approval endpoint is an authorization decision |
| 19 | **Phase 2 test set** — every item in gl §18's "must have a test before Phase 2 ships" list | 11, 16, 17, 18 | It is a shipping condition, not a follow-up |

### Phase 3 — async, Redis, containers, CI

| # | Step | Depends on | Why here |
|---|---|---|---|
| 20 | **SQS worker** — pointer message, idempotency key unique in `jobs`, visibility timeout, DLQ, graceful shutdown | 18 | Needs the job row and the checkpoint to resume against |
| 21 | **Redis** — shared rate limiter, URL dedupe set, search and fetch caches, with hit/miss logging | 7, 20 | The shared bucket only matters once more than one process makes LLM calls |
| 22 | **Docker Compose** — Postgres 16, Redis 7, LocalStack for SQS and S3; one image, two entrypoints | 20, 21 | The first point at which the full local shape exists |
| 23 | **CI** — ruff, mypy, pytest, gitleaks, image build to ECR | 19, 22 | Every later step ships through it |

### Phase 4 — observability and evaluation

| # | Step | Depends on | Why here |
|---|---|---|---|
| 24 | **LangSmith tracing** — one trace per job, `job_id` / `agent` / `model` / `revision` on every run, `node:reflection` on the reflection node | 22 | Needs full runs to be worth tracing |
| 25 | **Structured JSON logging** for the CloudWatch layer, with the two-layer rule enforced | 22 | Kept separate from step 24 on purpose |
| 26 | **Eval dataset** — 30–50 questions across comparison, event tracking, and threat analysis, with expected evidence | 24 | Trace linkage in both directions is part of the deliverable |
| 27 | **Rubric calibration** — score 20 reports by hand, fix any dimension where judge and human differ by more than a point | 26 | Until this passes, the reflection gate is decoration |
| 28 | **Eval as a release gate in CI** — path-filtered, no dimension may drop more than 0.3 | 23, 26, 27 | Enforcement, not intention |

### Phase 5 — AWS

| # | Step | Depends on | Why here |
|---|---|---|---|
| 29 | **AWS deployment** — ECS Fargate (API + worker services), RDS, ElastiCache, real SQS + S3, API Gateway, ECR, per-service IAM roles | 23, 28 | Nothing deploys that has not passed CI and the eval gate |
| 30 | **CloudWatch alarms** — queue depth, DLQ, task restarts, error rate | 25, 29 | Alarms need real metrics to threshold against |
| 31 | **Cognito JWT** — replaces API keys in one route-layer dependency | 18, 29 | Everything downstream already reads a `user_id` and a role |
| 32 | **Retention and gate-expiry sweep** — one job, four retention rules, every deletion an audit row | 13, 29 | Needs data old enough to sweep |

---

## 22. Unresolved Questions

Ten questions were raised at architecture review, and **all ten are now decided** — nine at the
review itself, and the tenth (where Phase 2 export writes) before implementation began. They are
recorded in §20 and applied in the sections they affect; where a decision made a statement in
`CLAUDE.md` or `docs/engineering-guidelines.md` untrue, that statement was corrected rather than left
to drift.

**No open question blocks implementation.** One item remains open — exposed by the export-failure
decision, and needed before Phase 3 — with a second low-stakes reading marked as such. **Nothing has
been silently resolved.**

### Decided at architecture review

| Was | Question | Decision | Now documented in |
|---|---|---|---|
| A | Stale report after a reflection-driven re-research | Reflection sets `report = None` and returns targeted subtopics to `pending`; new findings always re-enter through the Synthesizer and the Fact-Checker | §3, §5, §6 · gl §6 |
| B | Three fields `ResearchState` lacked | `quality_flag`, `reviewer_edit_text`, `failed_dimensions` added. Retry **scope** deliberately not added — `subtopic_status` already carries it | §5 · gl §4 |
| C | Reflection scoring failure | Keep the report, `quality_flag="unscored"`, audit the failure, route to the gate. **`unscored` ≠ passed** | §3, §6, §15 · gl §6 |
| D | Redis unavailability | Caches and dedupe **fail open**; the shared rate limiter **fails closed** | §8, §15 · gl §11, gl §17 |
| E | S3 write failure at export | Bounded retry (10s, 2 retries at 2s/8s), then terminal `export_write_failed`. Report and audit trail preserved; research and synthesis never re-run | §3, §8, §15 · gl §9, gl §17 |
| G | Is `/health` authenticated? | **No.** It is the single unauthenticated route, with a minimal body and an explicit list of what it must never contain | §10, §13 · gl §12, gl §16, gl §18 |
| H | "Revision" meant two things | A revision is one **automatic improvement cycle after the initial report**. `revision_count` is 0-based, the cap check is `>=`, and budget tables now say "per pass" | §3 · CLAUDE.md, gl §2.4, gl §2.5, gl §6 |
| I | "sums to roughly 44" | Corrected at review to 41/44 — and **corrected again on 2026-08-13**, because 44 itself undercounted the Researcher during revisions and mixed calls with requests. The caps sum to **79**; 60 binds | §16 · CLAUDE.md, gl §13 |
| J | Idempotency key missing from the schema | Added as `UNIQUE NOT NULL` on `jobs`, derived server-side, with the duplicate-request and worker-retry interaction spelled out | §9, §11 · gl §9 |

### Decided before implementation

| Was | Question | Decision | Now documented in |
|---|---|---|---|
| F | Where does Phase 2 export write, when S3 arrives in Phase 3? | Phase 2 ships the export **gate** and stores the approved body in `jobs.report_json`; `GET /jobs/{id}` serves it via the `report?` field it already carries, and `GET /jobs/{id}/report` answers `404 not exported` until Phase 3. **No S3 stand-in, abstraction, or extra storage service is built** | §8, §9, §10, §20 row 36, §21 step 16 · gl §9, gl §12 |

This was the one decision blocking implementation step 16. It no longer blocks anything.

### Still open

#### 1. How is a job recovered after `export_write_failed`?

Newly exposed by the S3 decision. The job ends with a finished, approved, gate-passed report and no
artifact. Re-running the graph would re-bill research and synthesis for a storage error, which the
decision explicitly rules out — so recovery has to be a **re-export of existing work**.

**gl §12's API surface has no route for that, and none has been invented here.** The options are an
operational script, a new authenticated route, or accepting that a failed export is re-submitted as a
new job. Each is a different answer about who is allowed to trigger a re-export, which makes it an
authorization question as much as an operational one (gl §16).

*Not blocking:* the failure path itself is fully specified and safe — bounded, loud, and
non-destructive. Only the recovery ergonomics are undecided. **Needed before Phase 3 ships S3**, which
is also when `export_write_failed` first becomes reachable (§8).

#### 2. Low stakes — may reflection start a cycle on the reviewer-`edit` path?

`fact_checker → reflection` is a fixed edge, so an edited draft is scored. gl §10 says the edit goes
"to the Synthesizer for one pass, **then back to the gate**", which reads as: score it, show the
reviewer, do not start an automatic cycle.

**This document implements that reading** (§12, marked `[derived]`) — letting an edit trigger
open-ended automatic rework would contradict "one pass, then back to the gate". Recorded here because
an implementer could reasonably read it the other way, and the two behave differently for a reviewer
who edits a job that has revisions left.

**Note on what the code does today:** the reflection node applies its normal rules on every pass, so
an edited draft that scores badly with revisions left would start a cycle rather than returning to the
gate. Nothing distinguishes the edit path there yet, because the field that would mark it —
`reviewer_edit_text` — is not read by any node (§12). Both are the same piece of Phase 2 work, and
this question has to be answered before that work lands, not after.

#### 3. Deferred, not decided: how the reviewer's edit reaches the Synthesizer

`reviewer_edit_text` is set by the gate and consumed by nobody (§12). The contract in §5 says the
Synthesizer applies it and clears it in the same pass; the code does neither. This is deferred rather
than open — the *design* is settled — but it is listed here so it is not mistaken for finished work.
It ships with §21 step 17, alongside the endpoint that produces the text.

---

## Where to look next

| Question | File |
|---|---|
| What is this project, and what must never break? | `../CLAUDE.md` |
| How is each part built, and why? | `engineering-guidelines.md` |
| Why was a decision made this way? | §20 above, and `docs/adr/` once ADRs exist |
