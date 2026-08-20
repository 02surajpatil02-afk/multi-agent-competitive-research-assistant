# WHY THIS FILE EXISTS
#     The one way in. The API is the only process with a port, and this is the only resource
#     with a public address - the worker has neither and must not acquire one.
#
#     **HTTPS if you have a certificate, HTTP if you do not, and the default is that you do
#     not** (docs/adr/0020-*.md decision 5). An ACM certificate cannot exist without a domain
#     name to validate against, and this repository owns no domain: creating a Route 53 hosted
#     zone would invent one, charge per month, and still need a registrar. So `certificate_arn`
#     is a variable an operator sets when they already have a validated certificate, and the
#     listener below is written both ways.
#
#     **With no certificate, the bearer credential travels in clear text.** That is true of a
#     Phase 2 API key and equally true of a Cognito access token: TLS is what would protect
#     either in transit, and there is none. Two things reduce the blast radius rather than
#     removing it - the keys used here must be throwaway keys, and a Cognito access token
#     expires in an hour, which is the life of the whole deployment. **The password-for-token
#     exchange itself is always over HTTPS**, because it goes to Cognito's own endpoint and not
#     to this load balancer; only the token's onward journey is exposed.
#
#     **With a certificate, port 80 stops serving and starts redirecting.** A listener that
#     forwarded on both would leave the plain-HTTP path open beside the encrypted one, which is
#     the same weakness with an extra step.
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

  # Exactly one of these two exists at a time. With a certificate configured, port 80 does
  # nothing but send the caller to port 443; without one, it is the only way in.
  dynamic "default_action" {
    for_each = local.https_enabled ? [1] : []

    content {
      type = "redirect"

      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = local.https_enabled ? [] : [1]

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }
}

# **The certificate is supplied, never created.** `aws_acm_certificate` would need a domain this
# repository does not own, and DNS validation would need a hosted zone that charges per month
# and outlives the deployment. An operator who already has a validated certificate passes its
# ARN; everyone else gets the HTTP listener above and the limitation written down.
resource "aws_lb_listener" "https" {
  count = local.https_enabled ? 1 : 0

  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn

  # TLS 1.2 as the floor, with 1.3 preferred. The older `ELBSecurityPolicy-2016-08` default
  # still negotiates TLS 1.0.
  ssl_policy = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
