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
#     **Every credential below is a plaintext task-definition environment variable.** That is
#     Block A's stated interface and Block B's first job: `secrets` with `valueFrom` pointing at
#     Secrets Manager, and `secretsmanager:GetSecretValue` on the execution role in iam.tf.
#     Anyone who can call `ecs:DescribeTaskDefinition` can read these values today.
#
# WHO USES IT
#     `terraform apply`, and the operator commands in docs/deployment.md.

locals {
  # `endpoint` is already `host:port`, so the URL needs no port of its own. The password is
  # URL-encoded because a libpq connection string is a URL and a `@` or `/` in a generated
  # password would otherwise end the credential early.
  database_url = "postgresql://${var.db_username}:${urlencode(var.db_password)}@${aws_db_instance.postgres.endpoint}/${var.db_name}"

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
  common_environment = [
    { name = "DATABASE_URL", value = local.database_url },
    { name = "REDIS_URL", value = local.redis_url },
    { name = "SQS_QUEUE_URL", value = aws_sqs_queue.jobs.url },
    { name = "S3_BUCKET", value = aws_s3_bucket.reports.id },
    { name = "AWS_REGION", value = var.region },
    { name = "APP_ENV", value = "prod" },
    { name = "LOG_LEVEL", value = var.log_level },
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
      selection    = {
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

# Three log groups rather than one, so `aws logs tail` reads one process at a time - which is
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

# --- The three task definitions ------------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
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

    # **AUTH_KEYS is here and no LLM or Tavily variable is** - ADR 0012 as configuration rather
    # than as intent. The API process starts, serves all six routes and passes its health check
    # with no provider credential in its environment.
    environment = concat(local.common_environment, [
      { name = "AUTH_KEYS", value = var.auth_keys },
    ])

    logConfiguration = {
      logDriver = "awslogs"
      options   = {
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
  execution_role_arn       = aws_iam_role.execution.arn
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

    environment = concat(local.common_environment, [
      { name = "LLM_BASE_URL", value = var.llm_base_url },
      { name = "LLM_MODEL", value = var.llm_model },
      { name = "LLM_FAST_MODEL", value = var.llm_fast_model },
      { name = "LLM_API_KEY", value = var.llm_api_key },
      { name = "TAVILY_API_KEY", value = var.tavily_api_key },
    ])

    logConfiguration = {
      logDriver = "awslogs"
      options   = {
        awslogs-group         = aws_cloudwatch_log_group.worker.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])

  tags = { Name = "${local.name}-worker" }
}

# The one-off. It has **no service, no task role and no environment beyond a database URL** - a
# migration needs no LLM key, no queue and no bucket, and this is the one point where least
# privilege costs nothing at all.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.image
    essential = true

    command = ["alembic", "upgrade", "head"]

    environment = [
      { name = "DATABASE_URL", value = local.database_url },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options   = {
        awslogs-group         = aws_cloudwatch_log_group.migrate.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "migrate"
      }
    }
  }])

  tags = { Name = "${local.name}-migrate" }
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

  # The listener must exist before a service may register into its target group.
  depends_on = [aws_lb_listener.http]

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
