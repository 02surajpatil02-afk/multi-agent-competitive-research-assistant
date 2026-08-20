# ADR 0020 — The API verifies Cognito tokens itself, and no credential reaches a task definition

**Status:** Accepted, **built** (Phase 5 block B); **never applied**
**Date:** 2026-08-20
**Supersedes:** nothing. **Amends:** [ADR 0019](0019-no-nat-gateway-in-the-temporary-aws-deployment.md)'s
deployment, which it hardens without changing its shape.

---

## Context

[Block A](0019-no-nat-gateway-in-the-temporary-aws-deployment.md) put the two processes on
Fargate and said out loud what it had left out. Three of those are one problem wearing three
faces:

**Every credential was a plaintext task-definition environment variable.** The database password,
the LLM key, the search key and the API's whole key table were readable by anyone who could call
`ecs:DescribeTaskDefinition` — and a task definition *revision* is retained after the deployment
that used it is destroyed, so the exposure outlives the environment.

**The database password passed through Terraform**, which means it was written into
`terraform.tfstate`: a plain JSON file on a laptop, in a repository directory, holding a working
credential.

**Authentication was still the Phase 2 API-key table**, and the ALB speaks plain HTTP. A static
shared secret with no expiry, sent in clear text, protecting the endpoint that decides whether a
report is exported.

The constraint has not changed: this deployment is created, verified, screenshotted and destroyed
inside about an hour. Anything that needs a domain, a rotation schedule, or an approval workflow
is not a thing this environment can carry.

---

## Decision

### 1. RDS generates its own master password, and the container composes the URL

`manage_master_user_password = true`. RDS creates the password, stores it in a Secrets Manager
secret it owns, and never shows it to Terraform. `var.db_password` is deleted — there is no value
for an operator to invent, no value to export, and **no database password in `terraform.tfstate`
at all**.

The cost is that the secret holds `{"username", "password"}` and nothing else — no host, no port,
no database name — so nothing upstream can build a connection string. The ECS task definition
therefore injects `DB_USER` and `DB_PASSWORD` from that secret and `DB_HOST`, `DB_PORT` and
`DB_NAME` as plain environment, and `config.resolve_database_url` assembles the URL inside the
container.

**`DATABASE_URL` always wins when it is set**, so every local command, the Compose stack, the
offline suite and the three `scripts/` entrypoints are untouched. The parts are read only when it
is absent, and only when all four required ones are present — a half-configured deployment is
`None`, which is the "not configured" every caller already handles, rather than a URL with the
word `None` in it.

`database/migrations/env.py` shares the same function. A migration still does not build a whole
`Config`, so it still needs no LLM key to run.

**What remains in state, stated rather than implied.** The LLM key, the search key and — in
`api_key` mode — the key table are written by Terraform into Secrets Manager, so their values are
in `terraform.tfstate`. `terraform.tfvars` and `*.tfstate` are gitignored, the teardown deletes
the state file, and `docs/deployment.md` says the file is a credential store. The production
alternative is to create the secret with no version and populate it out of band with
`aws secretsmanager put-secret-value`; the `ignore_changes = [secret_string]` on each version
already permits exactly that. It is not the default because an empty secret makes `apply` succeed
and the worker crash-loop, while a required variable makes `apply` refuse — the better failure.

### 2. One credential is live at a time, chosen by `AUTH_MODE`

`AUTH_MODE=api_key` is the Phase 2 hashed key table and stays the **default**, so nothing local
changes. `AUTH_MODE=cognito` is the AWS deployment's mode, and Terraform defaults `auth_mode` to
`cognito`.

**Never both.** An API that accepted a Cognito token *or* a static key would be exactly as strong
as the weaker of the two, and the weaker one is a shared secret with no expiry — which is the
thing this block exists to retire. Under Cognito the deployment creates no auth-keys secret, the
API's execution role is granted none, and the process holds no shared secret at all.

The abstraction is one object with one method — `Authenticator.identity(header) -> Identity | None`
— because `routes/auth.py` predicted this in Phase 2: *"everything downstream already reads a
`user_id` and a role rather than a key, so the change is confined to `identity_from()`"*. It was.
`audit_events.actor`, `jobs.user_id`, the ownership check and the two roles are untouched.

### 3. The access token, not the id token, and six checks on it

The **access token** is accepted. It is the one that says *this bearer may call this API*, and it
is the one carrying `cognito:groups` and `client_id`; the id token describes a user to the
application that signed them in. Accepting either would put two claim shapes on one authorization
path.

| Check | The failure it closes |
|---|---|
| `RS256`, named in the verifier and never read from the token | `alg: none`, and algorithm confusion — the public key used as an HMAC secret |
| The signature, against the pool's published JWKS | A token anyone can write |
| `iss` equals this pool's issuer | A token from a Cognito pool the attacker created in their own account |
| `exp` and `iat`, plus every required claim present | A token that was valid last year |
| `token_use == "access"` | An id token presented where an access token is required |
| `client_id` equals this app client | A token minted for a different application in the same pool |

A role comes from the pool's groups — `reviewer` and `submitter`, the two `routes/auth.py` has had
since Phase 2 — because a group is the one claim on a Cognito token that an administrator sets and
a user cannot. `reviewer` wins when both are present, since it is the superset. **A valid token
carrying neither group produces no identity**, and the caller gets the same `401` an unknown key
gets, for the reason the three key failures already share one answer.

**The key set is cached with two rules, and both are security properties.** An unknown key id
triggers a refetch, because that is what a rotation looks like from here — waiting for the
hour-long TTL would reject every caller until it expired. And no refetch happens more than once
every five minutes, because "unknown key id" is also what a *forged* token looks like, and an
unauthenticated caller must not be able to turn this process into a load generator against
Cognito. A fetch that fails leaves the cache alone and authenticates nobody: **fail closed**, since
trusting a token nobody could check is the only worse answer than refusing a caller who should
have been let in.

### 4. The application validates the token; the load balancer does not

ALB's `authenticate-cognito` action was evaluated and rejected. It is a browser flow: it redirects
an unauthenticated request to a hosted login page and maintains a session cookie. That is right for
a web application and wrong for an API whose callers are `curl`, a client library and the
verification steps in `docs/deployment.md` — those send `Authorization: Bearer …`, which is
**exactly the header shape the Phase 2 key already used**.

So application-level validation kept the request contract byte-for-byte identical, needed no hosted
UI, no user-pool domain and no callback URL, and left the ALB doing one job. Both were not
implemented; there is one authorization decision and one place it is made.

### 5. HTTPS if a certificate exists, HTTP if not, and the default is not

A public ACM certificate cannot be issued without a domain name to validate against. This
repository owns no domain, and creating a Route 53 hosted zone to invent one would charge per month
and outlive an environment that lives an hour.

So `var.certificate_arn` accepts an **existing, already-validated** certificate. With one, the ALB
gains an HTTPS listener on 443 with a TLS 1.2 floor, and port 80 stops forwarding and starts
redirecting — a listener that forwarded on both would leave the plain path open beside the
encrypted one. With none, the deployment stays HTTP and the limitation is written down rather than
hidden.

**What that limitation actually is.** The bearer credential travels in clear text — true of an API
key and equally true of a Cognito access token. Two things shrink the blast radius without
removing it: the credentials used here must be throwaway, and a Cognito access token expires in an
hour, which is the life of the deployment. **The password-for-token exchange is always encrypted**,
because it goes to Cognito's own HTTPS endpoint rather than to this load balancer; only the token's
onward journey is exposed.

### 6. Three execution roles, and no secret on any task role

An ECS *execution* role starts a task — pulls the image, creates the log stream, and now fetches
the secrets the task definition names. A *task* role is what the running application's boto3 picks
up. They are different identities.

**The secrets go on the execution roles.** The application never calls Secrets Manager — it reads
environment variables, exactly as it does locally — so nothing inside either container has, or
needs, permission to read one. A compromised worker process cannot fetch the auth table because it
cannot fetch anything.

**Three execution roles rather than one**, because a single shared role would have to be granted
every secret all three task definitions use — handing the API's task-start identity the LLM key.
That is precisely the boundary [ADR 0012](0012-the-api-stops-holding-a-compiled-graph.md) exists to
hold, and one role would quietly undo it in IAM while the task definition still looked clean.

Trust policies gained AWS's documented confused-deputy conditions: `aws:SourceAccount` equal to
this account, and `aws:SourceArn` matching this region's ECS. Both are practical here and neither
needs anything set up outside this configuration.

**No customer-managed KMS key.** RDS storage, the S3 bucket and every secret are encrypted with
AWS-managed keys, which cost nothing and need no grants. A customer-managed key is a per-month
charge, two more grants and a teardown step, for an environment measured in hours. A permissions
boundary and CloudTrail data events are documented as production recommendations for the same
reason: each needs setup this configuration cannot make.

---

## Consequences

**What got better.** No credential is readable from a task definition. The database password
exists only inside AWS and has never been in a file. The API, in its deployed mode, holds no shared
secret at all and every caller's token expires within the hour. Three IAM identities can each fetch
exactly the secrets their own task names, and none of the running processes can fetch any.

**What did not change, and must not have.** The `Authorization: Bearer …` contract, the two roles,
the `Identity` every downstream reader consumes, the six routes, the health contract, the queue
semantics, the visibility lease, the per-job execution fence, the checkpointer ownership and the
export durability. Block B is a change to how a caller proves who they are and how a process
receives a secret — nothing about how the system works.

**What is still true and uncomfortable.** With no certificate the deployment is plain HTTP, so a
token is observable in transit. Terraform state still holds two provider credentials. Nothing
rotates automatically, and for a one-hour environment nothing should: rotation is a schedule, and a
schedule needs something that runs longer than this does. MFA is off. Each is written into
`docs/deployment.md` §9 beside what production does instead.

**What this does not do.** No alarm watches a failed authentication, no dashboard shows token
rejections, and no sweep finds a stale job — those are Block C's, and adding one here would have
been the same mistake Block A avoided by not building Block B early.
