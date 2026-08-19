"""
WHY THIS FILE EXISTS
    `infra/` is the deployment, and almost nothing in it fails loudly. A database with
    `publicly_accessible = true` still starts. A security group that trusts a CIDR instead of
    another group still lets the application work. A NAT gateway added "to be safe" charges by
    the hour and breaks nothing. An API task definition that picked up an LLM key would serve
    every route exactly as it does now. Those are the failures asserted here.

    **This is not a substitute for `terraform validate`, and neither is a substitute for this.**
    Validate answers "is this valid HCL that could plan"; CI runs it in the `infra` job. These
    tests answer "does it still describe the architecture that was agreed" - which is a question
    about intent, and one Terraform has no opinion about.

    Six claims are worth a test rather than a sentence:

    **No NAT gateway** (docs/adr/0019-*.md). It is the single largest per-hour charge the
    textbook design would add, and adding one is a one-line edit that no test would otherwise
    notice.

    **The database and the cache are not reachable from outside.** Three independent facts hold
    that: private subnets whose route table has no internet route, `publicly_accessible = false`,
    and security groups whose ingress rules name other groups rather than any CIDR.

    **The worker has no ingress rule at all**, and the API has exactly one, from the load
    balancer. Fargate tasks sit in public subnets so they can reach an LLM endpoint without a
    NAT gateway, which makes the security group the only thing standing between a public IP and
    a reachable service.

    **The queue is the queue the application already expects**: FIFO, a 1800-second visibility
    window (ADR 0015), three deliveries then a DLQ (ARCHITECTURE.md section 11), and explicit
    deduplication ids rather than content-based deduplication.

    **The three commands are unchanged**, and the worker still gets `stopTimeout = 120`
    (ARCHITECTURE.md section 19) - the number that has to match `stop_grace_period` in
    docker-compose.yml or local shutdown stops predicting deployed shutdown.

    **Nothing carries a LocalStack endpoint into AWS.** `AWS_ENDPOINT_URL` and
    `S3_PUBLIC_ENDPOINT_URL` exist because the local stack has two addresses for one service.
    Against real AWS they must be absent, or `artifacts.presign` signs a host that resolves
    nowhere - and SigV4 covers the host, so it cannot be repaired afterwards.

    **Nothing here runs Terraform, opens a socket or reads an AWS credential** - these are file
    contents parsed and compared, the same rule `test_container_image.py` follows.

WHO CALLS IT
    pytest, as part of the offline suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_INFRA = _ROOT / "infra"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"

REQUIRED_OUTPUTS = (
    "api_url",
    "ecr_repository_url",
    "jobs_queue_url",
    "jobs_dlq_url",
    "reports_bucket",
    "rds_endpoint",
    "redis_endpoint",
    "ecs_cluster_name",
    "api_service_name",
    "worker_service_name",
    "migrate_task_definition",
)
"""What docs/deployment.md's deploy, verify and teardown steps have to be able to name."""

FORBIDDEN_RESOURCE_TYPES = (
    # The NAT-gateway policy, and the Elastic IP one would need.
    "aws_nat_gateway",
    "aws_eip",
    # Block B.
    "aws_cognito_user_pool",
    "aws_secretsmanager_secret",
    # Block C.
    "aws_cloudwatch_metric_alarm",
    "aws_appautoscaling_target",
    "aws_appautoscaling_policy",
)
"""Resource types whose absence is a decision. Each would be a one-line addition that changes
either the cost shape or which block this is."""

LOCALSTACK_ONLY_VARIABLES = ("AWS_ENDPOINT_URL", "S3_PUBLIC_ENDPOINT_URL")

TOP_LEVEL_BLOCKS = frozenset(
    {"terraform", "provider", "resource", "data", "variable", "output", "locals"}
)

PROVIDER_VARIABLES = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_FAST_MODEL",
    "LLM_API_KEY",
    "TAVILY_API_KEY",
)


# --- A small HCL reader -------------------------------------------------------------------
#
# Enough to split a .tf file into its top-level blocks and read a simple attribute out of one.
# Written rather than depended on because the alternative is a regex over the whole file, which
# passes for the wrong reason - `publicly_accessible = false` matching anywhere in a document
# says nothing about which resource it belongs to. Comments and heredocs are removed first, so
# a `#` inside prose and a URL's `//` cannot be mistaken for either.


@dataclass(frozen=True)
class Block:
    """One top-level `type "label" "label" { ... }`, with its body as text."""

    type: str
    labels: tuple[str, ...]
    body: str

    @property
    def address(self) -> str:
        return ".".join((self.type, *self.labels))


_HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\s*$")
_BLOCK_OPENER = re.compile(r'^(?P<type>[A-Za-z_][A-Za-z0-9_]*)(?P<labels>(?:\s+"[^"]*")*)\s*\{')


def _without_strings(line: str) -> str:
    """The line with every double-quoted span blanked, so braces inside strings do not count."""
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)


def _readable_lines(text: str) -> list[str]:
    """Comments and heredoc bodies removed; everything else kept, line for line."""
    out: list[str] = []
    heredoc_marker: str | None = None
    for line in text.split("\n"):
        if heredoc_marker is not None:
            if line.strip() == heredoc_marker:
                heredoc_marker = None
            continue
        stripped = _without_strings(line)
        if stripped.lstrip().startswith(("#", "//")):
            continue
        opener = _HEREDOC.search(stripped)
        if opener is not None:
            heredoc_marker = opener.group(1)
            # Keep the attribute name, drop the heredoc introducer.
            out.append(line.split("<<")[0].rstrip())
            continue
        out.append(line.split(" #")[0].rstrip() if " #" in stripped else line)
    return out


def _blocks(text: str) -> list[Block]:
    lines = _readable_lines(text)
    blocks: list[Block] = []
    current: Block | None = None
    body: list[str] = []
    depth = 0
    for line in lines:
        counted = _without_strings(line)
        if current is None:
            opener = _BLOCK_OPENER.match(line)
            if opener is None:
                continue
            labels = tuple(re.findall(r'"([^"]*)"', opener.group("labels")))
            current = Block(opener.group("type"), labels, "")
            body = []
            depth = counted.count("{") - counted.count("}")
            if depth == 0:
                blocks.append(current)
                current = None
            continue
        depth += counted.count("{") - counted.count("}")
        if depth <= 0:
            blocks.append(Block(current.type, current.labels, "\n".join(body)))
            current = None
            continue
        body.append(line)
    assert current is None, "a block was never closed; run `terraform fmt` and check the braces"
    return blocks


def _attribute(body: str, name: str) -> str | None:
    """The first `name = value` in a body, at any nesting depth, as written."""
    found = re.search(rf"^\s*{re.escape(name)}\s*=\s*(?P<value>.+?)\s*$", body, re.MULTILINE)
    return None if found is None else found.group("value")


def _tf_text() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(_INFRA.glob("*.tf"))}


def _tf_code() -> dict[str, str]:
    """The same files with the comments and heredoc prose removed.

    Every "this string must not appear anywhere" assertion below reads this rather than the raw
    text, because the comments discuss LocalStack, `MAX_REVISIONS` and the region on purpose -
    explaining why a thing is absent is not the same as declaring it.
    """
    return {name: "\n".join(_readable_lines(text)) for name, text in _tf_text().items()}


def _all_blocks() -> list[Block]:
    return [block for text in _tf_text().values() for block in _blocks(text)]


def _resources(resource_type: str) -> dict[str, Block]:
    return {
        block.labels[1]: block
        for block in _all_blocks()
        if block.type == "resource" and block.labels and block.labels[0] == resource_type
    }


def _variables() -> dict[str, Block]:
    return {
        block.labels[0]: block
        for block in _all_blocks()
        if block.type == "variable" and block.labels
    }


def _one(resource_type: str) -> Block:
    found = _resources(resource_type)
    assert len(found) == 1, f"expected exactly one {resource_type}, found {sorted(found)}"
    return next(iter(found.values()))


def _workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CI.read_text(encoding="utf-8")))


# --- One IaC tool, and it parses ------------------------------------------------------------


def test_the_infrastructure_is_terraform_and_only_terraform() -> None:
    """One tool. A second IaC framework beside this one is two sources of truth for one
    account, and the first drift between them is discovered during a deploy."""
    assert _INFRA.is_dir()
    assert _tf_text(), "infra/ holds no .tf files"

    others = ("cdk.json", "app.py", "template.yaml", "template.yml", "serverless.yml")
    for name in others:
        assert not (_INFRA / name).exists(), f"a second IaC tool appeared: infra/{name}"


def test_every_file_parses_into_balanced_blocks() -> None:
    """Not a substitute for `terraform validate` - it is what makes every assertion below
    trustworthy, because an unbalanced brace would silently merge two resources into one."""
    for name, text in _tf_text().items():
        blocks = _blocks(text)
        assert blocks, f"{name} declares nothing"
        for block in blocks:
            assert block.type in TOP_LEVEL_BLOCKS, (
                f"{name} declares an unexpected top-level block: {block.type}"
            )


def test_the_files_are_written_the_way_terraform_fmt_writes_them() -> None:
    """Only the two rules that are true of `terraform fmt` output regardless of alignment. The
    authority on formatting is `terraform fmt -check` in the `infra` CI job; this catches the
    edit that was made in a hurry."""
    for name, text in _tf_text().items():
        assert "\t" not in text, f"{name} contains a tab"
        assert text.endswith("\n"), f"{name} has no final newline"
        for number, line in enumerate(text.split("\n"), start=1):
            assert line == line.rstrip(), f"{name}:{number} has trailing whitespace"


# --- Nothing expensive, and nothing from the next block ------------------------------------


def test_no_forbidden_resource_type_appears() -> None:
    declared = {block.labels[0] for block in _all_blocks() if block.type == "resource"}
    for forbidden in FORBIDDEN_RESOURCE_TYPES:
        assert forbidden not in declared, f"{forbidden} is not part of Phase 5 block A"


def test_there_is_no_vpc_endpoint() -> None:
    """Interface endpoints charge per hour per AZ. The tasks reach SQS, S3 and ECR over the
    internet gateway they already have, which costs nothing extra."""
    declared = {block.labels[0] for block in _all_blocks() if block.type == "resource"}
    assert "aws_vpc_endpoint" not in declared


# --- The network ------------------------------------------------------------------------------


def test_there_are_two_availability_zones_for_the_services_that_require_two() -> None:
    """The ALB and both subnet groups require two AZs. The data resources inside them are
    deliberately Single-AZ, which is a different statement."""
    versions = _tf_text()["versions.tf"]
    assert "slice(data.aws_availability_zones.available.names, 0, 2)" in versions

    for kind in ("public", "private"):
        subnet = _resources("aws_subnet")[kind]
        assert _attribute(subnet.body, "count") == "length(local.azs)"


def test_only_the_public_route_table_reaches_the_internet() -> None:
    """One route, and it belongs to the public table. The private table having no route is what
    makes 'private' a fact rather than a name."""
    routes = _resources("aws_route")
    assert list(routes) == ["public_internet"], f"unexpected routes: {sorted(routes)}"

    route = routes["public_internet"]
    assert _attribute(route.body, "route_table_id") == "aws_route_table.public.id"
    assert _attribute(route.body, "gateway_id") == "aws_internet_gateway.main.id"


def test_the_data_stores_sit_in_the_private_subnets() -> None:
    for resource_type in ("aws_db_subnet_group", "aws_elasticache_subnet_group"):
        group = _one(resource_type)
        assert _attribute(group.body, "subnet_ids") == "aws_subnet.private[*].id"


def test_the_tasks_sit_in_the_public_subnets_with_a_public_ip() -> None:
    """The NAT-gateway trade, as it appears in the configuration: egress comes from a public IP
    and an internet gateway, and the security group is what makes that safe."""
    for name in ("api", "worker"):
        service = _resources("aws_ecs_service")[name]
        assert _attribute(service.body, "subnets") == "aws_subnet.public[*].id"
        assert _attribute(service.body, "assign_public_ip") == "true"


# --- Security groups ----------------------------------------------------------------------------


def _rules(direction: str) -> dict[str, Block]:
    return _resources(f"aws_vpc_security_group_{direction}_rule")


def test_security_groups_declare_no_inline_rules() -> None:
    """Every rule is its own resource, so a plan names the rule it is adding or removing and
    the tests below can read one relationship at a time."""
    for name, group in _resources("aws_security_group").items():
        assert "ingress {" not in group.body, f"{name} has an inline ingress block"
        assert "egress {" not in group.body, f"{name} has an inline egress block"


def test_the_load_balancer_is_the_only_thing_open_to_a_cidr() -> None:
    for name, rule in _rules("ingress").items():
        target = _attribute(rule.body, "security_group_id")
        cidr = _attribute(rule.body, "cidr_ipv4")
        if target == "aws_security_group.alb.id":
            assert cidr == "var.allowed_ingress_cidrs[count.index]"
            continue
        assert cidr is None, f"{name} opens {target} to an address range"


def test_the_api_is_reachable_only_from_the_load_balancer() -> None:
    api_ingress = [
        rule
        for rule in _rules("ingress").values()
        if _attribute(rule.body, "security_group_id") == "aws_security_group.api.id"
    ]
    assert len(api_ingress) == 1, "the API should have exactly one way in"

    rule = api_ingress[0]
    assert _attribute(rule.body, "referenced_security_group_id") == "aws_security_group.alb.id"
    assert _attribute(rule.body, "from_port") == "8000"
    assert _attribute(rule.body, "to_port") == "8000"


def test_nothing_can_reach_the_worker() -> None:
    """ARCHITECTURE.md section 19: the worker serves nothing and makes no authorization
    decision. No ingress rule is the strongest available statement of that."""
    for rule in _rules("ingress").values():
        assert _attribute(rule.body, "security_group_id") != "aws_security_group.worker.id"


def test_the_database_and_cache_accept_only_the_two_application_groups() -> None:
    for store, port in (("postgres", "5432"), ("redis", "6379")):
        rules = [
            rule
            for rule in _rules("ingress").values()
            if _attribute(rule.body, "security_group_id") == f"aws_security_group.{store}.id"
        ]
        referenced = {_attribute(rule.body, "referenced_security_group_id") for rule in rules}
        assert referenced == {"aws_security_group.api.id", "aws_security_group.worker.id"}
        for rule in rules:
            assert _attribute(rule.body, "from_port") == port
            assert _attribute(rule.body, "cidr_ipv4") is None


def test_the_data_stores_have_no_egress_rule() -> None:
    for rule in _rules("egress").values():
        target = _attribute(rule.body, "security_group_id")
        assert target not in ("aws_security_group.postgres.id", "aws_security_group.redis.id")


def test_the_api_has_no_plain_http_egress_and_the_worker_does() -> None:
    """The worker fetches research pages, and `tools/fetch.py` accepts http:// URLs. The API
    calls AWS service APIs and nothing else, so port 80 would be a rule with no requirement."""
    egress_ports: dict[str, set[str | None]] = {"api": set(), "worker": set()}
    for rule in _rules("egress").values():
        target = _attribute(rule.body, "security_group_id")
        for name in egress_ports:
            if target == f"aws_security_group.{name}.id":
                egress_ports[name].add(_attribute(rule.body, "from_port"))

    assert "80" not in egress_ports["api"]
    assert {"80", "443"} <= egress_ports["worker"]


# --- RDS ------------------------------------------------------------------------------------------


def test_the_database_is_private_encrypted_and_single_az() -> None:
    database = _one("aws_db_instance")

    assert _attribute(database.body, "publicly_accessible") == "false"
    assert _attribute(database.body, "multi_az") == "false"
    assert _attribute(database.body, "storage_encrypted") == "true"
    assert _attribute(database.body, "deletion_protection") == "false"
    assert _attribute(database.body, "vpc_security_group_ids") == "[aws_security_group.postgres.id]"
    assert _attribute(database.body, "db_subnet_group_name") == "aws_db_subnet_group.postgres.name"


def test_the_database_is_the_postgres_major_version_the_project_runs_locally() -> None:
    """16, matching `postgres:16-alpine` in docker-compose.yml - which is what makes the
    `postgres`-marked test layer evidence about this instance."""
    assert _attribute(_variables()["postgres_version"].body, "default") == '"16"'
    assert _attribute(_one("aws_db_instance").body, "engine") == '"postgres"'


def test_the_teardown_leaves_no_snapshot_or_backup_behind_by_default() -> None:
    """Both are storage that keeps charging after `terraform destroy` reports success."""
    database = _one("aws_db_instance")
    assert _attribute(database.body, "backup_retention_period") == "0"
    assert _attribute(database.body, "skip_final_snapshot") == "var.rds_skip_final_snapshot"
    assert _attribute(_variables()["rds_skip_final_snapshot"].body, "default") == "true"


# --- Redis -------------------------------------------------------------------------------------


def test_the_cache_is_one_private_node() -> None:
    cache = _one("aws_elasticache_cluster")

    assert _attribute(cache.body, "num_cache_nodes") == "1"
    assert _attribute(cache.body, "security_group_ids") == "[aws_security_group.redis.id]"
    assert _attribute(cache.body, "subnet_group_name") == "aws_elasticache_subnet_group.redis.name"
    assert _attribute(cache.body, "snapshot_retention_limit") == "0"


def test_the_cache_url_stays_the_plain_redis_scheme_the_application_builds() -> None:
    """A single node has no in-transit encryption, so `redis://` is correct and
    `redisstore.build_redis` needs no TLS argument. Production's replication group would make
    this `rediss://`, which is a code-free change but not a silent one."""
    assert 'redis_url = "redis://${aws_elasticache_cluster.redis' in _tf_text()["ecs.tf"]


# --- SQS -----------------------------------------------------------------------------------------


def test_both_queues_are_fifo_with_explicit_deduplication() -> None:
    """`jobqueue.py` sends its own MessageDeduplicationId - the idempotency key, or ADR 0007's
    gate-visit key. Content-based deduplication would hash the body instead and drop a
    legitimate second gate visit."""
    for name in ("jobs", "jobs_dlq"):
        queue = _resources("aws_sqs_queue")[name]
        assert _attribute(queue.body, "fifo_queue") == "true"
        assert _attribute(queue.body, "content_based_deduplication") == "false"

        queue_name = _attribute(queue.body, "name")
        assert queue_name is not None and queue_name.endswith('.fifo"')


def test_the_queue_keeps_the_visibility_window_the_worker_derives_its_lease_from() -> None:
    """ADR 0015: the worker renews every one third of this. `worker.check_queue` refuses a queue
    whose window cannot fit that cadence, so a smaller number here is a worker that will not
    start."""
    queue = _resources("aws_sqs_queue")["jobs"]
    assert _attribute(queue.body, "visibility_timeout_seconds") == "1800"

    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    local = compose["services"]["localstack"]["environment"]["JOBS_QUEUE_VISIBILITY_TIMEOUT"]
    assert int(local) == 1800, "AWS and LocalStack disagree about the visibility window"


def test_three_deliveries_then_the_dead_letter_queue() -> None:
    queue = _resources("aws_sqs_queue")["jobs"]
    policy = _attribute(queue.body, "maxReceiveCount")
    assert policy == "3"
    assert "aws_sqs_queue.jobs_dlq.arn" in queue.body


# --- S3 -------------------------------------------------------------------------------------------


def test_the_report_bucket_blocks_public_access_four_ways() -> None:
    block = _one("aws_s3_bucket_public_access_block")
    for setting in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert _attribute(block.body, setting) == "true", f"{setting} is not on"


def test_nothing_grants_the_bucket_a_policy_or_an_acl() -> None:
    declared = {block.labels[0] for block in _all_blocks() if block.type == "resource"}
    assert "aws_s3_bucket_policy" not in declared
    assert "aws_s3_bucket_acl" not in declared
    assert 'acl = "public-read"' not in _tf_text()["data_stores.tf"]


def test_the_bucket_is_encrypted_and_nothing_expires_the_reports() -> None:
    """A lifecycle rule that deleted objects on a schedule could delete the evidence before the
    screenshots were taken. Aborting unfinished multipart uploads is the only rule here."""
    assert _one("aws_s3_bucket_server_side_encryption_configuration")

    lifecycle = _one("aws_s3_bucket_lifecycle_configuration")
    assert "abort_incomplete_multipart_upload" in lifecycle.body
    assert "expiration" not in lifecycle.body


# --- ALB ------------------------------------------------------------------------------------------


def test_only_the_api_is_behind_the_load_balancer() -> None:
    assert "load_balancer {" in _resources("aws_ecs_service")["api"].body
    assert "load_balancer {" not in _resources("aws_ecs_service")["worker"].body


def test_the_health_check_is_the_applications_own_and_accepts_only_a_healthy_answer() -> None:
    """`/health` answers 503 when the database, Redis or the checkpoint store cannot be reached.
    A matcher that accepted 503 would report a deployment healthy in which no job can run."""
    target_group = _one("aws_lb_target_group")

    assert _attribute(target_group.body, "path") == '"/health"'
    assert _attribute(target_group.body, "matcher") == '"200"'
    assert _attribute(target_group.body, "port") == "8000"
    assert _attribute(target_group.body, "target_type") == '"ip"'


def test_the_listener_forwards_to_the_api_target_group() -> None:
    listener = _one("aws_lb_listener")
    assert _attribute(listener.body, "port") == "80"
    assert _attribute(listener.body, "target_group_arn") == "aws_lb_target_group.api.arn"


def test_the_api_service_is_given_time_to_wait_for_the_first_worker() -> None:
    """`checks.checkpoints` is false until a worker calls `setup()`, so the API is legitimately
    unhealthy at first. The grace period stops ECS killing the task for it; it does not make the
    ALB route traffic, and it does not weaken the check."""
    service = _resources("aws_ecs_service")["api"]
    assert _attribute(service.body, "health_check_grace_period_seconds") == "300"


# --- ECS ------------------------------------------------------------------------------------------


def test_one_image_carries_all_three_entrypoints() -> None:
    """The same claim `test_container_image.py` makes about Compose, in the deployment."""
    assert len(_resources("aws_ecr_repository")) == 1
    for name in ("api", "worker", "migrate"):
        definition = _resources("aws_ecs_task_definition")[name]
        assert _attribute(definition.body, "image") == "local.image"


def test_the_three_commands_are_the_ones_the_project_already_runs() -> None:
    expected = {
        "api": '["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]',
        "worker": '["python", "-m", "worker"]',
        "migrate": '["alembic", "upgrade", "head"]',
    }
    for name, command in expected.items():
        definition = _resources("aws_ecs_task_definition")[name]
        assert _attribute(definition.body, "command") == command


def test_the_worker_gets_fargates_maximum_graceful_stop_opportunity() -> None:
    """ARCHITECTURE.md section 19 requires the task definition to set 120, matching
    `stop_grace_period: 120s` in docker-compose.yml. The default is 30, and the two numbers have
    to agree or local shutdown stops predicting deployed shutdown."""
    assert _attribute(_resources("aws_ecs_task_definition")["worker"].body, "stopTimeout") == "120"

    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["worker"]["stop_grace_period"] == "120s"


def test_the_migration_is_a_task_and_never_a_service() -> None:
    """guidelines section 19: `alembic upgrade head` runs as a one-off that must exit 0 before
    anything relies on the schema, and neither long-running process migrates."""
    assert "migrate" in _resources("aws_ecs_task_definition")
    assert set(_resources("aws_ecs_service")) == {"api", "worker"}

    for name in ("api", "worker"):
        definition = _resources("aws_ecs_task_definition")[name]
        assert "alembic" not in definition.body


def test_the_migration_task_is_given_nothing_but_a_database() -> None:
    """No queue, no bucket, no provider credential, and no task role at all - a migration can
    reach nothing that could re-bill a model."""
    definition = _resources("aws_ecs_task_definition")["migrate"]

    assert _attribute(definition.body, "task_role_arn") is None
    assert "local.common_environment" not in definition.body
    assert "DATABASE_URL" in definition.body
    for variable in ("SQS_QUEUE_URL", "S3_BUCKET", *PROVIDER_VARIABLES):
        assert variable not in definition.body


def test_the_api_task_carries_no_llm_or_search_credential() -> None:
    """ADR 0012 as configuration rather than as intent. The API starts, serves all six routes
    and passes its health check with no provider credential in its environment."""
    definition = _resources("aws_ecs_task_definition")["api"]
    for variable in PROVIDER_VARIABLES:
        assert variable not in definition.body, f"the API task definition sets {variable}"


def test_the_worker_is_the_process_that_holds_the_provider_credentials() -> None:
    definition = _resources("aws_ecs_task_definition")["worker"]
    for variable in PROVIDER_VARIABLES:
        assert variable in definition.body


def test_neither_service_is_scaled_beyond_what_the_llm_tier_allows() -> None:
    """ARCHITECTURE.md section 11: worker count is bounded by the rate limit, not by queue
    depth, and autoscaling is deliberately absent."""
    for name in ("api_desired_count", "worker_desired_count"):
        assert _attribute(_variables()[name].body, "default") == "1"


# --- Configuration that must not cross over from the local stack ----------------------------------


def test_no_localstack_endpoint_reaches_the_deployment() -> None:
    """`artifacts.presign` signs a host into a SigV4 signature, so the address has to be right
    before the URL exists. Against AWS the default endpoint is the right one."""
    for name, text in _tf_code().items():
        for variable in LOCALSTACK_ONLY_VARIABLES:
            assert variable not in text, f"{name} sets {variable}, which is LocalStack-only"
        assert "localstack" not in text.lower(), f"{name} names LocalStack"
        assert "4566" not in text, f"{name} names the LocalStack port"


def test_the_application_tunables_are_not_duplicated_here() -> None:
    """They have defaults in config.py and keep them. A second copy in Terraform is a second
    source of truth, and the two drift silently."""
    duplicated = (
        "MAX_REVISIONS",
        "MAX_LLM_CALLS_PER_JOB",
        "MAX_JOB_RUNTIME",
        "MAX_SUPERVISOR_HOPS",
        "LLM_RPM_LIMIT",
        "REFLECTION_PASS_THRESHOLD",
        "RESEARCHER_CONCURRENCY",
    )
    joined = "\n".join(_tf_code().values())
    for name in duplicated:
        assert name not in joined, f"{name} belongs to config.py, not to Terraform"


# --- Nothing in here is a credential, and nothing names one account -------------------------------


def test_no_aws_account_id_is_written_down() -> None:
    """A twelve-digit literal is an account id, and an account id in a repository is a hint
    nobody needs to publish. Every ARN here comes from a resource attribute."""
    for name, text in _tf_code().items():
        for line in text.split("\n"):
            assert re.search(r"\b\d{12}\b", line) is None, f"{name}: {line.strip()}"


def test_the_provider_takes_no_credential_and_the_region_is_a_variable() -> None:
    provider = next(block for block in _all_blocks() if block.type == "provider")

    assert _attribute(provider.body, "region") == "var.region"
    for forbidden in ("access_key", "secret_key", "token", "profile"):
        assert _attribute(provider.body, forbidden) is None

    for name, text in _tf_code().items():
        if name == "variables.tf":
            continue
        assert "ap-south-1" not in text, f"{name} hard-codes a region"


def test_every_secret_variable_is_declared_without_a_default() -> None:
    """`terraform apply` refuses to run without them, so no credential can arrive by being
    forgotten - and none of them can be committed, because none of them is written down."""
    for name in ("db_password", "llm_api_key", "tavily_api_key", "auth_keys"):
        variable = _variables()[name]
        assert _attribute(variable.body, "sensitive") == "true", f"{name} is not marked sensitive"
        assert _attribute(variable.body, "default") is None, f"{name} has a default"


def test_no_output_would_print_a_secret() -> None:
    outputs = {block.labels[0]: block for block in _all_blocks() if block.type == "output"}
    for name, block in outputs.items():
        value = _attribute(block.body, "value") or ""
        for secret in ("password", "api_key", "auth_keys", "database_url"):
            assert secret not in value, f"output {name} would print {secret}"


def test_every_output_the_runbook_needs_exists() -> None:
    outputs = {block.labels[0] for block in _all_blocks() if block.type == "output"}
    missing = [name for name in REQUIRED_OUTPUTS if name not in outputs]
    assert not missing, f"missing outputs: {missing}"


def test_the_state_and_the_filled_in_variables_are_gitignored() -> None:
    """State holds the database password and the whole resource graph; terraform.tfvars is
    where the credentials go, the way .env is."""
    ignored = (_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("*.tfstate", "*.tfvars", "infra/.terraform/"):
        assert pattern in ignored, f"{pattern} is not gitignored"
    assert "!*.tfvars.example" in ignored


def test_the_example_variables_file_carries_no_real_value() -> None:
    example = (_INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    for assignment in re.findall(r"^\s*(\w+)\s*=\s*(.+)$", example, re.MULTILINE):
        name, value = assignment
        assert "REPLACE" in value, f"{name} in terraform.tfvars.example is not a placeholder"


def test_the_infrastructure_is_left_out_of_the_container_build_context() -> None:
    assert "\ninfra\n" in (_ROOT / ".dockerignore").read_text(encoding="utf-8")


# --- CI stays AWS-account-independent -------------------------------------------------------------


def test_ci_validates_the_infrastructure_without_an_aws_account() -> None:
    job = _workflow()["jobs"]["infra"]
    commands = " ".join(str(step.get("run", "")) for step in job["steps"])

    assert "terraform -chdir=infra fmt -check" in commands
    assert "init -backend=false" in commands
    assert "terraform -chdir=infra validate" in commands


def test_ci_never_plans_or_applies() -> None:
    """A plan reads real account state, which would make this job need credentials and would
    make CI able to change infrastructure. Deployment is a person's decision."""
    workflow = _workflow()
    for name, job in workflow["jobs"].items():
        commands = " ".join(str(step.get("run", "")) for step in job["steps"])
        uses = " ".join(str(step.get("uses", "")) for step in job["steps"])
        for forbidden in ("terraform apply", "terraform plan", "terraform destroy"):
            assert forbidden not in commands, f"the {name} job runs `{forbidden}`"
        assert "configure-aws-credentials" not in uses, f"the {name} job assumes an AWS role"


EXISTING_CI_JOBS = ("quality", "unit", "eval", "postgres", "redis", "integration", "container")


@pytest.mark.parametrize("name", EXISTING_CI_JOBS)
def test_every_existing_ci_job_is_still_present(name: str) -> None:
    """The infra job is an addition. Nothing about Phase 3 or Phase 4 verification changes."""
    assert name in _workflow()["jobs"]


def test_the_deployment_runbook_exists() -> None:
    """The stop condition for this block: infrastructure nobody can deploy, verify or tear down
    from a written procedure is not finished."""
    runbook = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for section in ("## 4. Deploy", "## 5. Verify", "## 6. Teardown", "## 7. Cost"):
        assert section in runbook, f"docs/deployment.md has no {section} section"

    # The teardown is only finished if it says what to look for afterwards.
    for orphan in ("describe-db-snapshots", "describe-nat-gateways", "describe-addresses"):
        assert orphan in runbook, f"the teardown checklist does not check for {orphan}"


def test_the_ecr_lifecycle_policy_expires_old_images() -> None:
    """Without it, every pushed image is stored until someone deletes it by hand. `force_delete`
    on the repository is what makes `terraform destroy` remove them; this is what keeps the count
    small while the deployment is alive."""
    policy = _one("aws_ecr_lifecycle_policy")

    assert _attribute(policy.body, "rulePriority") == "1"
    assert _attribute(policy.body, "countType") == '"imageCountMoreThan"'
    assert _attribute(policy.body, "countNumber") == "5"
    assert _attribute(policy.body, "type") == '"expire"'
    assert _attribute(_one("aws_ecr_repository").body, "force_delete") == "true"
