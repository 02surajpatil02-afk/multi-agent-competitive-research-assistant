# ADR 0019 — No NAT gateway in the temporary AWS deployment

- **Status:** **Accepted, built 2026-08-20** (Phase 5 block A). Nothing is deployed: the
  configuration exists in `infra/` and has never been applied
- **Date:** 2026-08-20
- **Affects:** `infra/network.tf` · `infra/ecs.tf` · `docs/deployment.md` ·
  `tests/test_infrastructure_terraform.py`
- **Does not affect:** any Phase 3 runtime semantic. The queue attributes, the visibility lease,
  the per-job execution fence, the migration and checkpointer ownership, the health contract and
  the 120-second stop timeout are unchanged

---

## Context

Phase 5 block A puts the existing two processes onto AWS for a **temporary portfolio
deployment**: deploy, verify end to end, collect evidence, destroy. Expected life is about an
hour, and the AWS credits behind it are limited.

Both processes need outbound network access, and they need different amounts of it:

| Process | Must reach |
|---|---|
| `uvicorn app:app` | RDS, ElastiCache, SQS, S3, ECR, CloudWatch Logs |
| `python -m worker` | all of the above, **plus** an OpenAI-compatible LLM endpoint, Tavily, and arbitrary third-party web pages that `tools/fetch.py` follows |

The worker's last row is the constraint that does not go away. Research means fetching pages
nobody enumerated in advance, so **no design of this system has a worker without open outbound
internet access**.

The conventional ECS layout is application subnets with no route to an internet gateway, plus a
NAT gateway per AZ for egress. That is the right answer for a long-running deployment. For this
one it has two costs:

1. **Money.** A NAT gateway charges per hour plus per GB processed, and one per AZ doubles it.
   Against a deployment whose other resources are `db.t4g.micro`, `cache.t4g.micro`, one ALB and
   0.75 vCPU of Fargate, NAT is comparable to everything else combined — for an hour of life.
2. **Teardown risk.** A NAT gateway and its Elastic IP are exactly the resources that survive a
   half-finished destroy and keep charging silently.

The alternative is to give the Fargate tasks public IPs in public subnets. That trades a network
boundary for a security-group boundary, and the question is whether the remaining boundary is
strong enough to state honestly.

---

## Decision

**1. No NAT gateway, no Elastic IP, and no VPC endpoints in the temporary deployment.** Both ECS
services run in public subnets with `assign_public_ip = true` and egress through the internet
gateway.

**2. The security group is the control, and it is written to be read.** A public subnet means
"has a route to the internet gateway", not "reachable from the internet". Concretely:

- the API accepts port 8000 **from the ALB's security group and from nothing else**;
- the worker has **no ingress rule at all** — it serves nothing and makes no authorization
  decision (ARCHITECTURE.md §19), so there is nothing to open;
- the API's egress is 443, DNS inside the VPC, and the two data stores. It has no port-80 rule,
  because it fetches no page;
- the worker's egress adds port 80, because `tools/fetch.py` accepts `http://` URLs and a real
  source can redirect through a plain-HTTP hop.

**3. The data tier stays genuinely private.** RDS and ElastiCache live in subnets whose route
table has **no route at all** beyond the implicit local one, are `publicly_accessible = false`,
and accept traffic only by security-group reference — no CIDR appears in any of their ingress
rules.

**4. The network spans two availability zones and the resources inside it do not.** An ALB
requires subnets in at least two AZs, and so do the RDS and ElastiCache subnet groups. The
database is `multi_az = false`, the cache is one node, and each service runs one task. **The
second AZ is an API requirement, not a redundancy claim**, and the documentation says so rather
than letting a reader infer high availability from a subnet count.

**5. The trade is asserted, not just written down.**
`tests/test_infrastructure_terraform.py` fails if a NAT gateway or an Elastic IP appears, if any
ingress rule other than the ALB's names a CIDR, if the worker gains an ingress rule, if the API
gains a second one, or if a security group grows an inline rule block that these checks would not
see.

**6. The long-running production version is different, and is written down rather than built.**
`docs/deployment.md` §9: private application subnets, controlled egress through a NAT gateway or
an egress-filtering proxy, a gateway endpoint for S3 and interface endpoints for ECR, SQS and
Logs.

---

## Consequences

**What this buys.** The largest per-hour line item in the textbook design disappears, along with
the two resource types most likely to be left behind by an incomplete teardown. The deployment
becomes cheap enough that the cost of forgetting it for a day is an annoyance rather than a
problem.

**What it costs, stated plainly.** The tasks have public IP addresses. A misconfigured security
group would expose a task directly to the internet, where in the private-subnet design the subnet
boundary would still stand. That is one defence rather than two, which is why the security-group
relationships are the most heavily asserted part of the configuration.

**What it does not cost.** Nothing about the application changed to make this work. No timeout,
no retry, no queue attribute, no ownership rule, and no code path is different because egress
goes through an internet gateway instead of a NAT gateway.

**What would reverse it.** Any of: the deployment stops being temporary; a compliance requirement
names private subnets; the worker's egress needs filtering by destination rather than by port. In
each case the change is additive — private subnets already exist for the data tier, and moving
the two services into them plus a NAT gateway is a route table and a network configuration, not a
redesign.
