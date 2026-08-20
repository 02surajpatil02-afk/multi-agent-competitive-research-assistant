# WHY THIS FILE EXISTS
#     Three roles, and the smallest set of permissions that lets the two processes do the work
#     they already do. **This is Block A's minimum-functional IAM, not Block B's hardening**,
#     and the difference is worth stating rather than implying:
#
#     | Built here | Left to Block B |
#     |---|---|
#     | One task role per service, so the API cannot consume the queue and the worker cannot presign | Conditions on the trust policy (`aws:SourceArn`), which stop a confused-deputy call from another account |
#     | Actions named one by one, never `sqs:*` or `s3:*` | A customer-managed KMS key and the grants that go with it |
#     | Resources scoped to this deployment's queue ARN and to `reports/*` inside this bucket | `secretsmanager:GetSecretValue` on the execution role, which is what replaces the plaintext environment in ecs.tf |
#     | An execution role that is AWS's managed ECS policy and nothing added | A permissions boundary, and access logging on who assumed what |
#
#     **The split is the one ARCHITECTURE.md section 19 already describes.** The worker may
#     receive, delete and extend a message and write an object; the API may send a message and
#     read one back for a presigned URL. Neither can do the other's job, which is what makes
#     "the API never runs the graph" a property of the deployment rather than of the code alone.
#
#     **The migration task gets no task role at all.** `alembic upgrade head` talks to
#     PostgreSQL and to nothing else - no queue, no bucket, no secret - so the correct set of
#     AWS permissions for it is the empty one.
#
# WHO USES IT
#     ecs.tf, which names these roles on its three task definitions.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- The execution role ---------------------------------------------------------------------
#
# This is the role the ECS agent uses to *start* a task - pull the image from ECR and create the
# log stream - rather than the role the application code runs as. One is enough for all three
# task definitions, because all three start the same way.

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "${local.name}-ecs-execution" }
}

# AWS's own managed policy, deliberately. It is exactly ECR pull plus CloudWatch Logs write, it
# is maintained by AWS, and hand-copying it would produce a private version of the same thing
# that drifts.
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
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
