# WHY THIS FILE EXISTS
#     Everything an operator must decide, in one place, with the temporary-deployment default
#     already chosen. Two rules run through it:
#
#     **A secret has no default.** `llm_api_key` and `tavily_api_key` are declared and never
#     valued here, so `terraform apply` refuses to run without them and no credential can arrive
#     by being forgotten. They are passed as `TF_VAR_*` environment variables
#     (docs/deployment.md), which is also why `terraform.tfvars` is gitignored.
#
#     **`db_password` is gone entirely**, and that is Block B's largest single improvement to
#     state exposure: RDS generates the master password and holds it in a secret it owns, so
#     there is no value for an operator to invent, type, or leave in `terraform.tfstate`
#     (docs/adr/0020-*.md decision 1).
#
#     **An application tunable is not repeated here.** MAX_REVISIONS, MAX_JOB_RUNTIME,
#     LLM_RPM_LIMIT and the rest have defaults in `config.py` and keep them; a second copy in
#     Terraform is a second source of truth that drifts. The only variables below that reach the
#     application are the ones AWS decides - an endpoint, a queue URL, a bucket name - plus the
#     provider credentials, which no default could supply.
#
# WHO USES IT
#     `terraform apply -var ...`, `TF_VAR_*` in the environment, and terraform.tfvars.example.

variable "region" {
  description = "AWS region. Everything is deployed into exactly one."
  type        = string
  default     = "ap-south-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name, and the value of the Project tag."
  type        = string
  default     = "competitive-research"
}

variable "environment" {
  description = "The value of the Env tag. `portfolio` says out loud that this is temporary."
  type        = string
  default     = "portfolio"
}

# --- What reaches the internet ------------------------------------------------------------

variable "allowed_ingress_cidrs" {
  description = <<-EOT
    Who may reach the ALB on port 80. The default is the whole internet, which is what a
    demo link needs. Narrow it to your own address (x.x.x.x/32) whenever the deployment is
    not being shown to someone else - the API's key table is the only thing in front of it.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "vpc_cidr" {
  description = "The VPC address range. 10.42/16 avoids the 10.0/16 default every other VPC uses."
  type        = string
  default     = "10.42.0.0/16"
}

# --- The image ----------------------------------------------------------------------------

variable "image_tag" {
  description = <<-EOT
    The tag of the image in ECR that all three task definitions run. **No default, on
    purpose**: guidelines section 19 requires images tagged with a commit SHA and never only
    `latest`, because rollback is redeploying the previous task definition and `latest` cannot
    name one.
  EOT
  type        = string
}

# --- Sizing. Every default is the smallest thing that runs ---------------------------------

variable "api_cpu" {
  description = "Fargate CPU units for the API task. 256 = 0.25 vCPU, the smallest Fargate size."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "MiB for the API task. 512 is the smallest value 256 CPU units allows."
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = <<-EOT
    Fargate CPU units for the worker. 512 rather than the API's 256 because this is the
    process that runs the graph: up to RESEARCHER_CONCURRENCY (3) page extractions in flight,
    PDF text extraction, and a checkpoint write per node.
  EOT
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "MiB for the worker task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = <<-EOT
    How many API tasks run. 1 for the temporary deployment. Production runs 2 across two AZs
    (ARCHITECTURE.md section 18), which is the difference between "a task restart is invisible"
    and "a task restart is thirty seconds of downtime".
  EOT
  type        = number
  default     = 1
}

variable "worker_desired_count" {
  description = <<-EOT
    How many worker tasks run. **1, and it must stay small.** Worker count is bounded by the
    LLM tier rather than by queue depth (ARCHITECTURE.md section 11), and autoscaling is
    deliberately absent. ARCHITECTURE.md section 18 names 2 for production throughput; one is
    enough to verify the system end to end and costs half as much.
  EOT
  type        = number
  default     = 1
}

# --- The database --------------------------------------------------------------------------

variable "db_instance_class" {
  description = "The smallest current-generation RDS class. Graviton, because it is cheaper than t3."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "GiB of gp3 storage. 20 is the minimum RDS accepts."
  type        = number
  default     = 20
}

variable "postgres_version" {
  description = <<-EOT
    Major version only, so AWS selects the current minor. **16 matches `postgres:16-alpine` in
    docker-compose.yml**, which is what makes the local `postgres`-marked test layer evidence
    about this database. No extension is required - nothing in this repository uses pgvector or
    any other non-default extension.
  EOT
  type        = string
  default     = "16"
}

variable "db_name" {
  description = "The database `alembic upgrade head` migrates and both processes connect to."
  type        = string
  default     = "research"
}

variable "db_username" {
  description = "The master user. One user for one application, as locally."
  type        = string
  default     = "research"
}

variable "rds_skip_final_snapshot" {
  description = <<-EOT
    `true` for the temporary deployment: a final snapshot is storage that keeps charging after
    `terraform destroy` reports success, which is the single most common way a torn-down
    environment still costs money. Set `false` for anything whose data matters.
  EOT
  type        = bool
  default     = true
}

# --- Redis ---------------------------------------------------------------------------------

variable "redis_node_type" {
  description = "The smallest current-generation cache node."
  type        = string
  default     = "cache.t4g.micro"
}

variable "redis_engine_version" {
  description = <<-EOT
    Redis OSS 7.1, matching `redis:7-alpine` locally. The shared rate limiter is one Lua
    script (`redisstore.py`), so the engine has to support EVAL - Redis 7 and Valkey 7.2 both
    do. See docs/deployment.md for the Valkey trade.
  EOT
  type        = string
  default     = "7.1"
}

# --- Storage and logs ----------------------------------------------------------------------

variable "s3_force_destroy" {
  description = <<-EOT
    Whether `terraform destroy` may delete report objects with the bucket. `true` for the
    temporary deployment, because a bucket that refuses to delete leaves the whole destroy
    half-finished. **Take your evidence before you destroy** - nothing else keeps the reports.
  EOT
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch Logs retention. 1 day, because the deployment lives for an hour and log storage
    is one of the few things that keeps charging after every task has stopped.
  EOT
  type        = number
  default     = 1
}

# --- What the worker needs to call a model -------------------------------------------------

variable "llm_base_url" {
  description = "OpenAI-compatible endpoint. Worker-only (ADR 0012); the API never sees it."
  type        = string
}

variable "llm_model" {
  description = "Main model id - planning, extraction, writing, fact-checking."
  type        = string
}

variable "llm_fast_model" {
  description = "Routing and scoring model id. Empty falls back to llm_model, as config.py does."
  type        = string
  default     = ""
}

variable "llm_api_key" {
  description = "LLM credential. **No default.** Worker-only."
  type        = string
  sensitive   = true
}

variable "tavily_api_key" {
  description = "Web-search credential. **No default.** Worker-only."
  type        = string
  sensitive   = true
}

variable "auth_keys" {
  description = <<-EOT
    The API's hashed key table, as JSON mapping a sha256 of each key to its user_id and role
    (guidelines section 16). **Used only when auth_mode is `api_key`**, and empty otherwise:
    under Cognito the API holds no shared secret and no auth-keys secret is created.

    The default is empty rather than absent so that the Cognito deployment - the one this block
    is for - needs no value at all. In `api_key` mode a value is required, and the validation
    below is what refuses an apply that forgot it. The two published development keys in
    .env.example must never be used here.
  EOT
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.auth_mode == "cognito" || trimspace(var.auth_keys) != ""
    error_message = "auth_keys is required when auth_mode is api_key."
  }
}

# --- Block B: how a caller proves who they are ------------------------------------------------

variable "auth_mode" {
  description = <<-EOT
    Which credential the API accepts, and **only one is live at a time**
    (docs/adr/0020-*.md decision 2). `cognito` verifies a Cognito access token against the
    pool's published signing keys and is the default for this deployment; `api_key` keeps the
    Phase 2 hashed key table, which is what every local command and the whole offline suite
    still use.

    An API that accepted either would be exactly as strong as the weaker of the two, and the
    weaker one is a shared secret with no expiry.
  EOT
  type        = string
  default     = "cognito"

  validation {
    condition     = contains(["cognito", "api_key"], var.auth_mode)
    error_message = "auth_mode must be cognito or api_key."
  }
}

variable "certificate_arn" {
  description = <<-EOT
    An **existing, already-validated** ACM certificate in this region. Set it and the load
    balancer gains an HTTPS listener on 443 while port 80 redirects to it; leave it empty and
    the deployment stays plain HTTP, which is the default because this repository owns no
    domain (docs/adr/0020-*.md decision 5).

    Nothing here creates a certificate or a hosted zone. A public ACM certificate cannot be
    issued without a domain name to validate against, and inventing one would mean a Route 53
    hosted zone that charges per month and outlives an hour-long deployment.

    **With this empty, the bearer token travels in clear text.** Use throwaway credentials, and
    read the trade in docs/deployment.md before showing the link to anyone.
  EOT
  type        = string
  default     = ""
}

variable "log_level" {
  description = "LOG_LEVEL for both processes."
  type        = string
  default     = "INFO"
}
