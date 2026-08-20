# WHY THIS FILE EXISTS
#     The four stores the application already has, as the AWS services they were always meant
#     to be. Nothing here is a new capability: docker-compose.yml starts PostgreSQL 16, Redis 7
#     and LocalStack's SQS and S3, and `docker/localstack-init/01-create-queues-and-bucket.sh`
#     already declares the queue's shape. **This file has to agree with that script**, because
#     the `integration`-marked test layer is evidence about these settings and only stays
#     evidence while the two match.
#
#     **Every sizing choice here is the smallest one that works, and each is reversible.** A
#     temporary deployment does not need Multi-AZ, a read replica, a cache replica, backups, or
#     Performance Insights, and each of those is either a per-hour charge or a storage charge
#     that outlives `terraform destroy`.
#
# WHO USES IT
#     ecs.tf, which turns these endpoints into DATABASE_URL, REDIS_URL, SQS_QUEUE_URL and
#     S3_BUCKET, and outputs.tf.

# --- PostgreSQL ---------------------------------------------------------------------------
#
# It holds the five application tables Alembic migrates and LangGraph's checkpoint tables
# beside them, which is why this is the one store a job cannot lose.

resource "aws_db_instance" "postgres" {
  identifier     = "${local.name}-postgres"
  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username

  # **The one Block B change in this file, and it is the one that matters most for state.**
  # RDS generates the master password itself, stores it in a Secrets Manager secret it owns,
  # and never shows it to Terraform. `var.db_password` is gone: there is no value to pass, no
  # value to keep in a shell variable, and - the point - **no password anywhere in
  # `terraform.tfstate`**, which is a plain JSON file on the operator's laptop.
  #
  # The cost is that nothing upstream can compose a connection string, because the secret holds
  # `{"username", "password"}` and no host. ecs.tf therefore injects the two halves and the
  # container assembles the URL (`config.resolve_database_url`, ADR 0020 decision 1).
  #
  # No `master_user_secret_kms_key_id`: the default is the AWS-managed `aws/secretsmanager`
  # key, which is free. A customer-managed key would be a per-month charge and two more grants.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.postgres.id]

  # **The two that matter.** `publicly_accessible = false` keeps the instance off a public
  # address entirely; the private subnets and the security group are the second and third
  # answers to the same question.
  publicly_accessible = false
  multi_az            = false

  # No automated backups: a one-hour environment has nothing to recover to, and retained
  # backups are storage that keeps charging after the instance is gone.
  backup_retention_period = 0

  # The teardown pair. Deletion protection off so `terraform destroy` is not a two-step, and
  # the final snapshot skipped for the reason var.rds_skip_final_snapshot gives - a retained
  # snapshot is the most common way a destroyed environment still costs money.
  skip_final_snapshot = var.rds_skip_final_snapshot
  deletion_protection = false

  # An hour-long deployment must not take a maintenance-window version change mid-demo, and
  # must apply what it is told immediately rather than at 03:00.
  apply_immediately          = true
  auto_minor_version_upgrade = false

  # Both are per-hour charges that answer questions this deployment is not asking.
  performance_insights_enabled = false
  monitoring_interval          = 0

  tags = { Name = "${local.name}-postgres" }
}

# --- Redis --------------------------------------------------------------------------------
#
# `redisstore.py` holds two caches and a per-job URL set that fail open, and the shared rate
# limiter that fails closed. The limiter is one Lua script, so the engine must support EVAL -
# which is the only compatibility requirement this application places on it.

resource "aws_elasticache_cluster" "redis" {
  cluster_id = "${local.name}-redis"
  engine     = "redis"

  # `aws_elasticache_cluster` rather than `aws_elasticache_replication_group` **because it is
  # the cheaper shape and it keeps the application unchanged**: a single node with no
  # replication group has no in-transit encryption and no auth token, so the URL stays
  # `redis://host:6379/0` and `redisstore.build_redis` needs no TLS argument. Production wants
  # the opposite trade - see docs/deployment.md, where it becomes a replication group with TLS
  # and a `rediss://` URL, which `Redis.from_url` already understands.
  engine_version       = var.redis_engine_version
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  # Everything in Redis here is a cache, a 6-hour URL set, or a 60-second rate-limit window.
  # None of it survives usefully, so a snapshot would be storage for nothing.
  snapshot_retention_limit = 0
  apply_immediately        = true

  tags = { Name = "${local.name}-redis" }
}

# --- SQS ------------------------------------------------------------------------------------
#
# The numbers below are not chosen here. FIFO is ADR 0010 decision 4, the 1800-second visibility
# window is ADR 0015's, and three deliveries then the DLQ is ARCHITECTURE.md section 11. All
# three already exist in docker/localstack-init, and the worker refuses to start against a queue
# that disagrees (`worker.check_queue`).

resource "aws_sqs_queue" "jobs_dlq" {
  name       = "${local.name}-jobs-dlq.fifo"
  fifo_queue = true

  # False on both queues, because `jobqueue.py` sends an explicit MessageDeduplicationId - the
  # job's idempotency key, or ADR 0007's gate-visit key. Content-based deduplication would
  # hash the body instead and silently drop a legitimate second visit.
  content_based_deduplication = false

  # 14 days, the maximum. A dead-lettered message is the evidence for why a job failed, and it
  # must outlive the demo rather than the deployment.
  message_retention_seconds = 1209600

  tags = { Name = "${local.name}-jobs-dlq" }
}

resource "aws_sqs_queue" "jobs" {
  name       = "${local.name}-jobs.fifo"
  fifo_queue = true

  content_based_deduplication = false

  # ADR 0015: the worker renews this lease every one third of it. It is not a prediction of how
  # long a node takes.
  visibility_timeout_seconds = 1800

  # The client already asks for a 20-second long poll on every receive (`jobqueue.LONG_POLL_SECONDS`).
  # Setting the queue default to match means a receive from anything else behaves the same way.
  receive_wait_time_seconds = 20

  message_retention_seconds = 345600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })

  tags = { Name = "${local.name}-jobs" }
}

# --- S3 ---------------------------------------------------------------------------------------
#
# One object per exported job, at `reports/{job_id}.json` (`artifacts.object_key`). The worker
# writes it; the API signs a 15-minute URL for it; nothing else touches it.

# The bucket namespace is global, so the name needs something the next deployment will not
# collide with. A random suffix rather than the account id: an account id in a bucket name is an
# account id printed on every URL.
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "reports" {
  bucket = "${local.name}-reports-${random_id.bucket_suffix.hex}"

  # Whether `terraform destroy` may delete the objects with the bucket. True for a temporary
  # deployment; see the warning on var.s3_force_destroy.
  force_destroy = var.s3_force_destroy

  tags = { Name = "${local.name}-reports" }
}

# **All four, on.** A presigned URL is how a report is downloaded, which means nothing in this
# design ever needs the bucket or an object in it to be public.
resource "aws_s3_bucket_public_access_block" "reports" {
  bucket = aws_s3_bucket.reports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ACLs disabled entirely. The only writer is the worker's task role, so object ownership never
# needs to be expressed as an ACL, and an ACL is the other way a bucket accidentally goes public.
resource "aws_s3_bucket_ownership_controls" "reports" {
  bucket = aws_s3_bucket.reports.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3 rather than SSE-KMS: encryption at rest with no per-request KMS charge and no key to
# grant the two task roles. A customer-managed key is a Block B decision, not a Block A default.
resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# **The one lifecycle rule, and what it deliberately is not.** It cleans up multipart uploads
# that never finished, which cost storage and are invisible in the console. There is **no
# expiration rule**: reports are the evidence this deployment exists to produce, and a rule that
# deleted them on a schedule could delete them before the screenshots were taken. Retention is a
# Phase 5 sweep concern (ARCHITECTURE.md, "nothing removes an artifact, ever").
resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
