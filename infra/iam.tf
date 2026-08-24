# WHY THIS FILE EXISTS
#     Six roles, and the smallest set of permissions that lets each process do the work it
#     already does. Block A built one execution role and two task roles; Block B split the
#     execution role three ways and added the conditions that stop it being assumed by anything
#     but this account's ECS.
#
#     **The distinction that shapes the whole file.** An *execution* role belongs to the ECS
#     agent and is used to *start* a task - pull the image, create the log stream, and now fetch
#     the secrets the task definition names. A *task* role belongs to the running application
#     and is what boto3 picks up inside the container. They are different identities with
#     different jobs, and a secret granted to one is not readable by the other.
#
#     **That is why the secrets go on the execution roles and not the task roles.** The
#     application never calls Secrets Manager - it reads environment variables, exactly as it
#     does locally - so nothing in the container needs, or has, permission to read a secret.
#     A compromised worker process cannot fetch the auth table; it cannot fetch anything.
#
#     **Three execution roles rather than one**, because one shared role would have to be
#     granted every secret all three tasks use, which would give the API's task-start identity
#     the LLM key. That is precisely the boundary ADR 0012 exists to hold, and a shared role
#     would quietly undo it in IAM while the task definition still looked clean.
#
#     | Role | May fetch |
#     |---|---|
#     | `api` execution | the RDS-managed database credential; the auth-keys secret **only in api_key mode** |
#     | `worker` execution | the database credential, the search key, and the LLM key **only in openai mode** |
#     | `migrate` execution | the database credential, and nothing else |
#     | `api` task | send a message to this queue; sign for objects under `reports/` |
#     | `worker` task | receive, delete and extend a message on this queue; write objects under `reports/`; invoke the configured Bedrock model **only in bedrock mode** |
#     | `migrate` task | **there is none** - a migration talks to PostgreSQL and to nothing AWS |
#
#     **The worker task role is the only identity in this deployment that may call a model**, and
#     under `llm_provider = "bedrock"` that permission is the whole of the model credential - there
#     is no key, in the task definition or in Secrets Manager (docs/adr/0022-*.md decision 3).
#
#     **No wildcard resource appears anywhere below**, and no `s3:*`, `sqs:*` or
#     `secretsmanager:*`. Every statement names its actions one at a time and scopes them to a
#     resource this configuration creates. The one thing worth knowing about
#     `AmazonECSTaskExecutionRolePolicy`, which is attached to all three execution roles: it is
#     AWS's own managed policy and it does use `Resource: "*"` for `ecr:GetAuthorizationToken`
#     and the CloudWatch Logs actions. That is not avoidable - `GetAuthorizationToken` has no
#     resource to scope to, it is an account-level call - and hand-copying the policy would
#     produce a private version of the same thing that drifts as AWS updates it.
#
#     **Left to production rather than built** (docs/deployment.md section 9): a permissions
#     boundary, a customer-managed KMS key with grants to these roles, and CloudTrail data
#     events on who assumed what. Each needs setup outside this configuration.
#
# WHO USES IT
#     ecs.tf, which names these roles on its four task definitions.

data "aws_caller_identity" "current" {}

# --- Who may assume these roles ------------------------------------------------------------
#
# **Block B's trust hardening.** Block A trusted `ecs-tasks.amazonaws.com` with no conditions,
# which is the shape every tutorial uses and which technically lets the ECS service in *any*
# account ask to assume this role on behalf of a task there. The two conditions below are the
# confused-deputy fix AWS documents: the request must be made on behalf of this account, and the
# thing making it must be an ECS resource in this region.
#
# `aws:SourceArn` is `ArnLike` with a trailing wildcard because the concrete value is the task
# ARN, which does not exist until ECS creates the task - there is nothing narrower to write.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

# --- The three execution roles ---------------------------------------------------------------

locals {
  # The database credential RDS generated and holds. All three tasks need it: the API and the
  # worker connect, and the migration is the one that creates the schema.
  db_secret_arn = aws_db_instance.postgres.master_user_secret[0].secret_arn

  # Read as a table: role name to the exact secret ARNs its task definition names. iam.tf and
  # ecs.tf must agree, and this local is the one place that decides.
  execution_secret_arns = {
    api = local.cognito_enabled ? [
      local.db_secret_arn,
      ] : [
      local.db_secret_arn,
      aws_secretsmanager_secret.auth_keys[0].arn,
    ]
    # **The LLM key is in this list only when it exists.** Under the default
    # `llm_provider = "bedrock"` no such secret is created, so the worker's task-start identity
    # is granted the database credential and the search key and nothing else - and there is no
    # stale permission left pointing at a secret that is not there (docs/adr/0022-*.md).
    worker = concat(
      [
        local.db_secret_arn,
        aws_secretsmanager_secret.tavily_api_key.arn,
      ],
      local.bedrock_enabled ? [] : [aws_secretsmanager_secret.llm_api_key[0].arn],
    )
    migrate = [
      local.db_secret_arn,
    ]
    # Block C's operator tooling. The database credential and nothing else - the four scripts
    # read rows, checkpoints, queues and one object prefix, and none of them can reach a model.
    # The bucket is reached with the *task* role, not this one: nothing in the container calls
    # Secrets Manager, so widening the recovery path widened no secret access.
    ops = [
      local.db_secret_arn,
    ]
  }
}

resource "aws_iam_role" "execution" {
  for_each = local.execution_secret_arns

  name               = "${local.name}-${each.key}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-${each.key}-execution" }
}

resource "aws_iam_role_policy_attachment" "execution" {
  for_each = aws_iam_role.execution

  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# One statement, one action, and a list of exactly the secrets this task definition names. The
# `DescribeSecret` companion permission is deliberately absent: ECS needs only the value.
resource "aws_iam_role_policy" "execution_secrets" {
  for_each = local.execution_secret_arns

  name = "${local.name}-${each.key}-execution-secrets"
  role = aws_iam_role.execution[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "FetchOnlyThisTasksSecrets"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = each.value
    }]
  })
}

# --- The API task role ------------------------------------------------------------------------

resource "aws_iam_role" "api" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-api-task" }
}

data "aws_iam_policy_document" "api" {
  # `POST /jobs` and `POST /jobs/{id}/approve` send a pointer message. **No Receive and no
  # Delete**: the API does not consume its own queue, and this is where that is enforced rather
  # than assumed.
  statement {
    sid    = "SendJobPointerMessages"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  # `GET /jobs/{id}/report` signs a URL for one object. Signing itself calls nothing, but the
  # signature is only honoured if the signing identity may perform the operation - so this
  # permission is what makes the presigned URL work, and `reports/*` is every key
  # `artifacts.object_key` can produce. **No PutObject**: the API never writes an artifact.
  statement {
    sid       = "PresignReportDownloads"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.reports.arn}/reports/*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "${local.name}-api-task"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

# --- The operations task role (Phase 5 block C) ---------------------------------------------------
#
# **Why this role exists at all, when the operator already has credentials.** RDS is
# `publicly_accessible = false` in subnets with no route off the VPC, so a laptop cannot reach
# the database - which every one of the three recovery scripts needs. The only place they can run
# is inside this VPC, as a one-off task from the same image, and a task needs a role
# (docs/adr/0021-*.md decision 7).
#
# **It is a task and never a service.** Nothing starts it; an operator runs `aws ecs run-task`
# with a command override, exactly as they do for the migration.
#
# What it may do is the union of what the four scripts actually call, and no more: read and
# release messages on the dead-letter queue, delete one from it during a deliberate replay, send
# one back to the jobs queue, and write one report object during ADR 0009's re-export. **No
# Secrets Manager, no CloudWatch, and no permission on any other queue.** The API and the worker
# gained nothing from this file.
#
# **The S3 statement is `s3:PutObject` and nothing else, and that is read off the code rather
# than guessed.** `scripts/reexport_job.py` calls exactly one store method, `put_report`, which
# is one `put_object`; `object_key` is string arithmetic and `presign` signs locally without
# reaching S3, so no `GetObject`, `ListBucket` or `HeadObject` is required and none is granted.
# The prefix is the same `reports/*` the worker writes, because a re-export overwrites the key
# the exhausted attempt was going to write.

resource "aws_iam_role" "ops" {
  name               = "${local.name}-ops-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-ops-task" }
}

data "aws_iam_policy_document" "ops" {
  # `reconcile_jobs.py` re-enqueues a job whose message was lost, using the same `send_start`
  # the API uses. **No Receive and no Delete on this queue**: the operator tooling never
  # consumes the jobs queue - that is the worker's, and only the worker's.
  statement {
    sid    = "ReEnqueueLostJobMessages"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  # The dead-letter queue: read it, hand messages straight back (`ChangeMessageVisibility` with
  # a zero timeout is what `release` is), and delete one only after a deliberate replay has
  # already sent it. `GetQueueUrl` is how the DLQ is resolved from the jobs queue's redrive
  # policy rather than from a second environment variable.
  statement {
    sid    = "InspectAndRecoverDeadLetters"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:DeleteMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs_dlq.arn]
  }

  # ADR 0009's recovery: re-project an approved `jobs.report_json` that has no artifact. One
  # action, one prefix. **No `GetObject`**, so this identity can write a report and still cannot
  # read one back out - which keeps `GET /jobs/{id}/report`'s presigned download the API's job,
  # exactly as `PutObject` without `GetObject` keeps it out of the worker's.
  statement {
    sid       = "RecoverAnExhaustedReportArtifact"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.reports.arn}/reports/*"]
  }
}

resource "aws_iam_role_policy" "ops" {
  name   = "${local.name}-ops-task"
  role   = aws_iam_role.ops.id
  policy = data.aws_iam_policy_document.ops.json
}


# --- The worker task role ----------------------------------------------------------------------

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-worker-task" }
}

data "aws_iam_policy_document" "worker" {
  # The four operations `jobqueue.py` performs, plus the two attribute reads `worker.check_queue`
  # makes at startup to refuse a queue that is not FIFO. `ChangeMessageVisibility` is ADR 0015's
  # heartbeat and is as load-bearing as the receive itself.
  statement {
    sid    = "ConsumeAndOwnJobMessages"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  # The export node's `PutObject`, under the one prefix it writes. **No GetObject and no
  # presign**: the worker writes the artifact and never hands it out.
  statement {
    sid       = "WriteReportArtifacts"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.reports.arn}/reports/*"]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "${local.name}-worker-task"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# --- The worker's Bedrock permission, and only the worker's (ADR 0022) --------------------------
#
# **A separate policy with a `count` rather than a statement inside the document above**, because
# the grant is conditional and the condition is the provider. In `openai` mode this resource does
# not exist at all, so the worker task role carries no Bedrock permission - which is what makes
# "which provider is live" a single variable rather than a variable plus a policy nobody
# remembered to remove.
#
# **`bedrock:InvokeModel` and nothing else.** `bedrock.py` calls `converse`, which is authorized
# by `InvokeModel`. `bedrock:InvokeModelWithResponseStream` is deliberately absent: nothing here
# calls `ConverseStream`, and granting a permission "for completeness" is how a least-privilege
# policy stops being one. Neither is `bedrock:ListFoundationModels` - discovering a model is an
# operator's job with their own credentials, not the worker's, and `BEDROCK_MODEL_ID` is how the
# answer reaches the task.
#
# **The API, the migration and the ops task get none of this.** The API constructs no LLM client
# (ADR 0012), a migration talks only to PostgreSQL, and the four recovery scripts re-project rows
# and move messages - `tests/test_infrastructure_terraform.py` fails if any of the three gains a
# Bedrock action.
#
# The resource list is `local.bedrock_invoke_arns` (versions.tf), which names the inference
# profile in this account and the foundation model in each destination Region. It is never `*`.

data "aws_iam_policy_document" "worker_bedrock" {
  count = local.bedrock_enabled ? 1 : 0

  statement {
    sid       = "InvokeTheConfiguredNovaModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = local.bedrock_invoke_arns
  }
}

resource "aws_iam_role_policy" "worker_bedrock" {
  count = local.bedrock_enabled ? 1 : 0

  name   = "${local.name}-worker-bedrock"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_bedrock[0].json
}
