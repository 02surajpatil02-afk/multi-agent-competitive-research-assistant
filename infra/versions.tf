# WHY THIS FILE EXISTS
#     One IaC tool, one state, one region. Terraform was chosen over CDK and CloudFormation for
#     the reason this repository picks anything: it is the smallest thing that does the job.
#     `terraform plan` answers "what would change" without an account mutation, `terraform
#     destroy` answers "is anything still charging" in one command, and neither needs a Node
#     toolchain or a second language runtime beside the Python this project already pins.
#
#     **This configuration describes a TEMPORARY portfolio deployment.** It is deployed, verified,
#     screenshotted, and destroyed - roughly an hour of life. Where that differs from what a
#     long-running production deployment should look like, the difference is written down in
#     docs/deployment.md rather than silently built.
#
# WHO USES IT
#     `terraform -chdir=infra init | plan | apply | destroy`, run by a person following
#     docs/deployment.md. Nothing in CI deploys; CI only formats and validates.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60.0, < 7.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6.0, < 4.0.0"
    }
  }
}

# No `backend` block. State is local, in infra/terraform.tfstate, and .gitignore keeps it out of
# the repository - it holds the database password and every generated name. A remote backend is
# the right answer for a deployment more than one person operates, and is listed in
# docs/deployment.md as a production difference rather than built for an environment that exists
# for an hour.

provider "aws" {
  region = var.region

  # Every resource this configuration creates carries the same three tags, which is what makes
  # the teardown check in docs/deployment.md a query rather than a memory test.
  default_tags {
    tags = local.tags
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = var.name_prefix

  tags = {
    Project   = var.name_prefix
    Env       = var.environment
    ManagedBy = "terraform"
  }

  # Two availability zones, because an ALB requires subnets in at least two and an RDS subnet
  # group requires the same. **This is not a high-availability claim**: the database is
  # explicitly Single-AZ and each service runs one task. Two AZs is the minimum the services
  # accept, not a redundancy decision.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  # The two Block B switches, resolved once so no file has to re-derive them (ADR 0020).
  #
  # `cognito_enabled` decides three things together and they must agree: whether the user pool
  # exists, whether the API is told to verify tokens, and whether an auth-keys secret is created
  # and granted. Under Cognito the API holds no shared secret at all.
  cognito_enabled = var.auth_mode == "cognito"

  # HTTPS exists exactly when an operator supplies a certificate. Nothing here creates one - see
  # alb.tf for why a certificate needs a domain this repository does not own.
  https_enabled = var.certificate_arn != ""

  # --- The Block C+ switch: which model provider the worker calls (ADR 0022) ---------------
  #
  # `bedrock_enabled` decides four things together and they must agree: what the worker's
  # environment says, whether an LLM API-key secret exists at all, whether the worker's
  # execution role may fetch one, and whether the worker's task role carries
  # `bedrock:InvokeModel`. Under Bedrock the deployment holds **no model-provider credential**.
  bedrock_enabled = var.llm_provider == "bedrock"

  bedrock_region = var.bedrock_region != "" ? var.bedrock_region : var.region

  # --- Which Bedrock resources the worker may invoke ----------------------------------------
  #
  # **Read this before widening it, because `Resource = "*"` is the obvious wrong answer.**
  # Bedrock authorizes an invocation through a cross-region inference profile against two kinds
  # of resource: the profile itself, which lives in this account and this Region, and the
  # foundation model in whichever Region the request is routed to, which is AWS-owned and has no
  # account id in its ARN. Naming only the first produces an `AccessDeniedException` on the
  # first job; naming neither and writing `*` grants every model in every Region.
  #
  # So the two are derived here, from the one id the application actually sends:
  #
  #   `bedrock_model_ref`     - the id, whether it arrived bare or inside an ARN.
  #   `bedrock_is_profile`    - **the segment count is what tells them apart.** A foundation
  #                             model id is `provider.model` and a cross-region inference
  #                             profile id puts a geo prefix in front of it, so
  #                             `amazon.nova-pro-v1:0` has two dot-separated segments and
  #                             `apac.amazon.nova-pro-v1:0` has three. Matching the prefix
  #                             itself is what does not work: the list is `us`, `eu`, `apac`,
  #                             `jp`, `global` and more, and any pattern loose enough to hold
  #                             all of them also matches the `amazon.` of a bare model id.
  #   `bedrock_base_model_id` - the same id with that prefix dropped, which is the foundation
  #                             model the profile routes to.
  #
  # The destination Regions cannot be derived - they are a property of the profile that nothing
  # here reads - so they are an explicit input, `var.bedrock_inference_profile_regions`, and
  # empty means this Region only (docs/adr/0022-*.md decision 9).
  bedrock_model_ref = startswith(var.bedrock_model_id, "arn:") ? element(split("/", var.bedrock_model_id), 1) : var.bedrock_model_id

  bedrock_model_segments = split(".", local.bedrock_model_ref)

  bedrock_is_profile = length(local.bedrock_model_segments) > 2

  bedrock_base_model_id = local.bedrock_is_profile ? join(".", slice(local.bedrock_model_segments, 1, length(local.bedrock_model_segments))) : local.bedrock_model_ref

  bedrock_profile_arns = local.bedrock_is_profile ? [
    startswith(var.bedrock_model_id, "arn:") ? var.bedrock_model_id : "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${local.bedrock_model_ref}"
  ] : []

  bedrock_destination_regions = length(var.bedrock_inference_profile_regions) > 0 ? var.bedrock_inference_profile_regions : [local.bedrock_region]

  bedrock_foundation_model_arns = [
    for destination in local.bedrock_destination_regions :
    "arn:aws:bedrock:${destination}::foundation-model/${local.bedrock_base_model_id}"
  ]

  bedrock_invoke_arns = concat(local.bedrock_profile_arns, local.bedrock_foundation_model_arns)
}
