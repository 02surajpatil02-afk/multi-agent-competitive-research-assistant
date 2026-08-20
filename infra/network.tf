# WHY THIS FILE EXISTS
#     The smallest network that can run this application, and the one file where the
#     cost-driven trade is made explicit.
#
#     **There is no NAT gateway, and that is a decision rather than an omission**
#     (docs/adr/0019-*.md). The worker must reach an LLM endpoint, Tavily, and arbitrary
#     third-party web pages, so it needs outbound internet whatever else is true. The textbook
#     answer - private subnets plus a NAT gateway per AZ - costs roughly as much per hour as
#     every other resource here put together, for an environment that lives for an hour. So the
#     two Fargate services run in public subnets with public IPs and reach the internet through
#     the internet gateway directly.
#
#     **What replaces the NAT gateway as a control is the security group, not the subnet.** A
#     public subnet means "has a route to the internet gateway"; it does not mean "reachable
#     from the internet". The API accepts port 8000 from the ALB's security group and from
#     nothing else, and the worker accepts no inbound traffic at all - it has no ingress rule,
#     which is the strongest statement available. Neither task is addressable from outside
#     regardless of the public IP attached to it.
#
#     **The data tier is genuinely private.** PostgreSQL and Redis live in subnets whose route
#     table has no route to the internet gateway, so there is no path in or out even before the
#     security groups refuse. They accept traffic only from the two application security groups.
#
#     **Two availability zones, one of everything.** The ALB requires subnets in at least two
#     AZs, and RDS and ElastiCache subnet groups have the same requirement. That is why the
#     network spans two AZs while the database is Single-AZ and every service runs one task -
#     the second AZ is an API requirement, not a redundancy claim.
#
# WHO USES IT
#     alb.tf, ecs.tf and data_stores.tf, which place their resources in these subnets and
#     reference these security groups.

resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  # Both are required by anything that resolves a name: RDS and ElastiCache endpoints are DNS
  # names, and the tasks reach ECR, SQS and S3 by name as well.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.name }
}

# --- Public subnets: the ALB, and the two Fargate services --------------------------------

resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)

  # Deliberately false. A task's public IP is granted by the ECS service's network
  # configuration, one service at a time, rather than by every ENI that happens to land here.
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- Private subnets: PostgreSQL and Redis, with no route off the VPC ----------------------

resource "aws_subnet" "private" {
  count = length(local.azs)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 100)

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

# One route table with **no routes in it at all** beyond the implicit local one. That is what
# makes these subnets private: there is nothing to add a NAT gateway to later without an edit,
# and nothing here can reach or be reached from the internet.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = length(aws_subnet.private)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- Subnet groups. Two AZs because the services require two ------------------------------

resource "aws_db_subnet_group" "postgres" {
  name       = "${local.name}-postgres"
  subnet_ids = aws_subnet.private[*].id

  description = "Private subnets in two AZs. RDS requires two even for a Single-AZ instance."
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id

  description = "Private subnets. The cache is a single node in one of them."
}

# --- Security groups ------------------------------------------------------------------------
#
# Four groups and one direction of trust: the internet reaches the ALB, the ALB reaches the API,
# and the API and worker reach the two data stores. Nothing reaches the worker.
#
# Rules are separate resources rather than inline blocks so each one is a named object a plan
# can add or remove on its own, and so the relationship between two groups is a single
# attribute - `referenced_security_group_id` - rather than a CIDR that has to be read twice.

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public entry point. The only group with an ingress rule from the internet."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-alb" }
}

resource "aws_security_group" "api" {
  name        = "${local.name}-api"
  description = "The API tasks. Reachable from the load balancer and from nothing else."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-api" }
}

resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "The worker tasks, and the one-off migration task. No ingress rule exists."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-worker" }
}

resource "aws_security_group" "postgres" {
  name        = "${local.name}-postgres"
  description = "RDS. Ingress from the API and worker groups only; no egress rule at all."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-postgres" }
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "ElastiCache. Ingress from the API and worker groups only; no egress rule."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-redis" }
}

# The internet to the ALB. `var.allowed_ingress_cidrs` is the one place a public address range
# is written down.
resource "aws_vpc_security_group_ingress_rule" "alb_from_internet" {
  count = length(var.allowed_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from the configured client range"
  cidr_ipv4         = var.allowed_ingress_cidrs[count.index]
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

# The same range on 443, and only when there is a certificate to terminate with. An open 443
# with no listener behind it would be a rule that describes nothing.
resource "aws_vpc_security_group_ingress_rule" "alb_from_internet_tls" {
  count = local.https_enabled ? length(var.allowed_ingress_cidrs) : 0

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the configured client range"
  cidr_ipv4         = var.allowed_ingress_cidrs[count.index]
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Health checks and forwarded requests to the API container port"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

# **The API's only ingress rule.** A public IP on the task changes nothing while this is the
# only way in.
resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.api.id
  description                  = "The container port, from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

# The API's egress: HTTPS for SQS, S3, ECR and CloudWatch Logs, DNS inside the VPC, and the two
# data stores. It has no reason to make a plain HTTP request and no rule that would let it.
resource "aws_vpc_security_group_egress_rule" "api_https" {
  security_group_id = aws_security_group.api.id
  description       = "AWS service APIs, the image pull, and log delivery"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# Without this the tasks resolve nothing and every AWS call fails on DNS rather than on a
# permission - a failure that costs an hour to read. The VPC resolver lives inside the VPC
# range, so this is not internet egress.
resource "aws_vpc_security_group_egress_rule" "api_dns_udp" {
  security_group_id = aws_security_group.api.id
  description       = "The VPC DNS resolver"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "udp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "api_dns_tcp" {
  security_group_id = aws_security_group.api.id
  description       = "The VPC DNS resolver, for answers too large for UDP"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "api_to_postgres" {
  security_group_id            = aws_security_group.api.id
  description                  = "Rows, and the checkpoint reads behind /health and the gate view"
  referenced_security_group_id = aws_security_group.postgres.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "api_to_redis" {
  security_group_id            = aws_security_group.api.id
  description                  = "One PING, for checks.redis on /health. The API caches nothing"
  referenced_security_group_id = aws_security_group.redis.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

# **The worker has no ingress rule.** It serves nothing and makes no authorization decision
# (ARCHITECTURE.md section 19), so there is nothing to open.
resource "aws_vpc_security_group_egress_rule" "worker_https" {
  security_group_id = aws_security_group.worker.id
  description       = "The LLM endpoint, Tavily, AWS service APIs, and https:// research pages"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# `tools/fetch.py` accepts http:// as well as https:// URLs, and a real source that redirects
# through a plain-HTTP hop would otherwise be unreachable. This is the one rule that exists
# because of what the research tool does rather than because of AWS.
resource "aws_vpc_security_group_egress_rule" "worker_http" {
  security_group_id = aws_security_group.worker.id
  description       = "http:// research pages and redirect hops (tools/fetch.py)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "worker_dns_udp" {
  security_group_id = aws_security_group.worker.id
  description       = "The VPC DNS resolver"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "udp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "worker_dns_tcp" {
  security_group_id = aws_security_group.worker.id
  description       = "The VPC DNS resolver, for answers too large for UDP"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 53
  to_port           = 53
}

resource "aws_vpc_security_group_egress_rule" "worker_to_postgres" {
  security_group_id            = aws_security_group.worker.id
  description                  = "Rows, checkpoints, the job lock, and `alembic upgrade head`"
  referenced_security_group_id = aws_security_group.postgres.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "worker_to_redis" {
  security_group_id            = aws_security_group.worker.id
  description                  = "The caches, the per-job URL set, and the shared rate limiter"
  referenced_security_group_id = aws_security_group.redis.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

# The data tier accepts the two application groups by reference. **No CIDR appears in any of
# these four rules**, which is what makes "PostgreSQL is not reachable from the internet" a
# property of the configuration rather than of the subnet it happens to sit in.
resource "aws_vpc_security_group_ingress_rule" "postgres_from_api" {
  security_group_id            = aws_security_group.postgres.id
  description                  = "The API process"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# The one-off migration task runs in the worker's security group: it needs exactly the egress
# the worker has, accepts no ingress either, and giving it a fifth group would add a resource
# that differs from this one in nothing.
resource "aws_vpc_security_group_ingress_rule" "postgres_from_worker" {
  security_group_id            = aws_security_group.postgres.id
  description                  = "The worker process, and the migration task"
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_api" {
  security_group_id            = aws_security_group.redis.id
  description                  = "The /health probe"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_worker" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Caches, URL dedupe, and the shared rate limiter"
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}
