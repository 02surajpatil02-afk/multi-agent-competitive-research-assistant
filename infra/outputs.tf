# WHY THIS FILE EXISTS
#     Everything docs/deployment.md's deploy, verify and teardown steps need to name, so no step
#     asks anyone to read an identifier out of the console.
#
#     **No output is a secret.** The database password, the LLM key, the search key and the auth
#     key table are inputs and stay inputs; nothing here would print one, and no output is marked
#     `sensitive` because none needs to be. An endpoint address is not a credential - the RDS and
#     Redis endpoints below resolve to private addresses reachable only from inside this VPC.
#
# WHO USES IT
#     `terraform -chdir=infra output`, and every command in docs/deployment.md that takes a name.

output "api_url" {
  description = "The deployment's front door. `curl $(terraform output -raw api_url)/health`."
  value       = "http://${aws_lb.api.dns_name}"
}

output "alb_dns_name" {
  description = "The load balancer's DNS name on its own, for a health-check or listener lookup."
  value       = aws_lb.api.dns_name
}

output "ecr_repository_url" {
  description = "Where `docker push` sends the one image all three task definitions run."
  value       = aws_ecr_repository.app.repository_url
}

output "jobs_queue_url" {
  description = "SQS_QUEUE_URL, as both processes receive it."
  value       = aws_sqs_queue.jobs.url
}

output "jobs_queue_name" {
  description = "The FIFO job queue's name, for `aws sqs get-queue-attributes` during verification."
  value       = aws_sqs_queue.jobs.name
}

output "jobs_dlq_url" {
  description = "The dead-letter queue. A message here means three deliveries failed."
  value       = aws_sqs_queue.jobs_dlq.url
}

output "jobs_dlq_name" {
  description = "The dead-letter queue's name."
  value       = aws_sqs_queue.jobs_dlq.name
}

output "reports_bucket" {
  description = "S3_BUCKET. Objects land at reports/{job_id}.json."
  value       = aws_s3_bucket.reports.id
}

output "rds_endpoint" {
  description = "host:port for PostgreSQL. Private: it resolves only inside the VPC."
  value       = aws_db_instance.postgres.endpoint
}

output "redis_endpoint" {
  description = "The cache node's private address."
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "ecs_cluster_name" {
  description = "The cluster both services and the migration task run in."
  value       = aws_ecs_cluster.main.name
}

output "api_service_name" {
  description = "The API service, for `aws ecs update-service` and `describe-services`."
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "The worker service."
  value       = aws_ecs_service.worker.name
}

output "migrate_task_definition" {
  description = <<-EOT
    The one-off migration task definition, for `aws ecs run-task`. Nothing starts it
    automatically - see the deployment sequence in docs/deployment.md.
  EOT
  value       = aws_ecs_task_definition.migrate.arn
}

output "task_subnet_ids" {
  description = "The subnets `aws ecs run-task` must place the migration task in."
  value       = aws_subnet.public[*].id
}

output "worker_security_group_id" {
  description = "The security group `aws ecs run-task` must give the migration task."
  value       = aws_security_group.worker.id
}

output "vpc_id" {
  description = "For the teardown orphan checks in docs/deployment.md."
  value       = aws_vpc.main.id
}

output "log_groups" {
  description = "The three CloudWatch log groups, which keep charging for storage if left behind."
  value       = [
    aws_cloudwatch_log_group.api.name,
    aws_cloudwatch_log_group.worker.name,
    aws_cloudwatch_log_group.migrate.name,
  ]
}
