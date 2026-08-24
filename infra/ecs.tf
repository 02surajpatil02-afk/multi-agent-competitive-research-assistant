# WHY THIS FILE EXISTS
#     **One image, three commands** - the shape `Dockerfile` and docker-compose.yml's `app`
#     profile already have, moved onto Fargate without changing it. `api` runs
#     `uvicorn app:app`, `worker` runs `python -m worker`, `migrate` runs
#     `alembic upgrade head`, and all three run identical bytes from one ECR repository.
#
#     **The migration is a task definition and not a service**, which is the one structural
#     thing to notice. Nothing starts it automatically: an operator runs it once with
#     `aws ecs run-task` and waits for exit 0 before submitting a job (docs/deployment.md).
#     That preserves guidelines section 19's rule - neither long-running process migrates, and
#     two of them can never race `alembic upgrade head` against one database - and it is the
#     same rule Compose expresses locally with `service_completed_successfully`.
#
#     **LangGraph's checkpoint tables are still the worker's**, created by `setup()` at worker
#     startup, and Alembic still never touches them. That has a visible consequence here: until
#     the first worker has started, `/health` answers 503 with `checks.checkpoints = false`, so
#     the API's target is unhealthy and the ALB routes nothing to it. That is the health
#     contract working - a deployment in which no job can run should not be serving - and it is
#     why the API service carries a health-check grace period rather than a weakened check.
#     docs/deployment.md gives the startup order this produces.
#
#     **No credential appears in any `environment` block below** (Block B, ADR 0020 decision 1).
#     Every secret arrives through `secrets`, which names a Secrets Manager ARN rather than a
#     value: the ECS agent fetches it at task start using the execution role, and the container
#     sees an ordinary environment variable. `ecs:DescribeTaskDefinition` now returns ARNs, and
#     a task definition revision - which is kept forever, long after the deployment is destroyed
#     - no longer carries a password.
#
# WHO USES IT
#     `terraform apply`, and the operator commands in docs/deployment.md.

locals {
  # **The database arrives in parts, not as a URL.** RDS generates and holds the master password
  # (`manage_master_user_password`), so no value Terraform can see would complete a connection
  # string - which is exactly why the password is not in the state file. The two halves of the
  # credential come from the RDS-managed secret's JSON below, and the container composes the URL
  # (`config.resolve_database_url`, ADR 0020 decision 1).
  #
  # `address` rather than `endpoint`, because `endpoint` is already `host:port` and these are
  # separate variables.
  database_environment = [
    { name = "DB_HOST", value = aws_db_instance.postgres.address },
    { name = "DB_PORT", value = tostring(aws_db_instance.postgres.port) },
    { name = "DB_NAME", value = var.db_name },
  ]

  # `secretsmanager:...:json-key::` is how ECS reads one field out of a JSON secret. The RDS
  # managed secret holds `{"username": ..., "password": ...}` and nothing else, which is why the
  # host, port and database name are plain environment above.
  database_secrets = [
    { name = "DB_USER", valueFrom = "${local.db_secret_arn}:username::" },
    { name = "DB_PASSWORD", valueFrom = "${local.db_secret_arn}:password::" },
  ]

  redis_url = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:${aws_elasticache_cluster.redis.port}/0"

  # Tagged with something a rollback can name - a commit SHA, per guidelines section 19 - which
  # is why var.image_tag has no default.
  image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

  # **Neither AWS_ENDPOINT_URL nor S3_PUBLIC_ENDPOINT_URL appears anywhere below, and both
  # absences are deliberate.** They exist for LocalStack, where the address a container writes
  # through and the address a browser downloads from are genuinely different machines' idea of
  # one service. Against real AWS they are the same address, so boto3's default endpoint is
  # correct and `artifacts.presign` signs a URL that already resolves. Setting either here
  # would sign a private or wrong host into a SigV4 signature that cannot be rewritten
  # afterwards.
  common_environment = concat(local.database_environment, [
    { name = "REDIS_URL", value = local.redis_url },
    { name = "SQS_QUEUE_URL", value = aws_sqs_queue.jobs.url },
    { name = "S3_BUCKET", value = aws_s3_bucket.reports.id },
    { name = "AWS_REGION", value = var.region },
    { name = "APP_ENV", value = "prod" },
    { name = "LOG_LEVEL", value = var.log_level },
  ])

  # **Not secrets, and deliberately plain.** Verifying a Cognito token needs a published signing
  # key, a published issuer and a client id; none of the three is a credential, and putting them
  # in Secrets Manager would buy nothing and cost a fetch at every task start.
  api_auth_environment = local.cognito_enabled ? [
    { name = "AUTH_MODE", value = "cognito" },
    { name = "COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.main[0].id },
    { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.api[0].id },
    { name = "COGNITO_REGION", value = var.region },
    ] : [
    { name = "AUTH_MODE", value = "api_key" },
  ]

  # The key table, when there still is one. Under Cognito the API holds **no shared secret at
  # all** - which is why this list is empty and its execution role is granted nothing but the
  # database credential.
  api_auth_secrets = local.cognito_enabled ? [] : [
    { name = "AUTH_KEYS", valueFrom = aws_secretsmanager_secret.auth_keys[0].arn },
  ]

  # **The worker's model provider, and the only place the two alternatives are written down**
  # (docs/adr/0022-*.md). They are alternatives rather than layers: under Bedrock there is no
  # endpoint, no model ids and no key, and under OpenAI there is no Bedrock configuration and no
  # Bedrock permission. Nothing in either list is a credential except `LLM_API_KEY`, which is why
  # it is the only entry that appears below in `worker_llm_secrets` rather than here.
  worker_llm_environment = local.bedrock_enabled ? [
    { name = "LLM_PROVIDER", value = "bedrock" },
    { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
    { name = "BEDROCK_REGION", value = local.bedrock_region },
    ] : [
    { name = "LLM_PROVIDER", value = "openai" },
    { name = "LLM_BASE_URL", value = var.llm_base_url },
    { name = "LLM_MODEL", value = var.llm_model },
    { name = "LLM_FAST_MODEL", value = var.llm_fast_model },
  ]

  # **Empty under Bedrock, and that emptiness is the security property.** No LLM secret is
  # created (secrets.tf), so the worker's execution role is granted nothing to fetch for the
  # model provider, and a task definition revision - kept forever - names no provider
  # credential. Authorization is `bedrock:InvokeModel` on the worker *task* role and nothing
  # else.
  worker_llm_secrets = local.bedrock_enabled ? [] : [
    { name = "LLM_API_KEY", valueFrom = aws_secretsmanager_secret.llm_api_key[0].arn },
  ]
}

# --- The image repository ---------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name = local.name

  # Mutable so a re-push of the same tag during a demo is not a wall. Production wants IMMUTABLE:
  # a tag that can move is a rollback target that can lie.
  image_tag_mutability = "MUTABLE"

  # ECR's basic scan is free and runs on push. It is not a security programme; it is one finding
  # in the console for the price of one line.
  image_scanning_configuration {
    scan_on_push = true
  }

  # `terraform destroy` deletes the repository *with the images in it*. Without this, destroy
  # fails on a non-empty repository and leaves storage charging.
  force_delete = true

  tags = { Name = local.name }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the five most recent images; a demo pushes two or three."
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = {
        type = "expire"
      }
    }]
  })
}

# --- The cluster and its logs -------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name

  # Container Insights is a per-metric CloudWatch charge and answers questions this deployment
  # is not asking. Monitoring is Block C's.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = { Name = local.name }
}

# Four log groups rather than one, so `aws logs tail` reads one process at a time - which is
# how a two-process system is actually debugged. They are Terraform-managed on purpose: a log
# group ECS creates on its own survives `terraform destroy` and keeps charging for storage.
resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}/api"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name}-api" }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}/worker"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name}-worker" }
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/ecs/${local.name}/migrate"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name}-migrate" }
}

# Block C's operator tooling writes here. It gets its own group for the same reason the other
# three do: what a recovery run decided, and about which job, is the thing you go looking for
# afterwards - and it should not be interleaved with twenty minutes of worker output.
resource "aws_cloudwatch_log_group" "ops" {
  name              = "/ecs/${local.name}/ops"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name}-ops" }
}

# --- The four task definitions -------------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution["api"].arn
  task_role_arn            = aws_iam_role.api.arn

  runtime_platform {
    operating_system_family = "LINUX"
    # The image is built on an x86 developer machine. ARM64 Fargate is cheaper and is a
    # production choice, but it needs an arm64 image and this deployment is not the place to
    # discover a cross-build problem.
    cpu_architecture = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "api"
    image     = local.image
    essential = true

    # The same command docker-compose.yml gives the `api` service.
    command = ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]

    # **No LLM or Tavily variable, in either list** - ADR 0012 as configuration rather than as
    # intent. The API process starts, serves all six routes and passes its health check with no
    # provider credential in its environment, and its execution role cannot fetch one either.
    environment = concat(local.common_environment, local.api_auth_environment)

    # The database credential, and the key table only in the mode that has one.
    secrets = concat(local.database_secrets, local.api_auth_secrets)

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])

  tags = { Name = "${local.name}-api" }
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution["worker"].arn
  task_role_arn            = aws_iam_role.worker.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true

    command = ["python", "-m", "worker"]

    # **No portMappings.** The worker serves nothing and makes no authorization decision.

    # **120 seconds, and it must stay 120.** ARCHITECTURE.md section 19 requires the task
    # definition to match `stop_grace_period: 120s` in docker-compose.yml, because the default
    # is 30 and the two numbers have to agree or local shutdown stops predicting deployed
    # shutdown. It is the maximum graceful-stop opportunity Fargate offers - the worker uses it
    # to finish the node in flight, checkpoint synchronously, release the job lock and stop the
    # visibility heartbeat. It is not a guarantee every node fits: a longer one is still killed,
    # the message is still never deleted, and redelivery is still the recovery path.
    stopTimeout = 120

    # A provider name, an endpoint or a model id, and a region are configuration rather than
    # credentials, so they stay here where they can be read and changed. `local.worker_llm_
    # environment` is the whole of the provider choice; the search key, and the model key when
    # there is one, are below.
    #
    # **The four runtime bounds are stated rather than defaulted**, and only here. `config.py`
    # keeps 60/2/24/1200 for local work; this deployment runs 180/3/30/1800, which is what both
    # published n=20 baselines measured against. They are `var.*` so an operator can move one
    # with `TF_VAR_*` instead of editing this file (variables.tf carries the derivation, and
    # why `max_job_runtime` sharing a value with the queue's visibility window couples nothing).
    # None of the four reaches the API, the migration or the ops task: none of those runs a node.
    environment = concat(local.common_environment, local.worker_llm_environment, [
      { name = "LLM_MAIN_TIMEOUT_S", value = tostring(var.llm_main_timeout_s) },
      { name = "MAX_REVISIONS", value = tostring(var.max_revisions) },
      { name = "MAX_SUPERVISOR_HOPS", value = tostring(var.max_supervisor_hops) },
      { name = "MAX_JOB_RUNTIME", value = tostring(var.max_job_runtime) },
    ])

    # **`TAVILY_API_KEY` is unchanged by the provider choice, and that is the point of listing
    # it separately.** The web-search credential has nothing to do with which model answers, so
    # switching to Bedrock removed one secret and left this one exactly where it was.
    secrets = concat(local.database_secrets, local.worker_llm_secrets, [
      { name = "TAVILY_API_KEY", valueFrom = aws_secretsmanager_secret.tavily_api_key.arn },
    ])

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])

  tags = { Name = "${local.name}-worker" }
}

# The one-off. It has **no service, no task role, and nothing but a database** - a migration
# needs no LLM key, no queue and no bucket, and this is the one point where least privilege
# costs nothing at all.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution["migrate"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.image
    essential = true

    command = ["alembic", "upgrade", "head"]

    # Connectivity and the database credential, and nothing else. Its execution role can fetch
    # this one secret and no other.
    environment = local.database_environment
    secrets     = local.database_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.migrate.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "migrate"
      }
    }
  }])

  tags = { Name = "${local.name}-migrate" }
}

# The other one-off: Phase 5 block C's operator recovery tooling, run by hand with a command
# override. **No service, and nothing starts it.**
#
#     aws ecs run-task --cluster ... --task-definition ...-ops \
#       --overrides '{"containerOverrides":[{"name":"ops","command":["python","scripts/reconcile_jobs.py"]}]}'
#
# It exists because RDS has no public address and sits in subnets with no route off the VPC, so
# a laptop cannot reach the database the four scripts read. Its environment is the database, the
# queue and the reports bucket; it has **no Redis, no provider credential and no auth setting**,
# and its task role may touch two queues and one object prefix and nothing else
# (docs/adr/0021-*.md decision 7).
#
# **`S3_BUCKET` is here for exactly one script.** `scripts/reexport_job.py` is ADR 0009's
# recovery path for an approved report whose `PutObject` was exhausted, and docs/runbook.md
# sends an operator to it - but the only place it can run is here, for the same private-RDS
# reason the other three are here. Without the bucket it exited on `S3_BUCKET is required`, so
# the documented recovery was unreachable in a deployment. The three block C tools do not read
# it and are unaffected.
#
# The default command is the dry run, which is the safe thing for a task somebody starts by
# accident: it writes nothing and prints what it would do.
resource "aws_ecs_task_definition" "ops" {
  family                   = "${local.name}-ops"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution["ops"].arn
  task_role_arn            = aws_iam_role.ops.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "ops"
    image     = local.image
    essential = true

    command = ["python", "scripts/reconcile_jobs.py"]

    environment = concat(local.database_environment, [
      { name = "SQS_QUEUE_URL", value = aws_sqs_queue.jobs.url },
      { name = "S3_BUCKET", value = aws_s3_bucket.reports.id },
      { name = "AWS_REGION", value = var.region },
      { name = "APP_ENV", value = "prod" },
      { name = "LOG_LEVEL", value = var.log_level },
    ])

    secrets = local.database_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.ops.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "ops"
      }
    }
  }])

  tags = { Name = "${local.name}-ops" }
}

# --- The two services -------------------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.public[*].id
    security_groups = [aws_security_group.api.id]

    # **The NAT-gateway trade, in one attribute.** A public IP is how this task reaches ECR,
    # CloudWatch Logs, SQS and S3 without a NAT gateway. What stops it being reachable is
    # `aws_security_group.api`, whose single ingress rule names the load balancer's group.
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # **Why this is here, and what it does not do.** `/health` reports `checks.checkpoints`, and
  # LangGraph's tables do not exist until the first worker calls `setup()`. Without a grace
  # period ECS would kill and replace the API task for failing a check it is right to fail. The
  # grace period stops the killing; it does **not** make the ALB route traffic - the target
  # stays unhealthy, and 503 is the correct answer, until a worker has started.
  health_check_grace_period_seconds = 300

  # Off. It opens a shell into a running task, which is a debugging convenience and an
  # authenticated path into the process that holds the key table.
  enable_execute_command = false

  # The listener must exist before a service may register into its target group. Both are
  # named: with a certificate configured the HTTPS one is what forwards, and the HTTP one
  # redirects to it.
  depends_on = [aws_lb_listener.http, aws_lb_listener.https]

  tags = { Name = "${local.name}-api" }
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.public[*].id
    security_groups = [aws_security_group.worker.id]

    # Egress to the LLM endpoint, Tavily and the open web is the worker's whole job. Its
    # security group has no ingress rule at all, so nothing can reach back.
    assign_public_ip = true
  }

  # **No `load_balancer` block, and there must not be one.** The worker serves nothing.

  # Stop the old task before starting the new one. With one worker that means at most one
  # consumer at any instant, which is the cheapest way to keep a rolling deploy from briefly
  # doubling the deployment's share of a 40 RPM LLM tier. Correctness does not depend on it -
  # FIFO message groups and ADR 0016's PostgreSQL job lock already fence two workers off one
  # job - so this is a cost and clarity choice, not a safety one.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  enable_execute_command = false

  tags = { Name = "${local.name}-worker" }
}
