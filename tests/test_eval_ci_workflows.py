"""
WHY THIS FILE EXISTS
    Two claims about CI that are easy to state and easy to break silently, and that no other
    test in this repository would notice:

    **Normal CI reaches no provider.** The `eval` job runs the twelve deterministic metrics and
    nothing else. If someone adds `--judge` to it, or gives the job an `LLM_*` variable, every
    pull request starts spending money and every fork's CI starts failing on a missing secret.
    That change would be one line and would look reasonable in review.

    **The judge workflow can never become a required check.** It is `workflow_dispatch` only.
    Adding a `pull_request:` trigger to it would be one line too, and would quietly make a
    provider a dependency of merging.

    Also here, because they are the same kind of silent erosion: the six Phase 3 jobs are still
    present, the eval job needs no Docker or service, and the judge workflow reads its
    credential from a secret rather than from a literal.

    **These are file contents parsed as documents**, the rule `test_container_image.py` follows
    for `docker-compose.yml`: what is under test is structure - which triggers exist, which env
    a job carries - and a regex over YAML is the kind of test that passes for the wrong reason.

WHO CALLS IT
    pytest, as part of the offline suite. Nothing here runs a workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_JUDGE = _ROOT / ".github" / "workflows" / "eval-judge.yml"

PHASE_3_JOBS = ("quality", "unit", "postgres", "redis", "integration", "container")
"""The six jobs that were green before Block C. Block C adds a seventh and touches none of
these, which is the whole of "do not weaken the existing pipeline" as a checkable statement."""

PROVIDER_VARIABLES = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_FAST_MODEL",
    "LLM_API_KEY",
    "TAVILY_API_KEY",
)
"""What the evaluation job may not carry. The same list `test_container_image.py` uses for the
API container, for the same reason: a process that cannot see a credential cannot spend one."""


def _workflow(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _job(path: Path, name: str) -> dict[str, Any]:
    return cast(dict[str, Any], _workflow(path)["jobs"][name])


def _run_steps(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in job["steps"] if "run" in step]


def _triggers(path: Path) -> dict[str, Any]:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1), which is why the key
    # this looks up is not the string a reader of the file sees.
    document = cast(dict[Any, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    return cast(dict[str, Any], document.get("on", document.get(True)))


# --- 1. The six Phase 3 jobs are untouched --------------------------------------------------


def test_every_phase_3_job_is_still_present() -> None:
    jobs = _workflow(_CI)["jobs"]

    for name in PHASE_3_JOBS:
        assert name in jobs, f"CI lost the {name} job"


def test_the_localstack_diagnostics_are_still_there() -> None:
    # They exist because LocalStack's healthcheck waits for the queue and the bucket, and
    # neither the hook's output nor the failing probe's survives cleanup.
    for name in ("integration", "container"):
        steps = [str(step.get("name", "")) for step in _job(_CI, name)["steps"]]
        assert "Diagnose LocalStack" in steps, name


# --- 2. The deterministic evaluation job ----------------------------------------------------


def test_ci_has_a_dedicated_eval_job() -> None:
    assert "eval" in _workflow(_CI)["jobs"]


def test_the_eval_job_runs_the_benchmark_and_then_the_gate() -> None:
    # Two commands, deliberately: producing the evidence and judging it are separate, so the
    # judgement cannot quietly re-decide the measurement.
    runs = _run_steps(_job(_CI, "eval"))

    assert any("eval.run" in command for command in runs)
    assert any("eval.gate" in command for command in runs)


def test_the_eval_job_never_enables_the_judge() -> None:
    for command in _run_steps(_job(_CI, "eval")):
        assert "--judge" not in command


def test_the_eval_job_carries_no_provider_credential() -> None:
    job = _job(_CI, "eval")
    workflow_env = _workflow(_CI).get("env", {})
    job_env = job.get("env", {})
    step_envs = [step.get("env", {}) for step in job["steps"]]

    for scope in (workflow_env, job_env, *step_envs):
        for name in PROVIDER_VARIABLES:
            assert name not in scope, f"the eval job can see {name}"


def test_the_eval_job_needs_no_docker_and_no_service() -> None:
    # The twelve metrics are pure functions over committed fixtures, so this job is the
    # cheapest in the pipeline - and coupling it to a service would be the fastest way to
    # make people stop trusting it.
    job = _job(_CI, "eval")

    assert "services" not in job
    for command in _run_steps(job):
        assert "docker" not in command


def test_the_eval_job_uploads_its_report() -> None:
    # A violation has to be diagnosable without re-running anything.
    steps = _job(_CI, "eval")["steps"]
    uploads = [step for step in steps if "upload-artifact" in str(step.get("uses", ""))]

    assert uploads, "the eval job keeps no report"
    assert uploads[0].get("if") == "always()"


def test_no_ci_job_applies_a_score_threshold() -> None:
    # ADR 0018: the gate takes a report path and nothing else. A numeric argument appearing
    # here would be a threshold nobody wrote an ADR for.
    for command in _run_steps(_job(_CI, "eval")):
        for forbidden in ("--min-", "--threshold", "--max-", "--fail-under"):
            assert forbidden not in command


# --- 3. The judge workflow ------------------------------------------------------------------


def test_the_judge_workflow_exists_and_is_manual_only() -> None:
    triggers = _triggers(_JUDGE)

    assert "workflow_dispatch" in triggers
    # The two that would make a provider a dependency of merging.
    assert "pull_request" not in triggers
    assert "push" not in triggers
    assert "schedule" not in triggers


def test_the_judge_workflow_reads_its_credential_from_a_secret() -> None:
    text = _JUDGE.read_text(encoding="utf-8")

    assert "secrets.EVAL_JUDGE_API_KEY" in text
    # And the workflow never echoes an environment or a key.
    assert "env | " not in text
    assert "printenv" not in text


def test_the_judge_workflow_distinguishes_a_provider_fault_from_a_low_score() -> None:
    # `--require-judge-scores` fails only when the judge answered nothing at all. There is no
    # score comparison anywhere in the workflow.
    commands = " ".join(_run_steps(_job(_JUDGE, "judge")))

    assert "--judge" in commands
    assert "--require-judge-scores" in commands
    for forbidden in ("--min-", "--threshold", "--fail-under"):
        assert forbidden not in commands


def test_the_judge_workflow_never_reads_a_real_database() -> None:
    # `--from-database` would put real report text into an uploaded artifact.
    commands = " ".join(_run_steps(_job(_JUDGE, "judge")))

    assert "--from-database" not in commands


def test_the_judge_workflow_is_not_reachable_from_ci() -> None:
    # No `uses:` reference and no reusable-workflow call, so nothing in the merge path can
    # pull it in by accident.
    text = _CI.read_text(encoding="utf-8")

    assert "eval-judge" not in text
