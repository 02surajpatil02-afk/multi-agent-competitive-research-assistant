"""
WHY THIS FILE EXISTS
    The offline evaluation subsystem (Phase 4, block A+B). It answers one question the rest
    of the repository cannot: *is the research any good?* Everything else measures whether a
    job ran - latency, calls, statuses, audit rows - and a job can run perfectly and produce
    a useless report.

    Four rules shape what is in here, and each is a decision recorded in
    docs/adr/0017-deterministic-evaluators-and-a-custom-structured-judge.md:

      * **Evaluation reads produced outputs; it does not run the graph.** A research job costs
        minutes and real LLM calls, so re-running one to score it would make evaluation too
        expensive to run often - which is the same as not having it.
      * **Deterministic first.** Coverage is arithmetic and duplicates are a set operation.
        Spending a judge call on either buys variance in a number that should be exact
        (guidelines §15).
      * **The judge is optional and off by default.** `pytest` and CI stay provider-free.
      * **No opaque overall score.** Every metric is reported on its own, and nothing here
        computes a single number that hides which of twelve things regressed.

WHO CALLS IT
    `python -m eval.run`, and the tests in tests/test_eval_*.py. No production module imports
    anything from this package: evaluation observes the system, it is not part of it.
"""
