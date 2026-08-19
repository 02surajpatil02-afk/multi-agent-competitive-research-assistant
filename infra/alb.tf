# WHY THIS FILE EXISTS
#     The one way in. The API is the only process with a port, and this is the only resource
#     with a public address - the worker has neither and must not acquire one.
#
#     **HTTP, not HTTPS, and that is a stated limitation rather than an oversight.** HTTPS needs
#     an ACM certificate, which needs a domain name to validate against, which needs Route 53 or
#     a registrar - three resources and a DNS propagation wait for an environment that lives for
#     an hour. Nothing in the application requires TLS to function: the API key travels in a
#     header, so **an API key sent to this deployment is sent in clear text**, which is why the
#     keys used here must be throwaway keys and never a real key table. docs/deployment.md
#     records exactly what production adds.
#
#     **The health check is the application's own `/health`, unmodified.** It answers 200 when
#     the database, Redis and the checkpoint store all answer, and 503 otherwise - and the
#     matcher below accepts 200 only. A check that accepted 503 would report a deployment
#     healthy in which no job can run, which is the one thing `/health` exists to prevent. It
#     reaches no LLM provider and no external service, so a provider outage cannot deregister
#     the API.
#
# WHO USES IT
#     ecs.tf, whose API service registers into this target group.

resource "aws_lb" "api" {
  name               = "${local.name}-api"
  load_balancer_type = "application"
  internal           = false

  subnets         = aws_subnet.public[*].id
  security_groups = [aws_security_group.alb.id]

  # Off, so `terraform destroy` is one command. An ALB is one of the few resources here that
  # charges by the hour whether or not any task is running behind it.
  enable_deletion_protection = false

  # No access logs. They need a second bucket and a bucket policy, and this deployment's
  # request log is the CloudWatch log group the API already writes to.

  tags = { Name = "${local.name}-api" }
}

resource "aws_lb_target_group" "api" {
  name     = "${local.name}-api"
  port     = 8000
  protocol = "HTTP"
  vpc_id   = aws_vpc.main.id

  # `ip` rather than `instance`, because a Fargate task is an ENI and has no instance to
  # register.
  target_type = "ip"

  # 30 seconds rather than the 300-second default. Nothing here holds a long-running request -
  # the API's work is measured in milliseconds and the twenty-minute job runs in the worker -
  # so a slow deregistration only makes `terraform destroy` wait.
  deregistration_delay = 30

  health_check {
    path     = "/health"
    protocol = "HTTP"
    matcher  = "200"

    # 15s x 2 puts a newly started task in service about half a minute after `/health` first
    # answers 200; 3 consecutive failures takes it out. The task is not killed for failing
    # this - see `health_check_grace_period_seconds` on the API service in ecs.tf.
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${local.name}-api" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
