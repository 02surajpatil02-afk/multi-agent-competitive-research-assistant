# Architecture Decision Records

One file per decision, named `NNNN-kebab-case-title.md` (`CLAUDE.md`). A record is written when a
decision changes something the guidance already states — not for every choice. Decisions that were
settled before implementation began live in `docs/ARCHITECTURE.md` §20 and are not restated here.

A record is never edited to reflect a later change of mind. It is superseded by a new one, and the
old record's status says so, because the reasoning that was current at the time is the useful part.

**A factual error in a measurement a record reports is the one exception**, because other documents
quote those numbers from the record and leaving a known-wrong figure in the source spreads the error.
It is corrected in place, with a dated audit note that keeps the original value and says what was
wrong. [ADR 0002](0002-concurrent-page-extraction-in-the-researcher.md) carries one.

| # | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-supervisor-llm-routing-is-advisory.md) | The Supervisor's LLM routing call is advisory; `allowed_target(state)` is authoritative | Accepted | 2026-08-12 |
| [0002](0002-concurrent-page-extraction-in-the-researcher.md) | A subtopic's page extractions run concurrently; choosing and fetching sources stays sequential | Accepted | 2026-08-13 |
| [0003](0003-finding-ids-are-a-per-job-sequence.md) | Finding ids are a short per-job sequence (`f1`, `f2`, …), assigned after the extraction pool joins | Accepted | 2026-08-14 |
| [0004](0004-no-op-researcher-retries-after-evidence-exhaustion.md) | Reflection does not retry an `unresearched` subtopic; with no target left it acts on another failing dimension, or reaches the gate `below_threshold` | Accepted | 2026-08-14 |
