# CLAUDE.md — Multi-Agent Competitive Research Assistant

## What this is

A competitive research system. You give it a question — *"compare TCS and Infosys on cloud
strategy"* — and five agents plan the research, search the web, write a report, and check every
claim against its sources. A human approves the report before it is exported. Every claim in the
exported report traces back to a URL.

> **Status: Phases 1 and 2 are complete, and Phase 3 is three stages in.** Phase 1 core on
> 2026-08-13 and its production-hardening pass on 2026-08-15; **Phase 2 on 2026-08-16**; Phase 3
> stages 1, 2 and 3 on **2026-08-17** — the local infrastructure, the queue and the worker, and
> Redis. Steps 22 (the application image and S3) and 23 (CI) are not built.
>
> **Phase 1** put the graph on its feet locally, end to end, in memory — five agents, the reflection
> node, the tool boundary, the LLM client, in-memory checkpointing, and a test suite that makes no
> network calls.
>
> **The hardening pass is closed. Four changes, each implemented, tested offline, and verified against
> real jobs:**
>
> | Change | Verified by |
> |---|---|
> | [ADR 0002](docs/adr/0002-concurrent-page-extraction-in-the-researcher.md) — concurrent page extraction in the Researcher | A controlled A/B/A on one subtopic (141.9s → 43.7s → 111.1s), then the n=20 run: Researcher share **45.2% → 33.1%** |
> | [ADR 0003](docs/adr/0003-finding-ids-are-a-per-job-sequence.md) — finding ids are a per-job sequence | `measure-04` re-run to `approved`, then **zero `report_cites_unknown_findings` across all 20 jobs** of 2026-08-14 (585 claims over 969 findings) |
> | [ADR 0004](docs/adr/0004-no-op-researcher-retries-after-evidence-exhaustion.md) — reflection does not retry an exhausted subtopic | n=20: 8 re-research decisions naming 12 targets, **0 of them `unresearched`**; substitution fired 4 times |
> | Shared `LLMClient` / `wrap_openai` lifecycle fix | `tests/test_measure_jobs.py` offline, then **0 nested `llm` spans across 590 leaf spans** in the n=20 traces |
>
> **Two n=20 runs against the real endpoint and the real web, both reaching 16 `approved`**: the
> **reference baseline of 2026-08-12/13**, and the **post-hardening run of 2026-08-14**, the same 20
> questions re-run with the four changes above in place. The second **supplements** the first rather
> than replacing it, and the two are never merged into one number. Latency, calls, node shares,
> revisions and cache come from the post-hardening run; its tokens were **recovered from LangSmith**
> (the per-job rows carry none) and published only after a reconciliation that matched 544 successful
> calls to 544 token-bearing spans. Both runs are maintained in `docs/engineering-guidelines.md`
> §13–§14, and both are measurements rather than estimates.
>
> **Phase 2 closed on 2026-08-16**, its seven steps verified against the repository by a completion
> audit rather than declared from a green suite. What it added:
>
> | Capability | Steps · record |
> |---|---|
> | Five application tables — `jobs`, `findings`, `claims`, `claim_sources`, `audit_events` — with an Alembic migration, and a test that fails if the migration and `database/schema.py` ever drift | 13 |
> | The **Postgres checkpointer**, so a job survives a restart and an approval two days later costs only the export | 14 |
> | The audit trail written **while the job runs**, one transaction per node event, keyed so a replayed node converges, loud on failure | 15 · [ADR 0005](docs/adr/0005-graph-time-persistence-semantics.md) |
> | The export gate's durable answer — the approved body in `jobs.report_json`, `exported_at` stamped in the same transaction | 16 |
> | The **reviewer-edit path**: one Synthesizer pass over existing evidence, back to the gate, never research, bounded at 3 edits and refused when the live budget cannot fund it | 17 · [ADR 0006](docs/adr/0006-reviewer-edit-returns-to-the-human-gate.md) |
> | **Five FastAPI routes with API-key authentication** on all but `/health`, two roles, one error envelope, and server-derived idempotency on `POST /jobs` | 18 |
> | **One decision per gate visit** — keyed `(job_id, calls_used)`, a same-decision retry that continues the graph, `409 gate_already_decided` for a different one, and `jobs.status` reconciled from the checkpoint even when a resume dies | 18 · [ADR 0007](docs/adr/0007-reviewer-decision-idempotency-and-gate-resume-failure.md) |
> | The Phase 2 test set — every item on `docs/engineering-guidelines.md` §18's shipping list, still with **no network calls** | 19 |
>
> **Invariant 7 is now satisfied rather than promised:** the gate is authenticated, and every decision
> carries the `user_id` its API key maps to.
>
> **Both durable stores stay injected and optional.** With neither, the graph behaves exactly as Phase
> 1 did — which is what the offline suite and `scripts/measure_jobs.py` still run on.
>
> **Phase 3 is under way, and three of its stages are built.**
>
> **Stage 1 — the local infrastructure and the real-PostgreSQL verification** (2026-08-17).
> `docker-compose.yml` starts PostgreSQL 16, Redis 7, and LocalStack for SQS and S3, and the
> `postgres`-marked tests run the migration, every Phase 2 gate statement, two reviewers claiming one
> gate at the same instant, and `PostgresSaver` across a real process restart.
>
> **Stage 2 — the queue and the worker** (2026-08-17). A job now runs on its own:
>
> | Capability | Record |
> |---|---|
> | `jobqueue.py`, the one place that talks to SQS: a **pointer message of three identifiers**, FIFO with `MessageGroupId = job_id`, and a deduplication id that is the job's idempotency key or ADR 0007's gate-visit key | [ADR 0010](docs/adr/0010-job-dispatch-and-status-across-api-queue-and-worker.md) decisions 3 and 4 |
> | `jobs.status` gains **`queued`**, written by `POST /jobs`; the worker is the only thing that moves it to `running`. Migration `rev_0002` widens the CHECK, verified against real PostgreSQL 16 | decisions 1 and 2 |
> | `worker.py` — `python -m worker`. It discriminates **start, resume and continue from the checkpoint** rather than from the message, bounds one invocation by `MAX_JOB_RUNTIME`, deletes a message on exactly three outcomes, and finishes the message it is holding on SIGTERM | decisions 5, 6, 7, 9 |
> | **The API no longer resumes the graph.** A gate decision records, claims, enqueues, and answers `200 {status: "running"}`; the worker reads the decision out of `audit_events` keyed by the visit, so the reviewer's own words never reach the queue | [ADR 0011](docs/adr/0011-the-human-gate-resume-moves-to-the-worker.md) |
> | **The API constructs no graph and no `LLMClient`**, and starts with no LLM or Tavily credential in its environment at all | [ADR 0012](docs/adr/0012-the-api-stops-holding-a-compiled-graph.md) |
> | A third test layer, marked `integration`: real LocalStack SQS, real FIFO groups, real deduplication, real redelivery, and a real dead-letter queue | `tests/test_queue_localstack.py` |
>
> **Stage 3 — Redis** (2026-08-17, step 21). The last of the four stores is wired:
>
> | Capability | Failure policy | Record |
> |---|---|---|
> | `cache:search:{hash}` and `cache:fetch:{hash}`, 24h, keyed by argument hash | **Fail open** — a miss costs one call, bounded by `MAX_LLM_CALLS_PER_JOB` | guidelines §7, §11 |
> | `job:{id}:urls`, 6h — the per-job URL set that survives a process, so a redelivered message does not re-fetch what a dead invocation already read | **Fail open** — one wasted fetch and a duplicate finding | guidelines §7, §11 |
> | `ratelimit:llm` — one sliding 60s window across **every** worker, taken before every request attempt | **FAIL CLOSED** — no token, no LLM call; the node fails with `rate_limiter_unavailable` after guidelines §17's two retries | guidelines §11, §17, ARCHITECTURE.md §20 row 29 |
> | `/health` reports `checks.redis`, and a deployment that cannot reach Redis is `degraded` | — | guidelines §12 |
>
> `redisstore.py` is the one place that talks to Redis, the way `jobqueue.py` is the one place
> that talks to SQS. The limiter is one Lua script rather than three commands, because the race
> it closes is between *processes*: a read-then-write across two round trips is exactly how two
> workers each politely limiting themselves to 40 requests per minute produce 80.
>
> **Nothing consumes S3 yet**, and there is no application image, no CI, no AWS, and no eval
> set. Steps 22 and 23 stay open.
>
> **Two implementation defects were found by the Stage 2 tests and fixed, both in `worker.py`.** A
> redelivered start message for a job waiting at the gate wrote `running` over `awaiting_approval` and
> never put it back, which left the gate unanswerable — ADR 0007 invariant 4 broken in the one
> direction nothing recovers from. And ADR 0010 decisions 7 and 9 describe finalising a job as
> `update_state` then `invoke(None)`; measured, that runs one more node on the timeout path and
> reaches `finalize` on neither, so the worker writes the terminal row itself. Both are recorded in
> the code where the correction lives.
>
> That closes the first of four Phase-2-adjacent items which were **deliberately deferred and
> recorded** rather than forgotten. Three remain: gate expiry (Phase 5 sweep), Secrets Manager
> (Phase 5), and a failed job's durable `failure_reason`
> ([ADR 0008](docs/adr/0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md): it lives in
> the checkpoint until Phase 3 gives it a row).
> **Tracing is the one partial exception** — see the stack table below.
>
> **Neither baseline is a production-default benchmark.** Both runs used the NIM development overrides
> in `.env` — `MAX_REVISIONS=3`, `MAX_SUPERVISOR_HOPS=30`, `LLM_MAIN_TIMEOUT_S=180`,
> `MAX_JOB_RUNTIME=1800` — not the defaults in the environment table below, which are unchanged and
> remain the production values. Which of those overrides could have moved a measured number, and which
> could not: `docs/engineering-guidelines.md` §14 "Measurement context". The 2026-08-14 run
> additionally ran through a **local DNS outage that cost three of its twenty jobs**, and both its
> all-jobs p95 and its all-jobs token p50 land on failed jobs — §14 carries every caveat, and the
> approved-only figures next to them. **Cost is derived throughout, never provider spend:** actual NIM
> development spend is $0. The next useful measurement is a **production-default** run at the
> documented defaults, which answers a different question and is not another re-baseline of these two.
>
> Every section below marks what is **built** versus **planned**. If you are reading this and the two
> disagree, the code wins and this file must be corrected — a doc that claims working code is worse
> than no doc.

Global engineering rules live in `~/.claude/CLAUDE.md`. This file holds the facts about *this*
project. Detailed standards live in `docs/engineering-guidelines.md`.

> **Two documents referenced below are not published in this repository.**
> `docs/engineering-guidelines.md` and `docs/interview-prep.md` are gitignored local working
> documents — they carry personal interview-preparation material alongside the engineering
> standards. Every reference to them here resolves on a local checkout and nowhere else, which is
> why they are named in plain text rather than linked.

---

## The five agents

Five agents, each with one job. Reflection is a separate evaluation and routing node in the graph,
covered in the next section.

| Agent | Responsibility | Input | Output schema | Tools | Call budget | On failure |
|---|---|---|---|---|---|---|
| **Supervisor** | Names the next agent from state. The route is computed in code; the LLM proposal is **advisory** ([ADR 0001](docs/adr/0001-supervisor-llm-routing-is-advisory.md)) | `ResearchState` | `SupervisorDecision{next, reason}` | none | 1 per hop, max 24 hops | Advisory failure or disagreement → logged, routing continues on state. `rate_limited` / `budget_exceeded` still fail the job |
| **Planner** | Breaks the question into 3–5 researchable subtopics | question | `ResearchPlan{subtopics[], success_criteria}` | none | 2 (1 + 1 retry) | Empty plan → fail the job; do not research an unplanned question |
| **Researcher** | Searches and extracts findings for one subtopic. Sources are chosen and fetched one at a time; the extraction calls then run concurrently ([ADR 0002](docs/adr/0002-concurrent-page-extraction-in-the-researcher.md)) | one subtopic | `Finding[]{claim, evidence, url, retrieved_at, content_hash}` | web search, page fetch | 3 per subtopic, max 5 subtopics | Zero findings after retries → mark subtopic `unresearched`, continue, report the gap |
| **Synthesizer** | Writes the report from findings only | `Finding[]` | `Report{sections[], claims[], sources[]}` | none | 2 per pass, max 3 passes | Empty `sources` → hard failure, never an unsourced report |
| **Fact-Checker** | Verifies each claim against its cited source text | `Report`, `Finding[]` | `Verdict[]{claim_id, supported, quote, note}` | page fetch (re-fetch only) | 1 batched call per pass (+1 retry) | Unreachable source → `supported=false`, never a guess |

Per-job ceiling: **60 LLM calls** (`MAX_LLM_CALLS_PER_JOB`). Exceeding it fails the job loudly.

**The per-component caps do not sum below 60 — they sum to 79** for the automatic workflow (5
subtopics researched three times over, 24 hops, 3 report-producing passes, no reviewer edits). So
**60 is the binding guard, not headroom above a worst case** — it is the one that catches "everything
else". Measured max was 44 on the 2026-08-13 reference run and **53** on the 2026-08-14
post-hardening run — a call is an attempt, so a retried transport failure spends budget too.

Reviewer edits add 3 calls each and are **not bounded yet**: the planned `MAX_REVIEWER_EDITS` = 3
gives 88, and 91 is only what today's hop margin would permit at 4 edits — an artefact, not a design
target. Full derivation, the three cases, and the correction of the earlier 41/44 figure:
`docs/engineering-guidelines.md` §13.

### What each agent must never do

- The **Supervisor** never routes based on text fetched from the web. It reads state, nothing else —
  and since ADR 0001 the route is `allowed_target(state)` in plain Python, so the LLM has no
  authority over control flow at all.
- The **Researcher** never writes prose for the report. It produces findings.
- The **Synthesizer** never introduces a fact that is not in a finding.
- The **Fact-Checker** never infers. It quotes the source or marks the claim unsupported.

---

## Reflection

**Reflection is an LLM-powered evaluation and routing node, not an independent agent.** It evaluates
the current report against the reflection rubric, identifies failed dimensions, and routes the graph
to the appropriate specialist for a bounded retry.

It has no tools, no persona, and no goal of its own, and it performs no research. It is LangGraph
control flow that happens to use an LLM to score, which is why it is not one of the five agents.

The important design point: **route back by which dimension failed**, not a blind rerun.

| Failing dimension | Route back to |
|---|---|
| Thin or missing coverage | Researcher, for the specific subtopic — unless that subtopic is already `unresearched` ([ADR 0004](docs/adr/0004-no-op-researcher-retries-after-evidence-exhaustion.md)) |
| Weak structure or writing | Synthesizer |
| Unverified or unsupported claims | Fact-Checker |

**A subtopic already marked `unresearched` is not a Researcher target — the retry is aimed at an
eligible subtopic instead.** It would otherwise be re-researched from the same planned query and the
same cached search results, with no unread source to reach. **The route itself usually still fires:**
while one eligible subtopic remains it is the fallback target, so the guard changes *which* subtopic
is retried far more often than *whether* the retry happens. Only when **every** subtopic is
`unresearched` is there no target at all — then reflection acts on the next failing dimension, or
reaches the human gate with `quality_flag="below_threshold"`. Either way the evidence gap travels to
the reviewer, and is never turned into a pass. **This is a revision-budget heuristic, not a proof:**
extraction is a fresh LLM call, so such a retry can still find something — 2 of 6 measured ones did —
and [ADR 0004](docs/adr/0004-no-op-researcher-retries-after-evidence-exhaustion.md) records the trade,
with its 2026-08-15 correction recording how rare the no-target case is: 0 of 20 measured jobs.

It scores five dimensions, 1–5: **Research completeness · Source correctness · Citation coverage ·
Factual consistency · Report quality.** These are the same five dimensions the offline evaluation
uses, deliberately — one vocabulary, used inline as a gate and offline as a measurement.

A **revision** is one automatic improvement cycle triggered by reflection *after* the initial report.
Bounded by `MAX_REVISIONS` (default 2, so two improvement cycles and at most 3 passes). A reviewer
`edit` is not a revision. Hitting the cap is invariant 2 below — a visible outcome, never a silent
pass. A cycle that could not change anything is not started at all, so a job can reach the gate with
cycles unspent — though that needs every subtopic exhausted, and no measured job has hit it. A
Researcher route invalidates the draft, so new findings always re-enter through the Synthesizer.

Full rubric, weights, thresholds, and what exactly happens at the cap:
`docs/engineering-guidelines.md` §6.

---

## Graph shape

```mermaid
flowchart TD
    START(["POST /jobs"]) --> SUP{"Supervisor<br/>routes on state"}

    SUP -->|"no plan yet"| PLAN["Planner<br/>3-5 subtopics"]
    SUP -->|"subtopic pending"| RES["Researcher<br/>search + extract findings"]
    SUP -->|"findings ready"| SYN["Synthesizer<br/>draft report"]
    SUP -->|"draft ready"| FC["Fact-Checker<br/>verify claims vs sources"]

    PLAN --> SUP
    RES --> SUP
    SYN --> SUP
    FC --> REFL{"Reflection node<br/>evaluate 5 dimensions"}

    REFL -->|"coverage weak"| RES
    REFL -->|"writing weak"| SYN
    REFL -->|"claims unverified"| FC
    REFL -->|"pass, revision cap hit,<br/>or every subtopic exhausted"| GATE["Human gate<br/>interrupt - awaits approval"]

    GATE -->|"approve"| EXPORT["Export gate<br/>every claim has a source URL?"]
    GATE -->|"reject"| REJECTED(["job closed, not exported"])
    GATE -->|"edit"| SYN

    EXPORT -->|"yes"| DONE(["report stored"])
    EXPORT -->|"no"| FAILED(["export fails - loudly"])
```

Diamonds route; boxes do the work. The Supervisor is both an agent and the main router. The
reflection node is a router only — it evaluates and redirects.

---

## Stack

A row that cannot name the requirement it serves gets deleted.

| Layer | Technology | Why |
|---|---|---|
| LLM | Production LLM API; NVIDIA NIM in development | Reasoning |
| Agent framework | LangGraph | Stateful orchestration with checkpointing |
| Tool protocol | MCP | External research tools behind one interface |
| Web search | Tavily, behind the tool boundary | Returns cleaned page content **and** the URL — the audit trail needs both |
| API | FastAPI | Backend |
| Async jobs | SQS, FIFO with `MessageGroupId = job_id` | Research runs for minutes; a request cannot hold the connection. FIFO is load-bearing rather than a preference: it is what keeps one job to one writer ([ADR 0010](docs/adr/0010-job-dispatch-and-status-across-api-queue-and-worker.md) decision 4) |
| Short-term state | Redis / ElastiCache | Cache, URL dedupe, and the **shared** rate limiter — the one that has to be shared, because two workers limiting themselves separately do not limit the tier |
| Persistent data | PostgreSQL / RDS | Facts and the audit trail |
| Artifacts | S3 | Exported reports |
| Containers | Docker + ECR | Packaging |
| Compute | ECS Fargate | Deployment |
| API entry | API Gateway | Exposure and rate limiting |
| AI observability | LangSmith | Agent and LLM tracing. **Partially built** — see below |
| Infra observability | CloudWatch | AWS logs, metrics, alarms |
| Evaluation | LangSmith Evaluation | Research quality |
| Testing | pytest | Application tests |
| Auth | API keys (Phase 2) → Cognito JWT (Phase 5) | The approval gate is an authorization decision |
| Migrations | Alembic | Tracks which migrations ran, and in what order |
| CI/CD | GitHub Actions | Enforces the eval gate that §15 asks for |

**Built so far:** the LLM row, the agent framework row, the web-search row, the testing row, the
**persistent-data row and the migrations row** (`database/`, steps 13–16, 2026-08-15), the **API row**
and the **auth row** (`routes/`, `app.py`, step 18, 2026-08-16), the **async-jobs row**
(`jobqueue.py`, `worker.py`, Phase 3 stage 2, 2026-08-17), the **short-term-state row**
(`redisstore.py`, Phase 3 stage 3, 2026-08-17), plus **part of the AI-observability row**, which is
the one entry that is neither fully built nor untouched. PostgreSQL now holds `jobs`, `findings`,
`claims`, `claim_sources`, and `audit_events`, and LangGraph's checkpointer owns its own tables
beside them; SQS holds one pointer message per job start and per reviewer decision; Redis holds two
caches, a per-job URL set and the shared rate limiter. **Every remaining row is Phase 3 or later and
has no code behind it yet** — artifacts, containers, compute, API entry, infra observability,
evaluation, and CI/CD.

**The async-jobs row is built against LocalStack, not against AWS.** `jobqueue.build_queue()` takes an
`endpoint_url` and that is the only difference between the two, which is what makes the
`integration`-marked tests worth running — but a real queue, a real IAM policy and a real DLQ alarm are
Phase 5's, and nothing here has met them.

**What "partially built" means for tracing, stated precisely, because the two halves ship in different
phases.**

| | Status |
|---|---|
| **Client and graph tracing** | **Built and verified.** `llm_client.py` applies `wrap_openai` when `LANGSMITH_TRACING` is on, LangGraph emits a run tree per job, and `thread_id` carries the job id. `config.py` validates the credentials, and `tests/test_config.py` and `tests/test_measure_jobs.py` cover it offline. The 2026-08-14 reconciliation read 590 leaf spans back out of the project |
| **The standardized metadata contract** | **Not built.** Nothing sets `job_id`, `agent`, `model`, or `revision` under those names — what a query gets is whatever LangGraph and `wrap_openai` happen to record (`docs/engineering-guidelines.md` §14 has the mapping). `revision_count` reaches no run at all |
| **Evaluation and the release gate** | **Not built.** No eval dataset, no calibrated rubric, no CI gate. Phase 4 |

So tracing is **usable for debugging and was load-bearing for the token reconciliation**, and it is
**not** the production observability contract Phase 4 specifies. Do not describe LangSmith as done, and
do not describe it as absent.

**The tool-protocol row is the one to read carefully.** `tools/` is one boundary with one place for
argument validation, timeouts, size limits, and the injection wrapper — which is the property MCP was
chosen for — but it is **in-process. There is no MCP client, no MCP server, and no protocol hop**, and
none is scheduled. Do not describe MCP as integrated (`docs/engineering-guidelines.md` §7).

**Not used:** vector memory (no measurement says it is needed) · DeepEval (deferred; see
`docs/engineering-guidelines.md` §15). Coordination between agents is the Supervisor's job and goes
through `ResearchState` — agents never message each other directly, so no inter-agent protocol is
needed.

---

## Model configuration

One OpenAI-compatible client. **No provider classes, no provider abstraction.** NVIDIA NIM is
OpenAI-compatible at `https://integrate.api.nvidia.com/v1` with tool calling and JSON mode, so the
same client covers development and production.

| Variable | Purpose |
|---|---|
| `LLM_BASE_URL` | Endpoint. NIM in development, the production API in production |
| `LLM_MODEL` | Main model — planning, research extraction, writing, fact-checking |
| `LLM_FAST_MODEL` | Cheap model — Supervisor routing and reflection scoring only |
| `LLM_API_KEY` | Credential for whichever endpoint is configured |

> **Swapping a model is a config change plus a preflight plus an eval run — never a code change.**

**Development rate limit:** the NIM free tier allows roughly **40 requests per minute**, with
per-model ceilings NVIDIA does not publish. One report plausibly fires 40–80 calls. So a shared rate
limiter, batched fact-checking, a per-job call budget, and 429 backoff are load-bearing
requirements, not polish. Details in `docs/engineering-guidelines.md` §13.

**A job now holds more than one request open.** Since
[ADR 0002](docs/adr/0002-concurrent-page-extraction-in-the-researcher.md) a subtopic's page
extractions run concurrently, so a job can have up to `RESEARCHER_CONCURRENCY` (3) requests in
flight. Everything else stays sequential *within* a job — nodes run one at a time, one subtopic per
Researcher visit — and one worker takes one message at a time.

**What Phase 3 stage 2 changed is that "one job at a time" is no longer a property of the system.**
FIFO message groups guarantee one *worker* per job, not one job per deployment: run two workers and
two jobs run at once, each with up to three requests in flight.

**Stage 3 is what makes that safe.** The shared `ratelimit:llm` bucket is one sliding 60s window
across every worker, and a token is taken before **every request attempt** — retries included,
because a retried request is a real request against the same tier. So `LLM_RPM_LIMIT` now bounds the
deployment rather than each process, and it fails closed: no token, no LLM call.

---

## Repository layout

Flat. No directory exists to hold one small file. A directory arrives with the phase that needs it.

```text
config.py        env vars → one validated Config. Built
schemas.py       every boundary type: plan, finding, report, verdict, score, tool results. Built
llm_client.py    the one OpenAI-compatible client — structured output, retries, 429, call budget,
                 and `wrap_openai` for LangSmith when tracing is on. Built
agents/          one module per agent — supervisor, planner, researcher, synthesizer, fact_checker. Built
graph/           state.py, reflection.py, build.py — the nine nodes and the checkpointer. Built
tools/           search (Tavily), fetch, argument validation, the untrusted-content wrapper,
                 the failure vocabulary and cache interfaces. Built — in-process, not MCP
scripts/         check_model.py the preflight; measure_jobs.py the real-job measurement
                 harness. Both built
tests/           pytest suite, plus harness.py — FakeLLM and the recorded web — dbharness.py and
                 pgharness.py for the two database layers, and fakes.py, whose FakeQueue is the
                 offline stand-in for SQS. Built
docs/            ARCHITECTURE.md, adr/. Built. engineering-guidelines.md and interview-prep.md
                 exist locally but are gitignored — not published in this repository

database/        schema.py the five tables, queries.py the statements, migrations/ Alembic.
                 Built (steps 13-16). The checkpointer is in graph/build.py, next to the
                 in-memory one it replaces
app.py           the API entrypoint - `uvicorn app:app`. Built (step 18). Since ADR 0012 it
                 builds no graph and no LLM client: a config, an engine, a checkpoint
                 *reader*, a queue, and the key table
routes/          api.py the six endpoints, auth.py the API keys and the two roles.
                 Built (step 18; the gate view added by ADR 0013)

jobqueue.py      the one place that talks to SQS — the pointer message, the FIFO attributes,
                 send, receive, delete, and the queue's own attributes. Built (Phase 3 stage 2).
                 Nothing else imports boto3
redisstore.py    the one place that talks to Redis — the two caches, the per-job URL set, and
                 the shared rate limiter. Built (Phase 3 stage 3). Two failure policies in one
                 file: the caches and the URL set fail open, the limiter fails closed. Nothing
                 else imports redis
worker.py        the job runner - `python -m worker`. Long-polls, discriminates start from
                 resume from continue using the checkpoint, invokes the graph, and
                 acknowledges. Built (Phase 3 stage 2). The only process that builds an
                 LLMClient or executes a node

docker-compose.yml   Postgres 16, Redis 7, LocalStack for SQS and S3. Built (Phase 3 stage 1).
                 Infrastructure only — there is no application image, and step 22 is not closed
docker/          the two bootstrap scripts Compose mounts: the test database, and the queue,
                 its DLQ and the bucket. Built (Phase 3 stage 1)

eval/            dataset, evaluators, run script — Phase 4
observability/   LangSmith tracing setup, structured logging — Phase 4
.github/         workflows — lint, types, tests, image build, conditional eval gate — Phase 3
```

---

## Commands

These run today.

```bash
pytest
```

```bash
python scripts/check_model.py
```

```bash
python scripts/measure_jobs.py --summary
```

```bash
ruff check . && ruff format --check . && mypy --strict .
```

```bash
alembic upgrade head
```

`pytest` is the whole suite, and it makes **no network calls** — the LLM, Tavily, and DNS are all
replaced by the test harness, so it needs no credentials and no running service. **That includes the
database tests:** they run the real migration and the real statements against a temporary SQLite file,
which proves the columns, keys, foreign keys, CHECK constraints, and indexes, and proves nothing
PostgreSQL-specific.

**Since Phase 3 stage 1 there is a second layer that does.** The tests marked `postgres` run the same
migration and the same statements against the real PostgreSQL 16 in `docker-compose.yml`, plus the
things only a server can decide: JSONB, `timestamptz`, the statement timeout, two reviewers claiming
one gate at the same instant, and `PostgresSaver` across a process restart. They **skip** unless
`TEST_DATABASE_URL` is set, which is what keeps plain `pytest` offline:

```bash
docker compose up -d --wait
```

```bash
TEST_DATABASE_URL=postgresql://research:research@localhost:5432/research_test pytest -m postgres
```

In PowerShell the variable is set first: `$env:TEST_DATABASE_URL = "..."` then `pytest -m postgres`.
`pytest -m "not postgres"` is the offline layer on its own, so a failure always says which of the two
broke. **Every case drops its schema before it runs**, which is why the harness refuses any URL whose
database name does not contain `test` — Compose creates `research_test` beside `research` for it.

**Since Phase 3 stage 2 there is a third layer, on the same terms.** The tests marked `integration`
run against the real LocalStack SQS: the queue Compose declares, throwaway FIFO queues of their own,
and the real worker driving a real message. They exist because four of
[ADR 0010](docs/adr/0010-job-dispatch-and-status-across-api-queue-and-worker.md)'s decisions are queue
*attributes* rather than application code — FIFO message groups, deduplication ids,
`ApproximateReceiveCount`, and the redrive policy — and a fake that models a guarantee is not the
guarantee. They **skip** unless `SQS_ENDPOINT_URL` is set, and they need no AWS credentials and reach
no AWS:

```bash
SQS_ENDPOINT_URL=http://localhost:4566 pytest -m integration
```

**And a fourth, for Redis** (Phase 3 stage 3), on identical terms. It proves what a broken client
cannot: that a TTL is really kept, and above all that **two clients share one bucket** — the
"two workers produce 80 requests per minute" failure is a statement about two connections against
one key, and no single-process fake can be wrong about it in the right way. It **skips** unless
`TEST_REDIS_URL` is set, and it **refuses database 0**, because every case flushes the database it
is given and 0 is what `REDIS_URL` defaults to:

```bash
TEST_REDIS_URL=redis://localhost:6379/15 pytest -m redis
```

`alembic upgrade head` applies the migrations in `database/migrations/`. It reads `DATABASE_URL` — the
ordinary libpq string, `postgresql://user:pw@host/db` — and nothing else, so a migration does not need
an LLM key. In deployment it runs as its own task and must exit 0 **before** the new service revision
starts (`docs/engineering-guidelines.md` §19).

`check_model.py` is the preflight: it confirms the configured endpoint answers, supports tool
calling and JSON mode, and reports the observed rate limit. It needs real `LLM_*` credentials. Run it
after any model change.

`measure_jobs.py` runs real jobs against the real endpoint and the real web, and is the only thing
here that does. `--summary` rebuilds the published baseline from `measurements/jobs.jsonl` and makes
no network calls. Jobs run one at a time — two concurrent jobs saturate the 40 RPM development tier
(`docs/engineering-guidelines.md` §13) — and each result is written the moment its job ends, so the
run is resumable and a failure is kept as data. **Per-request detail is in LangSmith, not in a file
here** — the `measurements/requests.jsonl` sink and the `CallRecord` machinery behind it were removed
once LangSmith became the LLM observability mechanism, so `prompt_tokens` and `completion_tokens` are
no longer recorded in a row. `measurements/` is gitignored: the rows carry fetched third-party page
text and report bodies, and only the summary table is published.

The lint, format, and type commands are what CI will run once CI exists in Phase 3.

```bash
uvicorn app:app --reload
```

`uvicorn app:app` serves the six routes in `routes/api.py`. It needs `DATABASE_URL`, `SQS_QUEUE_URL`
and `AUTH_KEYS` — and **no LLM or Tavily credential**, since
[ADR 0012](docs/adr/0012-the-api-stops-holding-a-compiled-graph.md) took the graph out of this
process. What it does with a submission is write the row and enqueue a pointer message; the worker
does the rest.

```bash
python -m worker
```

`python -m worker` is the process that runs jobs. It long-polls the queue, decides from the checkpoint
what each message means, invokes the graph, and deletes the message only once the work is durable. It
is the **only** process that constructs an `LLMClient` or executes a node, so it is the one that needs
the `LLM_*` and `TAVILY_API_KEY` credentials — plus `DATABASE_URL`, `SQS_QUEUE_URL` and `REDIS_URL`.
Run as many as you like: FIFO message groups keep one job to one worker, and since stage 3 the shared
rate limiter bounds the whole deployment rather than each process.

**It refuses to start when Redis does not answer**, and that is the fail-closed rule reaching
startup: the shared limiter is what stands between a worker and the LLM endpoint, so a worker without
one would take a message, fail its first node with `rate_limiter_unavailable`, leave the message, and
repeat that until the job dead-lettered. One log line beats three deliveries.

At startup it reads its queue's attributes and **refuses to run** against a queue that is not FIFO, or
whose visibility timeout does not exceed `MAX_JOB_RUNTIME + 3 × LLM_MAIN_TIMEOUT_S + 10`
([ADR 0010](docs/adr/0010-job-dispatch-and-status-across-api-queue-and-worker.md) decisions 4 and 8).
SIGTERM stops it taking new work and lets the invocation in flight finish, which is what leaves the
checkpoint and the database agreeing.

**One job's path across the two processes:**

```text
POST /jobs            -> jobs.status = queued, one FIFO message  (identifiers only)
worker receives       -> queued -> running, invoke(new_state(...))
gate node interrupts  -> awaiting_approval, message deleted
POST /approve         -> decision recorded, gate claimed, resume message enqueued, 200 {status: running}
worker receives       -> reads the decision from audit_events, invoke(Command(resume=...))
export + finalize     -> approved | rejected | failed, message deleted
```

**The response to a gate decision is `running`, not the outcome** ([ADR 0011](docs/adr/0011-the-human-gate-resume-moves-to-the-worker.md)
decision 5): the gate is answered and the work is queued. A caller that needs the outcome polls
`GET /jobs/{id}`, which is what `docs/engineering-guidelines.md` §12 already tells them to do.

**Delivery is at-least-once and nothing here pretends otherwise.** The worker deletes a message on
exactly three outcomes — the graph interrupted at the gate, the job reached a terminal status, or it
was already terminal when the message arrived — and on nothing else, so an unhandled failure leaves
the message and redelivery is the retry. The same node can therefore run twice, which is why every
graph-time write is keyed to converge ([ADR 0005](docs/adr/0005-graph-time-persistence-semantics.md)).
After three deliveries the message reaches the dead-letter queue; the worker's last delivery also ends
the job `failed` with `failure_reason="job_dead_lettered"`, and still leaves the message, so the job
stops being pollable **and** the DLQ alarm fires.

```bash
docker compose up -d --wait
```

`docker compose up -d --wait` starts the local infrastructure: **PostgreSQL 16, Redis 7, and
LocalStack for SQS and S3**. `--wait` is not decoration — each service has a healthcheck, and
LocalStack's checks for the queue and the bucket rather than for the process, so when the command
returns the resources exist. Startup is idempotent: the bootstrap scripts in `docker/` converge
rather than create, so running it again changes nothing.

**PostgreSQL, SQS and Redis have application code behind them; S3 does not.** The API enqueues to
the LocalStack queue and `python -m worker` consumes it (stage 2); the worker caches searches and
fetches in Redis, deduplicates URLs per job there, and takes every LLM token from the shared bucket
(stage 3). **S3 is still started only** — there is no `PutObject`, which is step 22.

To point both processes at the local queue:

```bash
export SQS_QUEUE_URL=http://localhost:4566/000000000000/research-jobs.fifo AWS_ENDPOINT_URL=http://localhost:4566
```

`AWS_ENDPOINT_URL` is the only difference between LocalStack and real AWS. boto3 still wants
credentials to sign with, and LocalStack ignores their values, so `AWS_ACCESS_KEY_ID=test` and
`AWS_SECRET_ACCESS_KEY=test` are enough.

There is **no application image**. Compose starts services only, so migrations run from the host:

```bash
DATABASE_URL=postgresql://research:research@localhost:5432/research alembic upgrade head
```

`docker compose down -v` resets everything, including the `research_test` database the integration
suite uses — the next `up` recreates it. If port 5432 is already taken on your machine, set
`POSTGRES_PORT` in `.env` (Compose reads it) and follow it in every URL above.

This does **not** run yet — there is no eval set:

```text
python -m eval.run          # Phase 4
```

---

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `LLM_BASE_URL` | LLM endpoint. **Worker-only** since [ADR 0012](docs/adr/0012-the-api-stops-holding-a-compiled-graph.md) — see the note under this table | *(required by the worker)* |
| `LLM_MODEL` | Main model id. Worker-only | *(required by the worker)* |
| `LLM_FAST_MODEL` | Routing and scoring model id. Worker-only | falls back to `LLM_MODEL` |
| `LLM_API_KEY` | LLM credential. Worker-only | *(required by the worker)* |
| `LLM_RPM_LIMIT` | The shared client-side rate limit, and since stage 3 it really is shared: one `ratelimit:llm` window in Redis across every worker, not a ceiling each process keeps to itself | `40` |
| `LLM_MAIN_TIMEOUT_S` | Request timeout for **every** main-tier caller — Planner, Researcher extraction, Synthesizer, Fact-Checker. There is no per-agent timeout. Raise only where the endpoint is slow — NIM development uses `180` | `60` |
| `TAVILY_API_KEY` | Web search credential. Worker-only | *(required by the worker)* |
| `DATABASE_URL` | PostgreSQL connection string | *(required from Phase 2)* |
| `REDIS_URL` | Redis connection string. **Worker-required since Phase 3 stage 3** — it holds the two caches, the per-job URL set, and the shared rate limiter, and the worker refuses to start when it does not answer. The API reads it only to report `checks.redis` on `/health` | `redis://localhost:6379/0` |
| `SQS_QUEUE_URL` | Job queue. **Required by both processes** since Phase 3 stage 2: the API enqueues, the worker receives. It must be FIFO and carry a redrive policy — the worker reads both at startup and refuses to run without them ([ADR 0010](docs/adr/0010-job-dispatch-and-status-across-api-queue-and-worker.md) decisions 4 and 8) | *(required from Phase 3)* |
| `S3_BUCKET` | Exported report storage | *(required from Phase 3)* |
| `AWS_REGION` | AWS region | `ap-south-1` |
| `AWS_ENDPOINT_URL` | Where the AWS APIs live when they are not AWS. Unset against real AWS; `http://localhost:4566` for the Compose LocalStack. The **only** difference between the two, which is what makes the `integration`-marked tests worth running | *(unset)* |
| `LANGSMITH_TRACING` | Enable tracing | `false` |
| `LANGSMITH_API_KEY` | LangSmith credential | *(required when tracing)* |
| `LANGSMITH_PROJECT` | Trace project name | `competitive-research` |
| `MAX_REVISIONS` | Reflection loop retry cap | `2` |
| `MAX_SUPERVISOR_HOPS` | Routing loop guard. 20 is the automatic workflow's derived maximum; a reviewer edit legitimately costs +1 hop, so the bound of 3 edits accepted in [ADR 0006](docs/adr/0006-reviewer-edit-returns-to-the-human-gate.md) puts the ceiling at 23. **24 stays**, as the ceiling plus one | `24` |
| `MAX_LLM_CALLS_PER_JOB` | Per-job call budget | `60` |
| `MAX_REVIEWER_EDITS` | How many times a reviewer may send one job back for an edit ([ADR 0006](docs/adr/0006-reviewer-edit-returns-to-the-human-gate.md)). Each edit costs 3 calls and 1 hop. Enforced at `POST /jobs/{id}/approve` **before the graph is resumed**, so a refused edit spends nothing; counted from the audit trail, so a retried decision cannot spend one ([ADR 0007](docs/adr/0007-reviewer-decision-idempotency-and-gate-resume-failure.md)) | `3` |
| `MAX_JOB_RUNTIME` | How long **one worker invocation** may run, seconds — not a job's lifetime, so a job that waits three days at the gate does not fail on resume ([ADR 0010](docs/adr/0010-job-dispatch-and-status-across-api-queue-and-worker.md) decision 7). **Enforced since Phase 3 stage 2**, checked between nodes; over it the job ends `failed` with `failure_reason="job_timeout"`. The `.env` override of `1800` was dropped by decision 8: the queue's visibility timeout has to exceed `MAX_JOB_RUNTIME + 3 × LLM_MAIN_TIMEOUT_S + 10`, so raising the job bound raises both sides. The development slack lives in the local queue's visibility timeout instead, and `tests/test_local_infrastructure.py` checks the arithmetic | `1200` |
| `REFLECTION_PASS_THRESHOLD` | Weighted score needed to pass | `3.5` |
| `MAX_FETCH_BYTES` | Response cap on a page fetch | `2097152` (2 MB) |
| `MAX_PAGE_CHARS` | Cleaned text kept per page, ≈6k tokens | `24000` |
| `RESEARCHER_CONCURRENCY` | How many of one subtopic's page extractions run at once ([ADR 0002](docs/adr/0002-concurrent-page-extraction-in-the-researcher.md)). Refused outside 1–3 at startup. `1` restores sequential extraction | `3` |
| `AUTH_KEYS_SECRET_ID` | Secrets Manager id holding hashed API keys and their roles. **Read from Phase 5**; the same payload comes from `AUTH_KEYS` before then | *(required from Phase 5)* |
| `AUTH_KEYS` | The hashed API keys themselves, as JSON: `{"<sha256 of the key>": {"user_id", "role"}}`. Secrets are environment variables locally and Secrets Manager in AWS (`docs/engineering-guidelines.md` §16) | *(required from Phase 2)* |
| `RETENTION_DAYS` | How long jobs, findings, and audit rows are kept | `365` |
| `APP_ENV` | `local` \| `dev` \| `prod` | `local` |
| `LOG_LEVEL` | Log verbosity | `INFO` |

**"Required" now depends on which process is asking, and that is
[ADR 0012](docs/adr/0012-the-api-stops-holding-a-compiled-graph.md) decision 4 rather than
laxity.** `load_config()` refuses nothing: it builds a `Config` whose optional fields are
`None`, and each entrypoint states what *it* cannot start without.

| Process | Refuses to start without |
|---|---|
| `uvicorn app:app` | `DATABASE_URL`, `SQS_QUEUE_URL`, `AUTH_KEYS` |
| `python -m worker` | those first two, plus `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `TAVILY_API_KEY` (`worker.required_credentials`) — **and a Redis that answers** (`worker.check_redis`), because the shared limiter fails closed |
| `scripts/check_model.py`, `scripts/measure_jobs.py` | the `LLM_*` set |

So **the API process starts, serves all six routes and passes its health check with no LLM or
Tavily credential in its environment** — which is what makes `docs/engineering-guidelines.md`
§13's least-privilege table a property of the code rather than of an intended deployment. It is
driven as a test rather than asserted here: `tests/test_api.py` builds the application from
`load_config({})` and exercises every route, and the smoke run of 2026-08-17 did the same
against real PostgreSQL and real LocalStack.

---

## Project invariants

These are the non-negotiables. If a change breaks one, the change is wrong.

1. **Every claim in an exported report traces to at least one source URL, or export fails.** Not a
   warning. The export does not happen.
2. **Revisions are bounded by `MAX_REVISIONS`.** Hitting the cap is a visible outcome carried in the
   response, never a silent pass.
3. **Every job has an LLM call budget.** Exceeding `MAX_LLM_CALLS_PER_JOB` fails the job.
4. **Fetched web content is untrusted data.** It may never reach a tool argument, and it may never
   reach the Supervisor — the Supervisor reads state, not page text. The reflection node is the one
   component that unavoidably reads report text derived from fetched pages, because scoring a report
   means reading it. Its exposure is **bounded and tested rather than eliminated**
   (`docs/engineering-guidelines.md` §8).
5. **`ResearchState` is the contract between agents.** Agents communicate through state, never by
   calling each other directly.
6. **No export before the human gate.** The gate is the backstop for everything the automated checks
   miss.
7. **The gate is authenticated, and every decision records who made it.** An approval with no
   identity behind it is not a backstop — it is a formality (`docs/engineering-guidelines.md` §16).
8. **There are exactly five agents, and reflection is not one of them.** It stays an LLM-powered
   evaluation and routing node: no tools, no research, no goal of its own. Giving it any of those
   changes the architecture, so it needs an ADR before it needs code
   (`docs/engineering-guidelines.md` §6).

---

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| 0 | Documentation — this file, engineering guidelines, architecture | **Done** |
| 1 | Local graph: 5 agents + the reflection node, tool boundary, LLM client, in-memory state, no persistence | **Done.** Core on **2026-08-13** — twelve steps, plus the human-gate and export nodes, the test suite, and the 20-job reference baseline. **Production-hardening pass on 2026-08-15** — ADR 0002, ADR 0003, ADR 0004, the shared-`LLMClient` fix, the post-hardening n=20 run of 2026-08-14, and the LangSmith token reconciliation. Both are in the status block above |
| 2 | Postgres checkpointer, Alembic migrations, audit tables, the gate's **API side** — `POST /jobs/{id}/approve`, the reviewer payload, expiry — FastAPI routes, **API-key auth on every route except `/health`** | **Done, 2026-08-16.** Steps 13–16 on **2026-08-15** ([ADR 0005](docs/adr/0005-graph-time-persistence-semantics.md)); steps 17–19 on **2026-08-16** — the reviewer-edit path ([ADR 0006](docs/adr/0006-reviewer-edit-returns-to-the-human-gate.md)), the five routes with API-key auth, gate-decision idempotency ([ADR 0007](docs/adr/0007-reviewer-decision-idempotency-and-gate-resume-failure.md)), and the §18 test set. Closed by a completion audit against the repository, which found and fixed two blockers: reviewer-text edge cleaning (ADR 0006 decision 8) and the undecided `failure_reason` ([ADR 0008](docs/adr/0008-a-failed-jobs-reason-lives-in-the-checkpoint-for-phase-2.md)). **Gate expiry is the one listed item deliberately not built** — it waits for the Phase 5 sweep |
| 3 | Docker Compose (Postgres 16, Redis 7, LocalStack for SQS + S3), async worker, **CI: lint, types, tests, image build** | **In progress. Stages 1 and 2 done, both 2026-08-17.** Stage 1: the Compose infrastructure and the real-PostgreSQL verification of everything Phase 2 had only run on SQLite. Stage 2 (step 20): `jobqueue.py`, `worker.py`, `queued` and its migration, the API's enqueue, the asynchronous gate resume, and the LocalStack integration layer — [ADR 0010](docs/adr/0010-job-dispatch-and-status-across-api-queue-and-worker.md), [ADR 0011](docs/adr/0011-the-human-gate-resume-moves-to-the-worker.md), [ADR 0012](docs/adr/0012-the-api-stops-holding-a-compiled-graph.md). Stage 3 (step 21): `redisstore.py` — the two caches, the per-job URL set, and the shared fail-closed rate limiter, with `checks.redis` on `/health`. **Still open: the application image and S3 (step 22), and CI (step 23).** |
| 4 | LangSmith tracing, eval dataset of 30–50 questions, eval as a release gate | Planned |
| 5 | AWS: ECS Fargate, RDS, ElastiCache, real SQS + S3, API Gateway, CloudWatch alarms, **Cognito JWT** | Planned |

**Single tenant** for now. Every table still carries `user_id`, so tenant scoping is a later additive
change rather than a migration. The **human gate is API-only** in Phase 2 —
`POST /jobs/{id}/approve`, no web UI.

**What Phase 2 added to the gate, and what it still does not do.** Phase 1 built the graph node that
pauses for a reviewer: `interrupt()` holds the job, and a resume decision routes approve → export,
reject → finalize, edit → one Synthesizer pass. Phase 2 built everything around it — the authenticated
endpoint, the identity on every decision, the durable checkpoint that lets a job survive a restart, the
audit rows, and one decision per gate visit. **`invariant 7` is satisfied by the code now, not by a
plan.** What the gate still does not have is expiry: a job can wait at it indefinitely, because the
7-day sweep is Phase 5's (step 32).

**Phase 3 stage 2 moved the resume off the request, and changed what a decision answers with.** The
endpoint cleans the reviewer's text, refuses an unaffordable edit, claims the gate, records the
decision, enqueues a resume — and returns `200 {status: "running"}` rather than the outcome
([ADR 0011](docs/adr/0011-the-human-gate-resume-moves-to-the-worker.md) decision 5). Every rule around
it survives unchanged: one decision per visit, the same decision again is a retry that writes nothing,
a different one is `409 gate_already_decided`, and an edit still costs one of three. What changed is
that **redelivery, not the reviewer, is now the recovery path** when a resume dies — the message was
never deleted, so it comes back on its own.

**The reviewer can also read what they are approving, since 2026-08-17.** `GET /jobs/{id}/gate`
returns the payload the gate node already builds, rebuilt from the checkpoint, with no graph
execution and no write ([ADR 0013](docs/adr/0013-reviewer-gate-payload-view.md)). It is a **Phase 2
correction found by the Phase 3 readiness review**: `reviewer_payload()` shipped with the gate node
in step 17 and step 18's five routes never exposed it, so until now a reviewer had an authenticated
endpoint that decides whether a report is exported and no way to read the report — `GET /jobs/{id}`'s
`report` is the *exported* body and is `null` for the whole time a job waits at the gate. The API
surface is six routes, not five.

**Known gaps carried forward, so they are not rediscovered as bugs:**

- **A job runs on its own from Phase 3 stage 2, and `python -m worker` is what runs it.** What is
  still missing around that is the operational half: there is no application image, no CI, and no AWS,
  so the queue is LocalStack's and a DLQ alarm is a thing nobody is watching. A message that reaches
  the dead-letter queue makes its job terminal (ADR 0010 decision 9) and otherwise sits there.
- **A hard kill leaves a job `running` forever.** SIGTERM is handled; SIGKILL and OOM are not, and
  cannot be — nothing gets to write. The message is redelivered and the worker recovers the job, but
  if the failure is the job itself, the row stays non-terminal after the message reaches the DLQ.
  ADR 0010 decision 9 assigns the recovery to Phase 5's retention-and-expiry sweep, which finds it as
  `status = 'running' AND completed_at IS NULL` with no in-flight message.
- **A job whose enqueue failed sits `queued` with nothing to pick it up.** `POST /jobs` answers
  `503 enqueue_failed` and deliberately keeps the row, because it holds the `idempotency_key` a
  re-enqueue would target — but nothing re-enqueues it yet. Also Phase 5's sweep
  (`status = 'queued' AND created_at < now() - interval`).
- **A Redis outage stops LLM work entirely, and that is the design rather than a gap.** The caches
  and the URL set fail open, so they cost a call each; the rate limiter fails closed, because a
  limiter that fails open is not a limiter (ARCHITECTURE.md §20 row 29). It is acceptable at 1–2
  workers and it is loud rather than silent: the node fails with `rate_limiter_unavailable`, the
  worker refuses to start, and `/health` reports `degraded` so the task leaves the target group.
- **The reflection rubric is uncalibrated.** Until the hand-scoring pass in Phase 4 runs, the pass
  threshold is a reasonable heuristic, not a measured one.
- **Traces recorded before 2026-08-14 carry inflated token totals.** `scripts/measure_jobs.py` built
  one `OpenAI` client and a new `LLMClient` per job, and `LLMClient.__init__` applies `wrap_openai` to
  whatever client it is handed. That wrapper patches in place with no idempotency guard, so job *N*
  ran under *N* layers and emitted *N* nested spans per request, the outermost reporting a running
  total — job 3 of a 6-job run traced 1,597,176 prompt tokens against a real 266,196. **Behaviour was
  never affected:** httpx logged one POST per counted call, so no extra spend, rate pressure, or
  retries. **Fixed on 2026-08-14** — the harness now builds one `LLMClient` in `main()` and shares it,
  which `tests/test_measure_jobs.py` pins, and the n=20 traces then showed **0 nested `llm` spans
  across 590 leaf spans**. The old traces still have to be read off their leaf spans.
- **Some published evidence exists only as LangSmith traces, and nothing in this repository can
  reproduce it.** Two sets: the **2026-08-14 token and derived-cost figures** — `measurements/jobs.jsonl`
  carries `null` in both token columns, so §13–§14's token rows were recovered from the traces — and
  the **`smoke-e2e-*` cases** behind `docs/ARCHITECTURE.md` §22 items 5 and 6, which were driven
  interactively and wrote no measurement row or log line at all. Both were confirmed present on
  2026-08-15. **If those traces age out of the LangSmith project, the figures become unverifiable from
  the repo alone** — the retention window that applies has not been checked, so treat the risk as real
  and undated. The durable fix is to capture what still matters into the Phase 4 eval set rather than
  to keep citing traces; until then, re-run the question rather than reconstructing a number from prose.

**Future enhancement: adaptive Researcher query rewriting after evidence exhaustion.**

[ADR 0004](docs/adr/0004-no-op-researcher-retries-after-evidence-exhaustion.md) stops reflection
retrying a subtopic that produced nothing, because the retry re-issues the same planned query against
the same cached results with no unread source to reach. That spends the revision budget better; it
does not fill the evidence gap, which is reported to the reviewer instead. A future change might have the
Researcher generate a **bounded** alternative query, search again, and judge whether the new evidence
is genuinely better.

**Not implemented in the current production-hardening change.** The open questions — how the rewrite
is generated, how many are allowed, how different one has to be, whether it bypasses the search cache,
what the extra search costs, how "better evidence" is judged against an uncalibrated rubric, and above
all the injection risk if fetched page text could influence a search query (invariant 4) — are written
out in `docs/ARCHITECTURE.md` §22 item 4 and in ADR 0004's own future-work section.

---

## Where to look next

| Question | File |
|---|---|
| What is actually built right now? | `docs/ARCHITECTURE.md` §1 "Built vs planned", and §22 of the guidelines |
| How does an agent's contract work in detail? | `docs/engineering-guidelines.md` §2 |
| How is state designed and persisted? | `docs/engineering-guidelines.md` §4 |
| How do we defend against prompt injection, and what is the honest claim? | `docs/engineering-guidelines.md` §8 |
| How is the system tested, and what is the FakeLLM? | `docs/engineering-guidelines.md` §18 |
| What are the API endpoints and their contracts? | `docs/engineering-guidelines.md` §12 |
| Who is allowed to call what, and who may approve? | `docs/engineering-guidelines.md` §16 |
| How is research quality measured? | `docs/engineering-guidelines.md` §15 — planned |
| How does a change get built, migrated, deployed, or rolled back? | `docs/engineering-guidelines.md` §19 — planned |
| Why was a decision made this way? | `docs/ARCHITECTURE.md` §20 for decisions settled before implementation; `docs/adr/` for the ones taken since |
| How do I explain this project out loud? | `docs/interview-prep.md` |

ADRs follow the format `docs/adr/NNNN-kebab-case-title.md` with a `README.md` index.
