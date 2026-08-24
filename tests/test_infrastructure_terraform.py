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
    # Block B deliberately creates neither a domain nor a certificate: a public ACM certificate
    # needs a validated domain, and a hosted zone charges per month and outlives the deployment
    # (ADR 0020 decision 5). An operator supplies an existing certificate ARN instead.
    "aws_acm_certificate",
    "aws_route53_zone",
    "aws_route53_record",
    # A customer-managed key is a per-month charge and two more grants, for encryption the
    # AWS-managed keys already provide here (ADR 0020 decision 6).
    "aws_kms_key",
    # Autoscaling stays absent through block C too: worker count is bounded by the LLM rate
    # limit rather than by queue depth (ARCHITECTURE.md section 11), so scaling on the very
    # metric the backlog alarm watches would be the wrong response to it.
    "aws_appautoscaling_target",
    "aws_appautoscaling_policy",
    # Block C's own "did not build" list (docs/adr/0021-*.md decision 7). Each would be an
    # always-on operational service for an environment that lives an hour, doing work three
    # deterministic scripts already do on demand - and each is a one-line addition that no other
    # test would notice.
    "aws_cloudwatch_dashboard",
    "aws_lambda_function",
    "aws_cloudwatch_event_rule",
    "aws_cloudwatch_event_target",
    "aws_sfn_state_machine",
    "aws_xray_sampling_rule",
    # An email address in a repository, and a confirmation mail on every apply. The topic is
    # created; subscribing to it is one operator command (monitoring.tf).
    "aws_sns_topic_subscription",
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


def test_the_http_listener_serves_when_there_is_no_certificate_and_redirects_when_there_is() -> (
    None
):
    """ADR 0020 decision 5. Both branches exist in one listener, and only one is ever live: an
    HTTP listener that kept forwarding beside an HTTPS one would leave the plain path open."""
    listener = _resources("aws_lb_listener")["http"]

    assert _attribute(listener.body, "port") == "80"
    assert _attribute(listener.body, "target_group_arn") == "aws_lb_target_group.api.arn"
    assert "for_each = local.https_enabled ? [1] : []" in listener.body
    assert "for_each = local.https_enabled ? [] : [1]" in listener.body
    assert '"redirect"' in listener.body


def test_the_https_listener_exists_only_when_a_certificate_is_supplied() -> None:
    """Nothing here creates a certificate, and the default is no certificate at all - so the
    deployment is HTTP unless an operator brings one (ADR 0020 decision 5)."""
    listener = _resources("aws_lb_listener")["https"]

    assert _attribute(listener.body, "count") == "local.https_enabled ? 1 : 0"
    assert _attribute(listener.body, "port") == "443"
    assert _attribute(listener.body, "protocol") == '"HTTPS"'
    assert _attribute(listener.body, "certificate_arn") == "var.certificate_arn"
    assert _attribute(listener.body, "target_group_arn") == "aws_lb_target_group.api.arn"

    policy = _attribute(listener.body, "ssl_policy") or ""
    assert "TLS13-1-2" in policy, "the TLS floor must be 1.2, not the 2016 default"

    assert _attribute(_variables()["certificate_arn"].body, "default") == '""'


def test_https_is_the_only_thing_that_opens_a_second_port_on_the_load_balancer() -> None:
    """The 443 ingress rule exists exactly when the listener behind it does. An open port with
    no listener is a rule that describes nothing."""
    rule = _rules("ingress")["alb_from_internet_tls"]

    assert (
        _attribute(rule.body, "count")
        == "local.https_enabled ? length(var.allowed_ingress_cidrs) : 0"
    )
    assert _attribute(rule.body, "from_port") == "443"


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
    assert _attribute(definition.body, "environment") == "local.database_environment"
    assert _attribute(definition.body, "secrets") == "local.database_secrets"
    for variable in ("SQS_QUEUE_URL", "S3_BUCKET", "AUTH_KEYS", *PROVIDER_VARIABLES):
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


# --- Block B: secrets never reach a task definition in plaintext ----------------------------------


SECRET_VARIABLE_NAMES = ("LLM_API_KEY", "TAVILY_API_KEY", "AUTH_KEYS", "DB_PASSWORD", "DB_USER")
"""The five values that must arrive through `secrets`/`valueFrom` and never through
`environment`. Anything in `environment` is returned in full by `ecs:DescribeTaskDefinition`,
and a task definition revision outlives the deployment it belonged to."""


def _container_locals() -> str:
    """The `locals` block in ecs.tf, where the environment and secret lists are assembled."""
    return next(block.body for block in _blocks(_tf_text()["ecs.tf"]) if block.type == "locals")


def test_no_credential_is_assembled_into_a_plaintext_environment_list() -> None:
    """The check that would fail if a secret were moved back inline. Every `{ name = "X", value
    = ... }` pair in ecs.tf is read, and none of them may name a credential."""
    plaintext = re.findall(r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*value\s*=', _tf_code()["ecs.tf"])

    for variable in SECRET_VARIABLE_NAMES:
        assert variable not in plaintext, f"{variable} is a plaintext environment entry"


def test_every_credential_arrives_through_a_secrets_manager_reference() -> None:
    secret_refs = dict(
        re.findall(
            r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*valueFrom\s*=\s*(.+?)\s*\}',
            _tf_code()["ecs.tf"],
        )
    )

    for variable in SECRET_VARIABLE_NAMES:
        assert variable in secret_refs, f"{variable} has no valueFrom reference"
        assert "secret" in secret_refs[variable], f"{variable} does not name a secret"


def test_the_database_credential_comes_from_the_secret_rds_owns() -> None:
    """Not a secret Terraform wrote - the one RDS generated, which is the whole point: the value
    exists only inside AWS and no plan, apply or state file has ever held it."""
    locals_body = _container_locals()

    assert "aws_db_instance.postgres.master_user_secret[0].secret_arn" in _tf_code()["iam.tf"]
    assert "${local.db_secret_arn}:username::" in locals_body
    assert "${local.db_secret_arn}:password::" in locals_body


def test_the_api_receives_no_provider_secret_and_the_worker_receives_no_key_table() -> None:
    """The ADR 0012 boundary as IAM and as configuration at once. The API cannot read the LLM
    key, and the worker - which makes no authorization decision - cannot read the key table."""
    api = _resources("aws_ecs_task_definition")["api"].body
    worker = _resources("aws_ecs_task_definition")["worker"].body

    for variable in (*PROVIDER_VARIABLES, "llm_api_key", "tavily_api_key"):
        assert variable not in api, f"the API task definition names {variable}"

    for name in ("AUTH_KEYS", "auth_keys", "COGNITO"):
        assert name not in worker, f"the worker task definition names {name}"


def test_secrets_are_deleted_rather_than_scheduled_when_the_deployment_is_destroyed() -> None:
    """A scheduled secret still appears after teardown, and blocks re-creating the same name -
    which is how the second run of a demo fails."""
    assert "secret_recovery_window_days = 0" in _tf_code()["secrets.tf"]

    for secret in _resources("aws_secretsmanager_secret").values():
        window = _attribute(secret.body, "recovery_window_in_days")
        assert window == "local.secret_recovery_window_days", window


def test_the_key_table_secret_exists_only_in_the_mode_that_uses_it() -> None:
    secret = _resources("aws_secretsmanager_secret")["auth_keys"]
    assert _attribute(secret.body, "count") == "local.cognito_enabled ? 0 : 1"


# --- Block B: IAM is separated, narrowed, and trusted only by this account's ECS ------------------


def test_each_task_definition_has_its_own_execution_role() -> None:
    """One shared execution role would have to be granted every secret all three tasks use,
    which would hand the API's task-start identity the LLM key - undoing ADR 0012 in IAM while
    the task definition still looked clean."""
    role = _resources("aws_iam_role")["execution"]
    assert _attribute(role.body, "for_each") == "local.execution_secret_arns"

    for name in ("api", "worker", "migrate"):
        definition = _resources("aws_ecs_task_definition")[name]
        assert (
            _attribute(definition.body, "execution_role_arn")
            == f'aws_iam_role.execution["{name}"].arn'
        )


def test_each_execution_role_may_fetch_only_its_own_secrets() -> None:
    """`secretsmanager:GetSecretValue` on a named list, never a wildcard, and never
    `DescribeSecret` - ECS needs the value and nothing about the secret."""
    policy = _resources("aws_iam_role_policy")["execution_secrets"]

    assert _attribute(policy.body, "Action") == '["secretsmanager:GetSecretValue"]'
    assert _attribute(policy.body, "Resource") == "each.value"
    assert "secretsmanager:*" not in policy.body
    assert "DescribeSecret" not in policy.body


def test_the_api_execution_role_gets_no_provider_secret() -> None:
    """The table in iam.tf, read as code: the API's list is the database credential, plus the
    key table only in the mode that has one."""
    locals_body = next(
        block.body for block in _blocks(_tf_text()["iam.tf"]) if block.type == "locals"
    )
    api_entry = locals_body.split("api =")[1].split("worker =")[0]

    assert "llm_api_key" not in api_entry
    assert "tavily_api_key" not in api_entry
    assert "db_secret_arn" in api_entry


def test_the_application_itself_is_given_no_secrets_manager_permission() -> None:
    """The application reads environment variables, exactly as it does locally. Nothing in
    either container calls Secrets Manager, so neither task role can - a compromised worker
    process cannot fetch the auth table because it cannot fetch anything."""
    for name in ("api", "worker"):
        document = next(
            block
            for block in _all_blocks()
            if block.type == "data" and block.labels == ("aws_iam_policy_document", name)
        )
        assert "secretsmanager" not in document.body


def test_no_task_or_execution_policy_uses_a_wildcard_action_or_resource() -> None:
    """`AmazonECSTaskExecutionRolePolicy` is AWS's own and does use `Resource: "*"` for
    `ecr:GetAuthorizationToken`, which is an account-level call with no resource to scope to.
    Everything this repository writes is named."""
    written = "\n".join(
        block.body
        for block in _all_blocks()
        if (block.type == "data" and block.labels[:1] == ("aws_iam_policy_document",))
        or (block.type == "resource" and block.labels[:1] == ("aws_iam_role_policy",))
    )

    for wildcard in ('"s3:*"', '"sqs:*"', '"secretsmanager:*"', '"*"'):
        assert wildcard not in written, f"a policy grants {wildcard}"


def test_the_task_roles_are_trusted_only_by_this_accounts_ecs() -> None:
    """Block A trusted `ecs-tasks.amazonaws.com` unconditionally, which lets ECS in any account
    ask to assume these roles on behalf of a task there. These two conditions are AWS's
    documented confused-deputy fix."""
    document = next(
        block
        for block in _all_blocks()
        if block.type == "data" and block.labels == ("aws_iam_policy_document", "ecs_tasks_assume")
    )

    assert "aws:SourceAccount" in document.body
    assert "aws:SourceArn" in document.body
    assert "data.aws_caller_identity.current.account_id" in document.body

    for role in _resources("aws_iam_role").values():
        assert (
            _attribute(role.body, "assume_role_policy")
            == "data.aws_iam_policy_document.ecs_tasks_assume.json"
        )


# --- Block B: Cognito is minimal, and the API is what validates the token -------------------------


def test_there_is_one_user_pool_one_client_and_two_groups() -> None:
    assert set(_resources("aws_cognito_user_pool")) == {"main"}
    assert set(_resources("aws_cognito_user_pool_client")) == {"api"}
    assert set(_resources("aws_cognito_user_group")) == {"reviewer", "submitter"}, (
        "the pool's groups are the two roles routes/auth.py has had since Phase 2"
    )


def test_cognito_exists_only_when_the_deployment_asks_for_it() -> None:
    for resource_type in (
        "aws_cognito_user_pool",
        "aws_cognito_user_pool_client",
        "aws_cognito_user_group",
    ):
        for name, block in _resources(resource_type).items():
            assert _attribute(block.body, "count") == "local.cognito_enabled ? 1 : 0", name


def test_the_app_client_is_public_and_holds_no_client_secret() -> None:
    """A client secret a curl script has to carry would be exactly the shared secret Cognito is
    replacing. What secures this flow is the user's password and the token's signature."""
    client = _resources("aws_cognito_user_pool_client")["api"]

    assert _attribute(client.body, "generate_secret") == "false"
    assert _attribute(client.body, "prevent_user_existence_errors") == '"ENABLED"'
    assert _attribute(client.body, "access_token_validity") == "1"
    assert _attribute(client.body, "access_token") == '"hours"'


def test_the_pool_adds_no_hosted_ui_federation_or_lambda_trigger() -> None:
    """Each would be a resource to tear down and a claim to explain, and nothing here needs
    one: the API takes a bearer token and serves no browser."""
    cognito = _tf_code()["cognito.tf"]

    for absent in (
        "callback_urls",
        "logout_urls",
        "allowed_oauth_flows",
        "lambda_config",
        "aws_cognito_identity_provider",
        "aws_cognito_user_pool_domain",
    ):
        assert absent not in cognito, f"cognito.tf configures {absent}"


def test_the_load_balancer_does_not_authenticate() -> None:
    """ADR 0020 decision 4: ALB's `authenticate-cognito` action is a browser redirect flow with
    a session cookie, which is wrong for an API whose callers are scripts. The API validates the
    bearer token itself, which is what kept the request contract identical."""
    for listener in _resources("aws_lb_listener").values():
        assert "authenticate-cognito" not in listener.body
        assert "authenticate_cognito" not in listener.body


def test_the_api_is_told_which_pool_to_trust_in_plain_environment() -> None:
    """A signing key, an issuer and a client id are published by design, so putting them in
    Secrets Manager would buy nothing and cost a fetch at every task start."""
    locals_body = _container_locals()

    for name in ("AUTH_MODE", "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_REGION"):
        assert f'name = "{name}", value' in locals_body, f"{name} is not plain environment"


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
    source of truth, and the two drift silently.

    **Four exceptions, and they are named in `WORKER_RUNTIME_BOUNDS` below rather than here.**
    Those four describe the deployed endpoint rather than a preference, and the tests that
    follow pin what makes them safe: they are variables, they carry the measured defaults, and
    they reach the worker and nothing else.
    """
    duplicated = (
        "MAX_LLM_CALLS_PER_JOB",
        "MAX_REVIEWER_EDITS",
        "LLM_RPM_LIMIT",
        "REFLECTION_PASS_THRESHOLD",
        "RESEARCHER_CONCURRENCY",
        "MAX_FETCH_BYTES",
        "MAX_PAGE_CHARS",
        "STALE_JOB_MIN_AGE_SECONDS",
    )
    joined = "\n".join(_tf_code().values())
    for name in duplicated:
        assert name not in joined, f"{name} belongs to config.py, not to Terraform"


# --- W1: the four runtime bounds the deployed worker states explicitly ----------------------
#
# The audit finding this closes: the worker ran on `config.py`'s defaults (60 / 2 / 24 / 1200),
# which are not the values either published n=20 baseline was measured at, and there was no
# `-var` that could change the request timeout without editing ecs.tf mid-deployment.

WORKER_RUNTIME_BOUNDS: tuple[tuple[str, str, str], ...] = (
    ("LLM_MAIN_TIMEOUT_S", "llm_main_timeout_s", "180"),
    ("MAX_REVISIONS", "max_revisions", "3"),
    ("MAX_SUPERVISOR_HOPS", "max_supervisor_hops", "30"),
    ("MAX_JOB_RUNTIME", "max_job_runtime", "1800"),
)
"""Each row is (environment variable, Terraform variable, the default this deployment runs)."""


@pytest.mark.parametrize(("env_name", "var_name", "default"), WORKER_RUNTIME_BOUNDS)
def test_each_worker_runtime_bound_is_a_variable_with_the_measured_default(
    env_name: str, var_name: str, default: str
) -> None:
    """**A number, a type, and no secret.** These are configuration an operator may want to move
    on the day, so each is a `variable` with a default rather than a literal in a task
    definition - `TF_VAR_llm_main_timeout_s=90` has to be enough to change one.

    The defaults are 180 / 3 / 30 / 1800 because that is what both published baselines ran at
    (`docs/engineering-guidelines.md` section 14's measurement context), not because they are
    tuned. `config.py` keeps 60 / 2 / 24 / 1200 and this file does not touch it.
    """
    declared = _variables()
    assert var_name in declared, f"{env_name} is set from no Terraform variable"
    body = declared[var_name].body
    assert _attribute(body, "type") == "number", f"{var_name} must be a number"
    assert _attribute(body, "default") == default, (
        f"{var_name} must default to {default}, the value the measured runs used"
    )
    assert _attribute(body, "sensitive") is None, f"{var_name} is configuration, not a secret"


def test_the_worker_receives_all_four_runtime_bounds_from_those_variables() -> None:
    """The whole path in one assertion: Terraform variable -> worker task environment -> the
    `config.py` parsing that already existed. No application module was changed to carry them.

    `tostring` because a container environment value is a string and the variables are numbers;
    without it Terraform refuses the task definition rather than deploying a wrong one.
    """
    worker = _resources("aws_ecs_task_definition")["worker"]
    for env_name, var_name, _default in WORKER_RUNTIME_BOUNDS:
        entry = f'{{ name = "{env_name}", value = tostring(var.{var_name}) }}'
        assert entry in worker.body, f"the worker task definition does not set {env_name}"


@pytest.mark.parametrize("family", ["api", "migrate", "ops"])
def test_no_other_task_receives_a_worker_only_runtime_bound(family: str) -> None:
    """**The worker is the only process that runs a graph node**, so it is the only one any of
    these four describes. An API told `MAX_JOB_RUNTIME` would be documenting behaviour it does
    not have, and a reader would reasonably believe it."""
    definition = _resources("aws_ecs_task_definition")[family]
    for env_name, _var_name, _default in WORKER_RUNTIME_BOUNDS:
        assert env_name not in definition.body, (
            f"the {family} task definition names {env_name}, which only the worker acts on"
        )


def test_the_runtime_bounds_changed_no_application_default() -> None:
    """**Terraform states the deployment's values; it does not edit the application's.** If a
    number here ever became a `config.py` default too, the local suite and the deployment would
    have two sources of truth for the same bound - which is the thing
    `test_the_application_tunables_are_not_duplicated_here` exists to prevent."""
    config_source = (_ROOT / "config.py").read_text(encoding="utf-8")
    for env_name, expected in (
        ("LLM_MAIN_TIMEOUT_S", "60.0"),
        ("MAX_REVISIONS", "2"),
        ("MAX_SUPERVISOR_HOPS", "24"),
        ("MAX_JOB_RUNTIME", "1200"),
    ):
        found = re.search(
            rf'_(?:int|float)\(source, "{env_name}", default=(?P<value>[^)]+)\)', config_source
        )
        assert found is not None, f"config.py no longer parses {env_name}"
        assert found.group("value") == expected, (
            f"config.py's {env_name} default moved to {found.group('value')}; the local default "
            "is deliberately not the deployed one"
        )


def test_the_job_runtime_bound_is_not_coupled_to_the_visibility_window() -> None:
    """**They share the value 1800 and nothing else, and this is the test that says so.**

    `MAX_JOB_RUNTIME` is a `time.monotonic()` deadline checked after a node's checkpoint is
    durable; ownership of the delivery is kept by ADR 0015's background heartbeat, which renews
    every `V/3` derived from the queue's own attribute. `worker.check_queue` compares the
    visibility timeout with the renewal cadence and the bounded SQS call, and never with this.

    So the queue's window must still be declared by the queue, and this variable must not be
    what sets it - which is what would silently turn a runtime change into a lease change.
    """
    queue = _resources("aws_sqs_queue")["jobs"]
    assert _attribute(queue.body, "visibility_timeout_seconds") == "1800", (
        "the visibility window is ADR 0015's and is unchanged by the W1 runtime bounds"
    )
    assert "var.max_job_runtime" not in queue.body, (
        "the queue's lease must not be derived from a worker runtime bound"
    )
    assert (
        "MAX_JOB_RUNTIME"
        not in _resources("aws_cloudwatch_metric_alarm")["jobs_queue_backlog_age"].body
    ), "the queue-age alarm reads the queue, not the worker's deadline"


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
    forgotten - and none of them can be committed, because none of them is written down.

    `auth_keys` is the one exception and is checked separately below: under Cognito there is no
    key table at all, so requiring a value would demand a credential the deployment never reads.
    """
    for name in ("llm_api_key", "tavily_api_key"):
        variable = _variables()[name]
        assert _attribute(variable.body, "sensitive") == "true", f"{name} is not marked sensitive"
        assert _attribute(variable.body, "default") is None, f"{name} has a default"


def test_the_key_table_is_still_required_in_the_mode_that_reads_it() -> None:
    """Its default is empty because Cognito needs none, and a validation is what stops an
    `api_key` deployment starting with no table - which would authenticate nobody."""
    variable = _variables()["auth_keys"]

    assert _attribute(variable.body, "sensitive") == "true"
    assert _attribute(variable.body, "default") == '""'
    assert 'var.auth_mode == "cognito" || trimspace(var.auth_keys) != ""' in variable.body


def test_there_is_no_database_password_variable_at_all() -> None:
    """ADR 0020 decision 1, and the largest single reduction in what `terraform.tfstate` holds:
    RDS generates the master password into a secret it owns, so no password value ever passes
    through Terraform to be recorded."""
    assert "db_password" not in _variables()

    database = _one("aws_db_instance")
    assert _attribute(database.body, "manage_master_user_password") == "true"
    assert _attribute(database.body, "password") is None

    joined = "\n".join(_tf_code().values())
    assert "var.db_password" not in joined


SECRET_BEARING_EXPRESSIONS = (
    "var.llm_api_key",
    "var.tavily_api_key",
    "var.auth_keys",
    "secret_string",
    "aws_secretsmanager_secret_version",
    "master_user_secret[0].secret_string",
)
"""Expressions that resolve to a credential rather than to a name for one. An ARN is neither -
it is what `put-secret-value` and the teardown checklist take, and reading one grants nothing."""


def test_no_output_would_print_a_secret() -> None:
    outputs = {block.labels[0]: block for block in _all_blocks() if block.type == "output"}
    for name, block in outputs.items():
        value = _attribute(block.body, "value") or block.body
        for secret in ("password", *SECRET_BEARING_EXPRESSIONS):
            assert secret not in value, f"output {name} would print {secret}"


def test_no_output_is_marked_sensitive_because_none_needs_to_be() -> None:
    """A `sensitive` output is one that holds a secret and is being hidden. The rule here is
    stronger: an output must not hold one, so none of them needs hiding."""
    for block in _all_blocks():
        if block.type == "output":
            assert _attribute(block.body, "sensitive") is None, block.labels[0]


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


# --- Block C: six alarms, each one explainable ----------------------------------------------------

ALARMS = (
    "dlq_not_empty",
    "jobs_queue_backlog_age",
    "api_unhealthy_targets",
    "api_target_5xx",
    "rds_free_storage_low",
    "redis_memory_pressure",
)
"""The whole alarm set. Six is a decision in both directions: fewer would leave the dead-letter
queue or the worker unwatched, and a page of them would be a set nobody reads."""


def test_the_alarm_set_is_exactly_the_six_that_were_justified() -> None:
    """A seventh alarm is not free - it is another thing to explain, another threshold to defend
    and another source of noise. This fails when one is added or removed without the reasoning
    in monitoring.tf changing with it."""
    assert set(_resources("aws_cloudwatch_metric_alarm")) == set(ALARMS)


@pytest.mark.parametrize("name", ALARMS)
def test_every_alarm_says_what_to_do_about_it(name: str) -> None:
    """An alarm whose description does not tell you where to look is a notification, not an
    alarm. Every one of them names the runbook."""
    alarm = _resources("aws_cloudwatch_metric_alarm")[name]

    assert _attribute(alarm.body, "alarm_name") is not None
    assert "alarm_description" in alarm.body, f"{name} has no description"
    assert _attribute(alarm.body, "treat_missing_data") is not None, (
        f"{name} leaves missing-data behaviour to a default"
    )


def test_no_alarm_reads_a_metric_the_application_has_to_publish() -> None:
    """Every namespace below is one AWS populates on its own. A custom metric would be an
    operational signal the application has to remember to emit, which goes quiet exactly when
    the application is broken - and it would need `PutMetricData` on a task role."""
    native = {"AWS/SQS", "AWS/ApplicationELB", "AWS/RDS", "AWS/ElastiCache"}
    for name, alarm in _resources("aws_cloudwatch_metric_alarm").items():
        namespace = (_attribute(alarm.body, "namespace") or "").strip('"')
        assert namespace in native, f"{name} reads {namespace}, which nothing publishes for free"

    joined = "\n".join(_tf_code().values())
    assert "PutMetricData" not in joined
    assert "aws_cloudwatch_log_metric_filter" not in joined


def test_the_dead_letter_alarm_fires_on_a_single_message() -> None:
    """Any message in that queue means three deliveries failed and nobody has looked at why.
    There is no threshold above zero that would be defensible."""
    alarm = _resources("aws_cloudwatch_metric_alarm")["dlq_not_empty"]

    assert _attribute(alarm.body, "metric_name") == '"ApproximateNumberOfMessagesVisible"'
    assert _attribute(alarm.body, "dimensions") == "{ QueueName = aws_sqs_queue.jobs_dlq.name }"
    assert _attribute(alarm.body, "threshold") == "0"
    assert _attribute(alarm.body, "comparison_operator") == '"GreaterThanThreshold"'
    assert _attribute(alarm.body, "treat_missing_data") == '"notBreaching"'


def test_the_queue_age_alarm_is_slower_than_a_job_and_faster_than_the_dead_letter_queue() -> None:
    """The threshold has to sit between two numbers the system already fixed: longer than one
    invocation's `MAX_JOB_RUNTIME` (1200s), and shorter than three deliveries of the 1800-second
    visibility window (5400s). Anything below the first pages on a healthy job; anything above
    the second tells you nothing the dead-letter alarm has not already said."""
    alarm = _resources("aws_cloudwatch_metric_alarm")["jobs_queue_backlog_age"]

    assert _attribute(alarm.body, "metric_name") == '"ApproximateAgeOfOldestMessage"'
    assert _attribute(alarm.body, "dimensions") == "{ QueueName = aws_sqs_queue.jobs.name }"
    assert _attribute(alarm.body, "threshold") == "var.queue_age_alarm_seconds"

    default = int(_attribute(_variables()["queue_age_alarm_seconds"].body, "default") or 0)
    assert 1200 < default < 5400, f"{default} is outside the window the runtime already fixed"


def test_the_load_balancer_alarms_name_the_api_target_group() -> None:
    for name in ("api_unhealthy_targets", "api_target_5xx"):
        alarm = _resources("aws_cloudwatch_metric_alarm")[name]
        assert _attribute(alarm.body, "namespace") == '"AWS/ApplicationELB"'
        assert _attribute(alarm.body, "TargetGroup") == "aws_lb_target_group.api.arn_suffix"
        assert _attribute(alarm.body, "LoadBalancer") == "aws_lb.api.arn_suffix"


def test_the_unhealthy_target_alarm_outlasts_the_documented_startup_window() -> None:
    """`/health` is legitimately 503 until the first worker creates LangGraph's tables, and the
    API service is given a 300-second grace period for it. An alarm window shorter than that
    would page on every single deploy."""
    alarm = _resources("aws_cloudwatch_metric_alarm")["api_unhealthy_targets"]

    period = int(_attribute(alarm.body, "period") or 0)
    periods = int(_attribute(alarm.body, "evaluation_periods") or 0)
    grace = int(
        _attribute(_resources("aws_ecs_service")["api"].body, "health_check_grace_period_seconds")
        or 0
    )
    assert period * periods > grace, "the alarm fires inside the documented startup window"


def test_the_store_alarms_hold_their_state_when_a_store_stops_reporting() -> None:
    """A database or a cache that has stopped publishing metrics is not a healthy one.
    `notBreaching` is right for a queue that is legitimately silent when empty and wrong here."""
    for name in ("rds_free_storage_low", "redis_memory_pressure"):
        alarm = _resources("aws_cloudwatch_metric_alarm")[name]
        assert _attribute(alarm.body, "treat_missing_data") == '"missing"', name


def test_the_free_storage_threshold_leaves_room_to_react() -> None:
    """It fires with roughly 10% of the allocated storage left. A full disk stops every
    checkpoint, every audit row and every terminal status, and cannot be recovered in place."""
    alarm = _resources("aws_cloudwatch_metric_alarm")["rds_free_storage_low"]
    assert _attribute(alarm.body, "comparison_operator") == '"LessThanThreshold"'
    assert _attribute(alarm.body, "threshold") == "var.rds_free_storage_alarm_bytes"

    threshold = int(_attribute(_variables()["rds_free_storage_alarm_bytes"].body, "default") or 0)
    allocated = int(_attribute(_variables()["db_allocated_storage"].body, "default") or 0)
    assert 0 < threshold < allocated * 1024**3, "the alarm would fire on an empty database"


# --- Block C: where an alarm goes ---------------------------------------------------------------


def test_there_is_one_alarm_topic_and_it_is_optional() -> None:
    """An SNS topic with nothing subscribed costs nothing, so every alarm gets a real action to
    name - and `create_alarms_topic = false` leaves the alarms in place with no action, still
    changing state and still visible in the console."""
    topic = _one("aws_sns_topic")
    assert _attribute(topic.body, "count") == "var.create_alarms_topic ? 1 : 0"
    assert _attribute(_variables()["create_alarms_topic"].body, "default") == "true"

    assert (
        "alarm_actions = var.create_alarms_topic ? [aws_sns_topic.alarms[0].arn] : []"
        in _tf_code()["monitoring.tf"]
    )


@pytest.mark.parametrize("name", ALARMS)
def test_every_alarm_notifies_the_one_topic(name: str) -> None:
    alarm = _resources("aws_cloudwatch_metric_alarm")[name]
    assert _attribute(alarm.body, "alarm_actions") == "local.alarm_actions"
    assert _attribute(alarm.body, "ok_actions") == "local.alarm_actions"


def test_no_email_address_is_written_down() -> None:
    """Subscribing is one operator command. A subscription here would put an address in a
    repository and send a confirmation mail on every apply."""
    for name, text in _tf_code().items():
        assert "notification_endpoint" not in text, f"{name} subscribes something to an alarm"
        assert re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text) is None, f"{name} holds an address"


# --- Block C: retention is explicit, and nothing new can read a secret ---------------------------


def test_every_log_group_has_an_explicit_short_retention() -> None:
    """A log group ECS creates on its own never expires, which is storage that keeps charging
    after every task has stopped. All four are Terraform-managed and all four say how long."""
    groups = _resources("aws_cloudwatch_log_group")
    assert set(groups) == {"api", "worker", "migrate", "ops"}

    for name, group in groups.items():
        assert _attribute(group.body, "retention_in_days") == "var.log_retention_days", name

    retention = int(_attribute(_variables()["log_retention_days"].body, "default") or 0)
    assert 0 < retention <= 7, f"{retention} days is not a temporary deployment's retention"


def test_the_reports_bucket_still_expires_nothing() -> None:
    """Block C adds retention *documentation* and no S3 expiry rule. The reports are the
    evidence this deployment exists to produce, and a schedule that deleted them could delete
    them before the screenshots were taken."""
    lifecycle = _one("aws_s3_bucket_lifecycle_configuration")
    assert "expiration" not in lifecycle.body


def test_the_dead_letter_queue_keeps_its_messages_longer_than_the_logs() -> None:
    """14 days, the maximum. The evidence that a job failed has to outlive the demo, and it
    deliberately outlives the log lines explaining it at 1-day retention."""
    dlq = _resources("aws_sqs_queue")["jobs_dlq"]
    assert _attribute(dlq.body, "message_retention_seconds") == "1209600"


def test_block_c_changed_no_running_services_permissions() -> None:
    """**A recovery tool's reach must not become a running service's reach.** The worker still
    cannot receive from the dead-letter queue, the API still cannot receive from anything, and
    neither gained CloudWatch or SNS - an operator needing a permission is never a reason to
    give it to a process that runs all day."""
    for name in ("api", "worker"):
        document = next(
            block
            for block in _all_blocks()
            if block.type == "data" and block.labels == ("aws_iam_policy_document", name)
        )
        assert "jobs_dlq" not in document.body, f"the {name} task can reach the dead-letter queue"
        assert "cloudwatch" not in document.body.lower(), f"the {name} task gained CloudWatch"
        assert "sns" not in document.body.lower(), f"the {name} task gained SNS"


def test_the_one_new_role_is_a_task_nobody_starts_and_it_touches_two_queues() -> None:
    """**Why a role exists at all when the operator already has AWS credentials.** RDS is
    `publicly_accessible = false` in subnets with no route off the VPC, so a laptop cannot reach
    the database every one of the four scripts reads. The only place they can run is inside
    this VPC, as a one-off task from the same image - and a task needs a role.

    What it may do is the union of what those scripts call: send a replacement message to the
    jobs queue, read, release or delete one on the dead-letter queue, and - since the W2 fix -
    write one report object during ADR 0009's re-export. Nothing else."""
    assert set(_resources("aws_iam_role")) == {"execution", "api", "worker", "ops"}

    assert "ops" in _resources("aws_ecs_task_definition")
    assert "ops" not in _resources("aws_ecs_service"), "the operator tooling is not a service"

    document = next(
        block
        for block in _all_blocks()
        if block.type == "data" and block.labels == ("aws_iam_policy_document", "ops")
    )
    assert "aws_sqs_queue.jobs.arn" in document.body
    assert "aws_sqs_queue.jobs_dlq.arn" in document.body
    for absent in ("secretsmanager", "cloudwatch", "sns", "ecs:"):
        assert absent not in document.body.lower(), f"the ops task role grants {absent}"

    # It never consumes the jobs queue. That is the worker's, and only the worker's.
    send_only = document.body.split("InspectAndRecoverDeadLetters")[0]
    assert "sqs:ReceiveMessage" not in send_only
    assert "sqs:DeleteMessage" not in send_only


# --- W2: the documented S3 re-export can actually run as the one-off ops task ------------------
#
# The audit finding this closes: docs/runbook.md sends an operator to
# `python scripts/reexport_job.py` for a job whose artifact write was exhausted, and told them
# to run every `python scripts/...` command as an ops task override - but the ops task had no
# `S3_BUCKET` and its role had no `s3:PutObject`, so ADR 0009's recovery path exited on a
# missing variable and could not have been fixed from anywhere else. RDS has no public address,
# so there is no second place to run it from.


def test_the_operator_role_may_write_a_report_artifact_and_nothing_wider() -> None:
    """**One action, one prefix, and it is read off the code rather than guessed.**

    `scripts/reexport_job.py` calls exactly one store method - `ArtifactStore.put_report`, which
    is one `put_object`. `object_key` is string arithmetic and `presign` signs locally without
    reaching S3, so `GetObject`, `ListBucket` and `HeadObject` are not required and are not
    granted: this identity can recover an artifact and still cannot read one back.
    """
    document = next(
        block
        for block in _all_blocks()
        if block.type == "data" and block.labels == ("aws_iam_policy_document", "ops")
    )
    actions = re.findall(r'"(s3:[A-Za-z*]+)"', document.body)
    assert actions == ["s3:PutObject"], f"the ops role's S3 actions are {actions}"
    assert '"${aws_s3_bucket.reports.arn}/reports/*"' in document.body, (
        "the S3 statement must name the reports prefix rather than the bucket or a wildcard"
    )
    assert "s3:*" not in document.body


def test_the_operator_task_is_given_the_bucket_and_still_no_provider_credential() -> None:
    """It re-projects durable state, moves messages, and can rewrite one report object. It can
    still reach nothing that could re-bill a model, and it still holds no auth material.

    `S3_BUCKET` is the same Terraform-created bucket the worker writes and the API presigns -
    there is one, and a second name would be a second bucket nobody watches.
    """
    definition = _resources("aws_ecs_task_definition")["ops"]

    for variable in (*PROVIDER_VARIABLES, "AUTH_KEYS", "COGNITO", "REDIS_URL"):
        assert variable not in definition.body, f"the ops task definition names {variable}"
    assert '{ name = "S3_BUCKET", value = aws_s3_bucket.reports.id }' in definition.body, (
        "scripts/reexport_job.py requires S3_BUCKET and this is the only place it can run"
    )
    assert _attribute(definition.body, "secrets") == "local.database_secrets", (
        "the bucket is configuration; the ops execution role still fetches only the database"
    )
    assert _attribute(definition.body, "command") == '["python", "scripts/reconcile_jobs.py"]', (
        "the default command must be the dry run, for a task somebody starts by accident"
    )


def test_the_recovery_scripts_the_ops_task_runs_are_all_in_the_image() -> None:
    """The path the runbook documents, end to end: every tool it tells an operator to run as an
    ops override has to be a file the Dockerfile copies. `check_model.py` and `measure_jobs.py`
    deliberately are not - both reach a real provider."""
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for script in (
        "scripts/reexport_job.py",
        "scripts/reconcile_jobs.py",
        "scripts/inspect_dlq.py",
        "scripts/replay_dlq.py",
    ):
        assert script in dockerfile, f"{script} is not in the image the ops task runs"
    for excluded in ("scripts/check_model.py", "scripts/measure_jobs.py"):
        assert excluded not in dockerfile, f"{excluded} reaches a provider and must stay out"


def test_only_the_ops_task_gained_the_report_write_the_recovery_needs() -> None:
    """**An operator needing a permission is never a reason to give it to a running service.**
    The API still may not write an object, and the worker still may not read one back - W2
    widened one role by one action and left both services exactly as they were."""
    api = next(
        block
        for block in _all_blocks()
        if block.type == "data" and block.labels == ("aws_iam_policy_document", "api")
    )
    worker = next(
        block
        for block in _all_blocks()
        if block.type == "data" and block.labels == ("aws_iam_policy_document", "worker")
    )
    assert re.findall(r'"(s3:[A-Za-z*]+)"', api.body) == ["s3:GetObject"], (
        "the API presigns downloads and must not have gained a write"
    )
    assert re.findall(r'"(s3:[A-Za-z*]+)"', worker.body) == ["s3:PutObject"], (
        "the worker writes artifacts and must not have gained a read"
    )


def test_the_operations_runbook_exists() -> None:
    """A stop condition for this block: alarms nobody has a written response to are decoration.
    Every scenario named here is one the alarms above or the runtime can actually produce."""
    runbook = (_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    for scenario in (
        "API unhealthy",
        "Worker not running",
        "Redis unavailable",
        "RDS unavailable",
        "Jobs queue backing up",
        "DLQ contains messages",
        "Stale queued or running row",
        "Job stuck awaiting approval",
        "S3 export failed",
        "Provider outage",
        "Lease ownership lost",
        "Migration failure",
        "Authentication failure",
    ):
        assert scenario in runbook, f"docs/runbook.md has no entry for {scenario}"

    for tool in ("scripts/reconcile_jobs.py", "scripts/inspect_dlq.py", "scripts/replay_dlq.py"):
        assert tool in runbook, f"the runbook never names {tool}"


def test_the_deployment_runbook_covers_the_block_c_teardown() -> None:
    """`terraform destroy` removes the alarms and the topic because Terraform owns them. The
    orphan checks are what catch the ones it did not."""
    runbook = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for check in ("describe-alarms", "list-topics"):
        assert check in runbook, f"the teardown checklist does not check for {check}"


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
