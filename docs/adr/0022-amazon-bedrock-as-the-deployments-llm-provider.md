# ADR 0022 — Amazon Bedrock is the deployment's LLM provider, behind one client contract and with no key

- **Status:** **Accepted and built, 2026-08-24**; **never applied to AWS, and no Bedrock call has
  ever been made from this repository**. Supersedes
  [ARCHITECTURE.md](../ARCHITECTURE.md) §20 row 4's *"no provider class and no provider
  abstraction"* in the narrowest possible way — see **Consequences**
- **Date:** 2026-08-24
- **Affects:** `llm_client.py` · `bedrock.py` (new) · `config.py` · `worker.py` · `eval/run.py` ·
  `infra/versions.tf` · `infra/variables.tf` · `infra/ecs.tf` · `infra/iam.tf` ·
  `infra/secrets.tf` · `infra/outputs.tf` · `Dockerfile` · `docker-compose.yml` ·
  `.env.example` · `docs/deployment.md`
- **Changes nothing about:** the graph's topology, the five agents, the reflection node, the
  structured-output contract, ADR 0005's durability semantics, ADR 0010's dispatch and status
  rules, ADR 0015's visibility leases, ADR 0016's execution fence, the human gate, or the
  evaluation gate

---

## Context

The deployment is going to AWS, and the one thing in it that still needed a third-party account
was the model. Every other external service the worker touches — the queue, the cache, the
database, the object store — is an AWS service reached with the ECS task role. The model endpoint
was an OpenAI-compatible URL and a bearer token, which meant a credential to obtain, a secret to
store, an execution-role grant to maintain, and a value sitting in `terraform.tfstate`.

Amazon Bedrock removes all four at once. Its Converse API takes the same request shape for every
model family on the platform, Amazon Nova Pro is a capable general model on it, and
**authentication is IAM** — the worker's task role carries `bedrock:InvokeModel` and boto3 signs
with it through the credential chain it already uses for SQS and S3.

**What made this a decision rather than a swap** is that `llm_client.py` had said, since Phase 1
and in its own opening paragraph, that there is *no provider class and no provider abstraction*.
That was a good rule for the situation it described: NIM in development and a production
OpenAI-compatible API speak the same protocol, so an abstraction between them would have been
ceremony over a difference that did not exist. Bedrock is a genuinely different protocol —
system instructions in their own field, content as typed blocks, `inferenceConfig` instead of
top-level parameters, AWS error codes instead of HTTP statuses, no JSON mode at all — so
something had to give.

Three further constraints shaped it:

- **The agents must not change.** Five agents and the reflection node call
  `call_structured(schema=...)` and receive a validated Pydantic instance. If a provider change
  reached them, the abstraction would be in the wrong place.
- **Nova has no structured-output mode.** The OpenAI path asks for `response_format:
  {"type": "json_object"}`. There is no equivalent to switch on in Converse, so whatever
  enforcement exists has to be the prompt plus validation.
- **The offline suite must stay offline.** It runs with no network and no credential, and it must
  not start reading a developer's AWS profile because a module grew a boto3 import.

---

## Decision

### 1. One provider seam, one method wide, and every policy stays above it

`llm_client.py` gains a `ChatProvider` Protocol with a single method:

```python
def complete(self, *, messages, model, timeout, temperature) -> str: ...
```

and one exception type, `ProviderError(kind, message, retry_after=None)`, whose `kind` is
`rate_limited`, `transport` or `fatal`.

Everything that decides **policy** stayed in `LLMClient`: the per-job `CallBudget`, the shared
Redis rate limiter, both backoff schedules, the per-tier timeouts, the JSON Schema in the system
prompt, and the one validation retry. An adapter sends one request, returns the text, and
translates its SDK's failures into those three kinds. That is the whole contract.

**The three kinds are the seam's real content**, and they were chosen from how `_send` already
behaved rather than from what either SDK reports: a rate-limited job fails, a flaky connection
fails only the node, and a configuration error is not retried at all. An adapter that could not
answer "which of these three is it?" would be pushing a decision up that it is the only thing
able to make.

**What this deliberately is not:** a provider *class hierarchy*, a factory, a registry, or a
second client. There are two implementations of one Protocol, selected by one `if`. The rule in
the global engineering guidelines is that an abstraction is justified when more than one
implementation is actually needed *now* — there are two, and this says so out loud.

### 2. Provider selection is configuration, and there is no fallback

`LLM_PROVIDER` takes `openai` or `bedrock` and is refused loudly for anything else, exactly as
`AUTH_MODE` is ([ADR 0020](0020-cognito-jwt-validation-and-secret-injection.md) decision 2).

- **`config.py`'s default is `openai`**, so every local command, `docker compose up`, the whole
  offline suite, `scripts/measure_jobs.py` and `scripts/check_model.py` behave exactly as they
  did. Nothing local needs AWS.
- **Terraform's default is `bedrock`**, so the deployment makes the production choice explicitly.

The two defaults differ on purpose, and that is the same shape `AUTH_MODE` already has: local
stays stable, AWS states its choice.

**There is deliberately no automatic fallback from Bedrock to OpenAI.** A worker that failed over
would spend on two providers, under two sets of pricing and two sets of semantics, for a failure
the schedules in guidelines §17 already bound — and it would need an OpenAI credential to exist
in a deployment whose entire point is that it holds none. Provider selection is `bedrock` **or**
`openai`, never `bedrock` **then** `openai`.

### 3. Bedrock authenticates with IAM, and the deployment holds no model credential

`boto3.client("bedrock-runtime")` picks up the ECS task role through the ordinary provider chain.
There is no Bedrock API key, no long-lived access key in the container, and **no Secrets Manager
entry for the model provider**: under `llm_provider = "bedrock"` the `llm-api-key` secret is not
created, the worker's execution role is granted nothing to fetch for it, and no task definition
revision — which is kept forever — names a provider credential.

**No empty placeholder secret is created either.** An empty secret would make `terraform apply`
succeed and the worker crash-loop on a credential it never needed, which is a worse failure than
the absence it was trying to smooth over.

`TAVILY_API_KEY` is untouched. Web search is a real third-party API with a real key whichever
model answers, and it stays where it was.

### 4. Structured output is enforced by the caller, not by the adapter

`call_structured` already put the JSON Schema in the system prompt and already validated the
answer with pydantic, retrying exactly once with the validation error carried into the retry
prompt. **None of that moved.** Both providers get the same prompt and the same validation, and
the caller gets a validated instance of the schema or `LLMCallFailed`.

That matters more on Bedrock than on OpenAI, because there is no JSON mode to switch on: the
prompt is the whole request-side enforcement, and the validation is what makes that safe rather
than hopeful. It is also what keeps the two providers comparable — an adapter that improved its
own prompt would make the evaluation of one say nothing about the other.

**One concession, and it removes rather than adds.** `bedrock._json_text` strips a markdown fence
if the model wrapped its JSON in one. The prompt already asks for no fences; a fenced object is a
formatting slip rather than a wrong answer, and letting it consume the single validation retry
would spend a call from a 60-call budget on punctuation. Anything that is not exactly a fenced
block is returned untouched.

**No agent prompt was modified.** The system prompt Converse receives is byte-for-byte the one
`_system_prompt` produced.

### 5. Bedrock mode uses one model id for both tiers

Converse takes one `modelId`, and this deployment configures one: `BEDROCK_MODEL_ID`, which
accepts either a foundation model id (`amazon.nova-pro-v1:0`) or a cross-region inference profile
id (`apac.amazon.nova-pro-v1:0`) — Converse takes both in the same field, so nothing in the
application has to know which it was given and **no model routing is implemented here at all**.

The consequence is stated rather than hidden: the OpenAI path has a cheap `LLM_FAST_MODEL` for
Supervisor routing and reflection scoring, and Bedrock mode sends those calls to Nova Pro too.
The tier still selects the request timeout and the backoff schedule; it no longer selects a
cheaper model. **That is a cost difference, not a behaviour difference**, and adding a second
model id is a decision for a deployment that has measured the first one.

### 6. Which variables a process requires depends on the provider

`worker.required_credentials` already stated what the worker cannot start without
([ADR 0012](0012-the-api-stops-holding-a-compiled-graph.md) decision 4). It now states it per
provider: the database, the queue, the bucket and the Tavily key in both modes, plus
`BEDROCK_MODEL_ID` under Bedrock or `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` under OpenAI.

Demanding `LLM_API_KEY` in Bedrock mode would be demanding a value with nothing to authenticate
against. **The API still requires none of them**, in either mode.

The same conditional shape reaches Terraform, where Block A had it wrong: `llm_base_url`,
`llm_model` and `llm_api_key` had no defaults, so a Bedrock deployment could not `apply` **or
`destroy`** without inventing values for variables nothing would read. All three now default to
empty with a `validation` that fires only when `llm_provider == "openai"` — the same refusal,
placed where it is actually true.

### 7. The application owns retries; both SDKs' own retries stay off

`LLMClient._send` is the only retry layer, whichever provider answers. The OpenAI SDK is built
with `max_retries=0` and botocore with `total_max_attempts=1`, for the same reason `artifacts.py`
already does it: two schedules multiply into an attempt count nobody wrote down, and here every
attempt additionally spends a token of the **shared** rate limiter and a call of the per-job
budget. A silent SDK retry would send three requests against one token.

**Effective retry ownership, in one table:**

| Layer | Owns | Bound |
|---|---|---|
| `LLMClient._send` | transport retries, per tier | 2 retries — main 2s/8s, fast 1s/4s; then `llm_call_failed`, node fails |
| `LLMClient._send` | rate-limit retries | 3 retries — provider `Retry-After` capped at 30s, else 2s/8s/30s; then `rate_limited`, **job** fails |
| `LLMClient.call_structured` | validation retries | exactly 1, carrying the pydantic error |
| `LLMClient._take_a_token` | shared limiter retries | 2 retries at 2s/8s; then `rate_limiter_unavailable`, node fails |
| `bedrock.py` / `OpenAIChatProvider` | **classification only** | never waits, never re-sends |
| The OpenAI SDK | nothing | `max_retries=0` |
| botocore | nothing | `total_max_attempts=1` |

A `fatal` classification is not retried at all. An unrecognised AWS error code is classified
`fatal` by default: an unknown code is more likely a denied action than a transient fault, and
retrying the former spends three calls and three tokens to print one message three times.

### 8. Token usage is recorded; cost is not derived

Converse returns `usage.inputTokens` and `usage.outputTokens`. `bedrock.py` logs those, next to
the model id, and stops there.

**There is no cost figure, and there should not be one.** A price is not in the response, and a
hard-coded rate is a number that is wrong the first time AWS changes one. A missing usage block
is reported as unknown rather than estimated — a fabricated token count is worse than none,
because it looks like a measurement.

**No Phase 4 gate depends on provider pricing**, and none was changed. `eval/gate.py` is still a
regression contract over committed fixtures with no percentage in it
([ADR 0018](0018-the-ci-evaluation-gate-protects-the-contract-not-the-quality.md)).

**LangSmith tracing is OpenAI-only and stays that way.** `wrap_openai` wraps that SDK; Bedrock
mode emits LangGraph's run tree without a per-request LLM span, and the log line above is what
carries the tokens. That is a real reduction in trace detail and is listed under Consequences
rather than worked around.

### 9. IAM names the Bedrock resources and never uses a wildcard

The worker **task** role — and only the worker task role — gets one action,
`bedrock:InvokeModel`, on a named resource list.

`bedrock:InvokeModelWithResponseStream` is **not** granted: nothing calls `ConverseStream`, and a
permission granted "for completeness" is how a least-privilege policy stops being one.
`bedrock:ListFoundationModels` is not granted either — discovering a model is an operator's job
with their own credentials.

**Cross-region inference is where `Resource = "*"` becomes tempting, and this is the refusal.**
Invoking through an inference profile is authorized against *two* kinds of resource: the profile,
which lives in this account and this Region, and the foundation model in whichever Region the
request is routed to, which is AWS-owned and has no account id in its ARN. Naming only the first
produces an `AccessDeniedException` on the first job.

So the policy is derived from the one id the application sends:

- the id's **segment count** distinguishes the two kinds — `amazon.nova-pro-v1:0` has two
  dot-separated segments, `apac.amazon.nova-pro-v1:0` has three. Matching the geo prefix itself
  does not work: the list is `us`, `eu`, `apac`, `jp`, `global` and more, and any pattern loose
  enough to hold all of them also matches the `amazon.` of a bare model id;
- a profile contributes `arn:aws:bedrock:<region>:<account>:inference-profile/<id>`;
- the base model contributes `arn:aws:bedrock:<region>::foundation-model/<base-id>` for each
  Region in `bedrock_inference_profile_regions`, which is an **explicit Terraform input** because
  a profile's destination list is a property of the profile that nothing here reads.

Empty means this Region only, which is right for a plain model id. `docs/deployment.md` §4.0
carries the `get-inference-profile` command that produces the list, because in-Region model
availability is an account fact no Terraform configuration can assert.

**No other role gains anything.** The API constructs no LLM client (ADR 0012), a migration talks
only to PostgreSQL and has no task role at all, and the four recovery scripts re-project rows and
move messages ([ADR 0009](0009-recovering-an-export-that-failed-after-approval.md),
[ADR 0021](0021-stale-job-reconciliation-and-dlq-recovery.md)). Tests fail if any of the three
gains a Bedrock action.

### 10. Transport bounds are derived from the application's, never invented

botocore has no per-call timeout — `read_timeout` belongs to the client — so the client is built
with `max(LLM_MAIN_TIMEOUT_S, fast tier) + 30s`. Equal values would race: a request that took
exactly its allowance would lose about half the time, reporting a transport failure for a
generation that was merely slow, and charging another call to retry it.

`complete()` **refuses** a request longer than the client was built for rather than silently
serving a shorter bound, because a silent shorter bound looks like an endpoint that times out.

`LLM_MAIN_TIMEOUT_S=180`, `MAX_REVISIONS=3`, `MAX_SUPERVISOR_HOPS=30` and `MAX_JOB_RUNTIME=1800`
are unchanged.

---

## Alternatives considered

**An OpenAI-compatibility shim in front of Bedrock.** Rejected. It would add a translation layer
whose failures look like model failures, in exchange for reusing a client that would still need
this file's error mapping — the error codes, the missing JSON mode and the response shape do not
go away because the request shape was disguised.

**`InvokeModel` with a Nova-specific request body instead of Converse.** Rejected. Converse is the
one runtime API with the same request shape for every model family, which is exactly what makes
`BEDROCK_MODEL_ID` a configuration value rather than a code branch. A Nova-shaped body would make
"try a different model" a code change, which is the property ARCHITECTURE.md §20 row 4 was
protecting in the first place.

**A second LLM client for Bedrock.** Rejected. It would be a second place for the budget, the
limiter, the two schedules and the validation retry to live, and the first drift between them
would be discovered in production.

**Provider fallback, Bedrock then OpenAI.** Rejected — decision 2. Hidden cost, two sets of
semantics, and a credential the deployment exists to not have.

**Model routing — a cheap model for the fast tier.** Not built. Decision 5 states the cost
difference instead. Routing is a second variable, a second IAM resource and a second thing to
verify before an apply, for a saving nobody here has measured.

**`Resource = "*"` on the Bedrock grant.** Rejected — decision 9. It is the one-line way to make
cross-region inference work while granting every model in every Region, and a test fails if it
appears.

**A `bedrock:InvokeModelWithResponseStream` grant "just in case".** Rejected — decision 9.

---

## Consequences

**ARCHITECTURE.md §20 row 4 is narrowed, not overturned.** *"Swapping a model is a config change,
never a code change"* is still exactly true, and is now true across two protocols rather than one.
What is no longer true is *"there is no provider abstraction"* — there is one, it is a Protocol
with a single method, and it exists because two implementations are needed now.

**Bedrock mode loses per-request LangSmith spans.** `wrap_openai` wraps the OpenAI SDK. LangGraph's
run tree is unaffected and the `thread_id` join still works; what disappears is the per-call span
carrying model, latency and usage. `bedrock.py` logs the model and the token counts instead. This
is a real reduction and is why the trace-metadata contract (Phase 4 step 24) is worth more now
than it was.

**Both published n=20 baselines describe NIM and say nothing about Nova.** Latency, call counts,
node shares and derived cost were measured against a different endpoint. **They are not a Bedrock
estimate**, and a production-default Bedrock run is a separate measurement nobody has taken.

**The reflection rubric and the evaluation benchmark are unaffected and equally uncalibrated.**
The DEV benchmark is fixture-backed and scores authored files; it says nothing about either
provider's research quality.

**Nothing has been applied.** No `terraform apply`, no image push, no Bedrock call, and no AWS
API call of any kind was made in building this. Model access, the inference profile id and its
destination Regions are verified by an operator before the first apply — `docs/deployment.md`
§4.0.

**One thing to expect on the first real run:** Nova's JSON adherence under this prompt is
unmeasured. The failure mode is bounded — a malformed answer costs the single validation retry
and then fails the node, exactly as it would on any other endpoint — but if it turns out to be
common, the right fix is a measurement and then a prompt change in `_system_prompt`, which is the
one place both providers read from.
