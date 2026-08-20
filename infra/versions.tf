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
}
