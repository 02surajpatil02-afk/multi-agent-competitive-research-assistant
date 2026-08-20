# WHY THIS FILE EXISTS
#     Phase 5 block C: six CloudWatch alarms, one optional SNS topic, and nothing else.
#
#     **Every alarm below reads a metric AWS already publishes for free.** No custom metric, no
#     `PutMetricData` call, no agent, no sidecar, and no application change - which is deliberate
#     rather than lazy: an operational signal the application has to remember to emit is a signal
#     that goes quiet exactly when the application is broken. What each alarm watches is the
#     queue, the load balancer, the database and the cache themselves.
#
#     **Container Insights stays off, and that costs two alarms this block would otherwise have.**
#     `RunningTaskCount` lives in the `ECS/ContainerInsights` namespace, which is a per-metric
#     CloudWatch charge; the plain `AWS/ECS` namespace publishes only CPU and memory utilisation
#     and has no task count at all. So "is the API running?" is answered by the ALB's unhealthy
#     target count, and "is the worker running?" is answered by the age of the oldest message on
#     the jobs queue - a worker that is not consuming is a queue that is ageing, which is the
#     symptom that actually matters. docs/deployment.md section 9 records that a long-running
#     deployment should turn Container Insights on and alarm on the task counts directly.
#
#     **Every threshold below is explainable, and none of them is claimed to be universal.** The
#     four that are judgement calls are variables, so an operator can move them without editing
#     this file - see the reasoning on each one in variables.tf.
#
#     **No dashboard, no Lambda, no EventBridge rule, no Step Function.** A dashboard is a second
#     place to keep true; the rest would be an operational service running full time for an
#     environment that lives an hour, to do work three deterministic scripts already do on
#     demand (docs/adr/0021-*.md decision 7).
#
# WHO USES IT
#     `terraform apply`, and the alarm entries in docs/runbook.md.

# --- Where an alarm goes -----------------------------------------------------------------
#
# **A topic and no subscription.** An SNS topic with nothing subscribed costs nothing and is
# created so that every alarm has a real action to name; who hears about it is an operator's
# decision, made with one command:
#
#     aws sns subscribe --topic-arn "$(terraform -chdir=infra output -raw alarms_topic_arn)" \
#       --protocol email --notification-endpoint you@example.com
#
# Creating that subscription here would put an address in a repository and send a confirmation
# email on every apply. Setting `create_alarms_topic = false` leaves the alarms in place with no
# action at all - they still change state and are still visible in the console, which is what a
# one-hour deployment usually needs.

resource "aws_sns_topic" "alarms" {
  count = var.create_alarms_topic ? 1 : 0

  name = "${local.name}-alarms"

  tags = { Name = "${local.name}-alarms" }
}

locals {
  alarm_actions = var.create_alarms_topic ? [aws_sns_topic.alarms[0].arn] : []
}

# --- 1. The dead-letter queue is not empty -------------------------------------------------
#
# **Threshold: more than zero visible messages. Any message here needs a person**, because a
# message reaches this queue only after three deliveries failed, and the worker's last delivery
# also ends the job `failed` with `job_dead_lettered` (ADR 0010 decision 9). So this alarm does
# not mean "a job is stuck" - the job is already terminal - it means "three attempts at
# something could not be made to work, and nobody has looked at why".
#
# `period = 300` because SQS publishes these queue metrics on a five-minute cadence; a shorter
# period would produce gaps rather than faster detection. Missing data is `notBreaching`: an
# empty FIFO queue reports nothing, and an alarm that fired on silence would fire immediately
# and permanently on a healthy deployment.

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name = "${local.name}-dlq-not-empty"
  alarm_description = join(" ", [
    "A message is in the jobs dead-letter queue: three deliveries failed.",
    "Run scripts/inspect_dlq.py, then reconcile_jobs.py or replay_dlq.py (docs/runbook.md)."
  ])

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = aws_sqs_queue.jobs_dlq.name }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = { Name = "${local.name}-dlq-not-empty" }
}

# --- 2. The jobs queue is ageing ------------------------------------------------------------
#
# **This is the worker-liveness alarm**, and it is a queue metric rather than a task metric for
# the reason at the top of this file. `ApproximateAgeOfOldestMessage` counts in-flight messages
# too, so a job the worker is legitimately running for twenty minutes does age this metric -
# which is exactly why the default threshold is an hour rather than a minute.
#
# **The default is derived, not picked** (var.queue_age_alarm_seconds): it is longer than any
# one invocation is allowed to run (`MAX_JOB_RUNTIME` = 1200s) and shorter than the 5400 seconds
# a message can spend across three 1800-second deliveries before it is dead-lettered. So it
# fires while redelivery is still happening, rather than after alarm 1 has already told you.
#
# A job waiting at the human gate does **not** age this metric: the worker deletes the message
# when the gate interrupts (ADR 0010 decision 6), so a three-day review is invisible here. That
# is the correct behaviour and the reason no alarm covers "waiting for a reviewer" at all.

resource "aws_cloudwatch_metric_alarm" "jobs_queue_backlog_age" {
  alarm_name = "${local.name}-jobs-queue-backlog-age"
  alarm_description = join(" ", [
    "The oldest jobs-queue message is older than the threshold: the worker is probably not",
    "consuming. Check the worker service and its log group (docs/runbook.md)."
  ])

  namespace   = "AWS/SQS"
  metric_name = "ApproximateAgeOfOldestMessage"
  dimensions  = { QueueName = aws_sqs_queue.jobs.name }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.queue_age_alarm_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = { Name = "${local.name}-jobs-queue-backlog-age" }
}

# --- 3. The API has an unhealthy target ------------------------------------------------------
#
# The nearest thing to "is the API task up?" that exists without Container Insights, and in one
# way it is better: it reports what the load balancer can actually reach, so it covers a task
# that is running and failing `/health` as well as one that is not running at all.
#
# **Ten one-minute periods, and the length is the point.** `/health` answers 503 with
# `checks.checkpoints = false` until the first worker has called `setup()`, so a fresh
# deployment is legitimately unhealthy for a few minutes (docs/deployment.md section 3). A
# shorter window would make every single deploy page somebody. Ten minutes is comfortably longer
# than that window and still far shorter than a person would notice on their own.

resource "aws_cloudwatch_metric_alarm" "api_unhealthy_targets" {
  alarm_name = "${local.name}-api-unhealthy-targets"
  alarm_description = join(" ", [
    "The API target group has an unhealthy target for ten minutes. Expected briefly at",
    "startup while checks.checkpoints is false; otherwise check /health (docs/runbook.md)."
  ])

  namespace   = "AWS/ApplicationELB"
  metric_name = "UnHealthyHostCount"
  dimensions = {
    TargetGroup  = aws_lb_target_group.api.arn_suffix
    LoadBalancer = aws_lb.api.arn_suffix
  }

  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 10
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = { Name = "${local.name}-api-unhealthy-targets" }
}

# --- 4. The API is answering 5xx --------------------------------------------------------------
#
# Different from alarm 3 and worth its own line: a target can be perfectly healthy and still
# return `500 internal_error`, which is the envelope `app.py` produces for anything nobody
# planned for. `HTTPCode_Target_5XX_Count` counts what the application returned, not what the
# load balancer returned, so an ALB 502 from a dead target is alarm 3's job and this one is
# about the application's own answers.
#
# **Threshold: more than zero in five minutes.** This deployment serves a handful of requests
# by hand, so one 5xx is a real event rather than noise. A production deployment would use a
# rate against request count instead, and docs/deployment.md section 9 says so.

resource "aws_cloudwatch_metric_alarm" "api_target_5xx" {
  alarm_name = "${local.name}-api-target-5xx"
  alarm_description = join(" ", [
    "The API returned a 5xx. Read the api log group; the response body carries a stable",
    "error code and the job id (docs/runbook.md)."
  ])

  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_Target_5XX_Count"
  dimensions = {
    TargetGroup  = aws_lb_target_group.api.arn_suffix
    LoadBalancer = aws_lb.api.arn_suffix
  }

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = { Name = "${local.name}-api-target-5xx" }
}

# --- 5. The database is running out of storage -------------------------------------------------
#
# The one RDS condition that is both plausible here and unrecoverable if it is reached: a full
# disk stops every write, which means every checkpoint, every audit row and every terminal
# status. CPU and connection alarms are deliberately absent - one worker and one API task on a
# `db.t4g.micro` do not approach either, and an alarm nobody has a response to is noise.
#
# **Threshold: 2 GiB free of the 20 GiB allocated** (var.rds_free_storage_alarm_bytes), so it
# fires at 10% remaining. `Minimum` rather than `Average`, because the question is whether it
# ever got that low. Missing data is left as `missing` rather than `notBreaching`: an RDS
# instance that has stopped publishing is not a healthy one, and holding the previous state is
# more honest than declaring it fine.

resource "aws_cloudwatch_metric_alarm" "rds_free_storage_low" {
  alarm_name = "${local.name}-rds-free-storage-low"
  alarm_description = join(" ", [
    "RDS free storage is below the threshold. Every checkpoint, audit row and terminal status",
    "is a write; a full disk stops all of them (docs/runbook.md)."
  ])

  namespace   = "AWS/RDS"
  metric_name = "FreeStorageSpace"
  dimensions  = { DBInstanceIdentifier = aws_db_instance.postgres.identifier }

  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.rds_free_storage_alarm_bytes
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = { Name = "${local.name}-rds-free-storage-low" }
}

# --- 6. The cache is under memory pressure -------------------------------------------------------
#
# Redis holds two caches, a per-job URL set and the shared rate limiter. The first three fail
# open and cost a call each; the limiter is the one that matters, and **an evicted
# `ratelimit:llm` key silently widens the window every worker is sharing** - the failure mode is
# over-permission rather than an outage, which is precisely the kind that goes unnoticed.
#
# `DatabaseMemoryUsagePercentage` rather than `Evictions` because it is the earlier signal:
# by the time something has been evicted the limiter may already have been reset. Two periods
# rather than one, because a single five-minute spike on a `cache.t4g.micro` is not news.

resource "aws_cloudwatch_metric_alarm" "redis_memory_pressure" {
  alarm_name = "${local.name}-redis-memory-pressure"
  alarm_description = join(" ", [
    "ElastiCache memory usage is high. An evicted ratelimit:llm key widens the shared LLM",
    "window rather than breaking anything, which is why this is worth watching."
  ])

  namespace   = "AWS/ElastiCache"
  metric_name = "DatabaseMemoryUsagePercentage"
  dimensions  = { CacheClusterId = aws_elasticache_cluster.redis.cluster_id }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.redis_memory_alarm_percent
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = { Name = "${local.name}-redis-memory-pressure" }
}
