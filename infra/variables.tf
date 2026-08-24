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
#     **An application tunable is not repeated here.** MAX_LLM_CALLS_PER_JOB, LLM_RPM_LIMIT,
#     REFLECTION_PASS_THRESHOLD, RESEARCHER_CONCURRENCY and the rest have defaults in `config.py`
#     and keep them; a second copy in Terraform is a second source of truth that drifts. Most of
#     the variables below reach the application only as the things AWS decides - an endpoint, a
#     queue URL, a bucket name - plus the provider credentials, which no default could supply.
#
#     **Four exceptions, and they are stated rather than snuck in:** `llm_main_timeout_s`,
#     `max_revisions`, `max_supervisor_hops` and `max_job_runtime`. Those four describe the
#     deployed endpoint rather than a preference - both published n=20 baselines ran at
#     180/3/30/1800 and `config.py`'s defaults were never those numbers - and a request timeout
#     that could only be changed by editing `ecs.tf` is one nobody can change on the day.
#     `config.py` keeps its own defaults untouched, so the local suite is unaffected, and all
#     four reach the **worker only**: no other process runs a graph node.
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
    The tag of the image in ECR that all four task definitions run. **No default, on
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
    CloudWatch Logs retention, applied to all three ECS log groups. **1 day, and explicit.**

    A log group ECS creates on its own has retention "Never expire", which is storage that keeps
    charging long after every task has stopped - which is why the three groups are
    Terraform-managed and why this number exists at all rather than being left to a default.

    One day is right for a deployment that lives an hour and is destroyed. It is deliberately
    shorter than the dead-letter queue's 14-day message retention: the evidence that a job
    failed outlives the log lines explaining it, which is a trade a longer-lived environment
    should reverse. docs/deployment.md section 9 names 30 days for production.
  EOT
  type        = number
  default     = 1
}

# --- Block C: the operational alarms ---------------------------------------------------------
#
# Four numbers and one switch. Each threshold is derived from something this system already
# decided rather than chosen for looking reasonable, and each is a variable so an operator can
# move it without editing monitoring.tf. **None of them is claimed to be production-optimal** -
# they are right for one worker, one API task and a deployment that lives an hour.

variable "create_alarms_topic" {
  description = <<-EOT
    Whether to create the SNS topic the alarms notify. An SNS topic with nothing subscribed to
    it costs nothing, so the default is `true` and every alarm has a real action to name; who
    hears about it is an operator's decision, made afterwards with `aws sns subscribe`.

    **No email subscription is created here, on purpose.** That would put an address in a
    repository and send a confirmation mail on every apply.

    Set it to `false` and the alarms still exist, still change state and are still visible in
    the console - they simply notify nothing, which is often all a one-hour deployment needs.
  EOT
  type        = bool
  default     = true
}

variable "queue_age_alarm_seconds" {
  description = <<-EOT
    How old the oldest jobs-queue message may get before the alarm fires. **This is the
    worker-liveness alarm**: a worker that is not consuming is a queue that is ageing.

    3600 is derived rather than picked. `ApproximateAgeOfOldestMessage` counts in-flight
    messages, so a job the worker is legitimately running ages it - and one invocation is
    bounded at `MAX_JOB_RUNTIME` (1200s). Meanwhile a message can spend three deliveries of the
    1800-second visibility window in flight before it is dead-lettered, which is 5400s. An hour
    sits between the two: longer than any single legitimate invocation, and early enough to fire
    while redelivery is still happening rather than after the dead-letter alarm already has.

    A job waiting at the human gate does not age this metric at all - its message was deleted
    when the gate interrupted - so no threshold here can be tripped by a slow reviewer.
  EOT
  type        = number
  default     = 3600
}

variable "rds_free_storage_alarm_bytes" {
  description = <<-EOT
    How little free storage RDS may report before the alarm fires. The default is 2 GiB of the
    20 GiB `db_allocated_storage` allocates, so it fires at 10% remaining.

    A full disk is the one RDS condition that is unrecoverable in place: every checkpoint, every
    audit row and every terminal status is a write. 10% of 20 GiB is roughly a day of headroom
    at any rate this deployment can produce, which is early enough to react and late enough not
    to fire on an idle environment.

    Raise it with `db_allocated_storage` - a percentage-shaped threshold would need a metric
    math expression for a number that changes once.
  EOT
  type        = number
  default     = 2147483648
}

variable "redis_memory_alarm_percent" {
  description = <<-EOT
    The `DatabaseMemoryUsagePercentage` at which the cache alarm fires.

    80 is a conventional headroom figure and is stated as one. What makes it worth watching here
    is specific: everything in Redis fails open except the shared rate limiter, and an evicted
    `ratelimit:llm` key does not break anything - it silently widens the window every worker is
    sharing. Over-permission is the failure mode, which is the kind nobody notices.
  EOT
  type        = number
  default     = 80
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

# --- The four worker runtime bounds the deployment sets explicitly --------------------------
#
# **The one place this file departs from "an application tunable is not repeated here", and the
# reason is that these four are not tunables - they are the deployed endpoint's shape.**
# `config.py`'s defaults (60s, 2, 24, 1200s) are right for a fast endpoint and were never the
# values either published baseline ran at: both n=20 runs used 180/3/30/1800 against NVIDIA NIM,
# and a 60-second request timeout against that endpoint is the most likely way a deployed job
# fails for a reason that is not a defect.
#
# **They are variables rather than a second set of hard-coded numbers** so an operator can move
# one with `TF_VAR_*` without editing ecs.tf mid-demo, which is exactly the situation the
# pre-deployment audit found: a value that could not be changed without a code edit.
#
# **Worker-only, all four.** The API runs no node, calls no model and has no job runtime, so
# giving it any of them would describe behaviour it does not have. `config.py` keeps its
# defaults untouched, so every local command and the whole offline suite are unchanged.

variable "llm_main_timeout_s" {
  description = <<-EOT
    `LLM_MAIN_TIMEOUT_S` for the worker: the request timeout for every main-tier caller -
    Planner, Researcher extraction, Synthesizer, Fact-Checker. There is no per-agent timeout.

    180 rather than config.py's 60, because that is what both published baselines measured
    against, and because a request timeout shorter than the endpoint's real latency turns a
    working deployment into a job that fails after paying for its own retries. Lower it for a
    fast endpoint.
  EOT
  type        = number
  default     = 180
}

variable "max_revisions" {
  description = <<-EOT
    `MAX_REVISIONS`: how many automatic improvement cycles reflection may run after the first
    report. 3 means at most 4 report-producing passes.

    3 rather than config.py's 2 for the same reason as the timeout - it is what the measured
    runs used. Hitting the cap stays a visible outcome carried to the reviewer (invariant 2),
    which this number moves but does not change.
  EOT
  type        = number
  default     = 3
}

variable "max_supervisor_hops" {
  description = <<-EOT
    `MAX_SUPERVISOR_HOPS`: the routing loop guard.

    30 rather than config.py's 24. It has to rise with `max_revisions`: each extra revision
    cycle legitimately costs hops, so raising the revision cap while leaving the hop guard at
    the ceiling derived for 2 revisions would fail jobs on the guard rather than on the cap.
    It remains a guard against a routing loop, not a budget - `MAX_LLM_CALLS_PER_JOB` is still
    the binding ceiling and is deliberately not a variable here.
  EOT
  type        = number
  default     = 30
}

variable "max_job_runtime" {
  description = <<-EOT
    `MAX_JOB_RUNTIME`: the no-new-node deadline for **one worker invocation**, in seconds. Not
    a hard wall and not a job's lifetime - a job waiting three days at the human gate does not
    fail on resume ([ADR 0010](../docs/adr/0010-*.md) decision 7, clarified by ADR 0015).

    1800 rather than config.py's 1200, matching the measured runs.

    **It equals `visibility_timeout_seconds` on the jobs queue, and that is a coincidence of
    value rather than a coupling.** The two never interact: the deadline is a `time.monotonic()`
    comparison made after a node's checkpoint is durable, while ownership of the delivery is
    kept by ADR 0015's background heartbeat, which renews every `V/3` derived from the queue's
    own attribute and keeps renewing for as long as the invocation legitimately owns the
    message. Nothing in `worker.check_queue` compares the two. Raising this above 1800 would
    still be safe for the same reason - it is bounded by the lease, not by this number.
  EOT
  type        = number
  default     = 1800
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
