# WHY THIS FILE EXISTS
#     The credentials that are genuinely secret, and only those. Block A put every one of them
#     in a task definition's plaintext `environment`, where anyone who can call
#     `ecs:DescribeTaskDefinition` reads them - and a task definition revision is kept forever
#     after the deployment is destroyed. Block B moves them here and injects them through the
#     task definition's `secrets` block instead, so the ECS agent fetches the value at task
#     start and the application still reads a plain environment variable
#     (docs/adr/0020-*.md decision 1).
#
#     **Three secrets, one per independent credential, and no blob.** A single JSON secret
#     holding everything would mean the API's execution role could read the LLM key in order to
#     read the auth table - the exact separation ADR 0012 exists to preserve. Each of these is
#     granted to exactly the roles that need it in iam.tf.
#
#     **The database password is not here**, and its absence is the point. RDS generates and
#     holds it itself (`manage_master_user_password` in data_stores.tf), so it never passes
#     through Terraform at all. This file only holds the credentials AWS cannot generate.
#
#     **What is deliberately NOT a secret**, because putting configuration in Secrets Manager
#     buys nothing and costs a fetch: the LLM endpoint and model ids, the queue URL, the bucket
#     name, the Redis address, the Cognito pool and client ids. A public key and an issuer are
#     published by design. They stay ordinary environment variables in ecs.tf.
#
#     **The remaining state exposure, stated rather than implied.** Terraform writes these
#     values, so they are in `terraform.tfstate` - the file is a credential store and
#     docs/deployment.md says so, and the teardown deletes it. The production shape is to create
#     the secret here with no version and populate it out of band with
#     `aws secretsmanager put-secret-value`, which the `ignore_changes` below already allows.
#     It is not the default because an empty secret makes `terraform apply` succeed and the
#     worker crash-loop, while a required variable makes apply refuse - a better failure.
#
# WHO USES IT
#     ecs.tf, which references these ARNs from `secrets`, and iam.tf, which grants
#     `secretsmanager:GetSecretValue` on exactly these ARNs to exactly the roles that need them.

locals {
  # 0 days means `terraform destroy` deletes the secret immediately rather than scheduling it
  # for 7-30 days. That matters twice for a temporary deployment: a scheduled secret still shows
  # up in the console after teardown, and a re-deploy under the same name is **refused** while
  # one is scheduled - the most common way the second run of a demo fails.
  secret_recovery_window_days = 0
}

# --- The worker's provider credentials ---------------------------------------------------------

resource "aws_secretsmanager_secret" "llm_api_key" {
  name                    = "${local.name}/llm-api-key"
  description             = "LLM_API_KEY for the worker. The API never receives this (ADR 0012)."
  recovery_window_in_days = local.secret_recovery_window_days

  tags = { Name = "${local.name}-llm-api-key" }
}

resource "aws_secretsmanager_secret_version" "llm_api_key" {
  secret_id     = aws_secretsmanager_secret.llm_api_key.id
  secret_string = var.llm_api_key

  lifecycle {
    # So an operator who rotates the value with `put-secret-value` is not reverted by the next
    # `terraform apply`. Terraform owns the secret; the value is allowed to move without it.
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "tavily_api_key" {
  name                    = "${local.name}/tavily-api-key"
  description             = "TAVILY_API_KEY for the worker. The API fetches no page and needs none."
  recovery_window_in_days = local.secret_recovery_window_days

  tags = { Name = "${local.name}-tavily-api-key" }
}

resource "aws_secretsmanager_secret_version" "tavily_api_key" {
  secret_id     = aws_secretsmanager_secret.tavily_api_key.id
  secret_string = var.tavily_api_key

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# --- The API's key table, in the mode that still has one ----------------------------------------
#
# **Created only when `auth_mode = "api_key"`.** Under Cognito there is no key table at all: the
# API verifies a signed token against a published key, so it holds no shared secret and its
# execution role is granted nothing but the database credential.

resource "aws_secretsmanager_secret" "auth_keys" {
  count = local.cognito_enabled ? 0 : 1

  name                    = "${local.name}/auth-keys"
  description             = "AUTH_KEYS: the sha256 of each API key mapped to a user_id and a role."
  recovery_window_in_days = local.secret_recovery_window_days

  tags = { Name = "${local.name}-auth-keys" }
}

resource "aws_secretsmanager_secret_version" "auth_keys" {
  count = local.cognito_enabled ? 0 : 1

  secret_id     = aws_secretsmanager_secret.auth_keys[0].id
  secret_string = var.auth_keys

  lifecycle {
    ignore_changes = [secret_string]
  }
}
