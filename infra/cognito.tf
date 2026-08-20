# WHY THIS FILE EXISTS
#     Who the reviewers and submitters are, when the deployment is not using a static key table.
#     One user pool, one app client, two groups, and nothing else
#     (docs/adr/0020-*.md decisions 2, 3 and 4).
#
#     **The API validates the token itself; the load balancer does not.** ALB's
#     `authenticate-cognito` action was considered and rejected: it is a browser flow that
#     redirects to a hosted login page and sets a session cookie, which is right for a web
#     application and wrong for an API whose callers are `curl` and a client library. Those
#     callers already send `Authorization: Bearer ...`, which is the shape both the Phase 2 key
#     and a Cognito access token take - so application-level validation kept the API contract
#     identical and needed no hosted UI, no domain and no callback URL. ADR 0020 decision 4.
#
#     **The two groups are the two roles.** `routes/auth.py` has had exactly two since Phase 2 -
#     a submitter submits and reads its own jobs, a reviewer may also decide at the gate - and a
#     pool group is the one claim on a Cognito token that an administrator sets and a user
#     cannot. `role_from_groups` reads them by these names.
#
#     **Deliberately absent**, each because nothing here needs it: a hosted UI and its domain,
#     any social or SAML identity provider, Lambda triggers, custom auth challenges, MFA, and
#     self-service sign-up. Every one is a resource to tear down and a claim to explain.
#
#     **Cost: nothing.** A user pool with a handful of administrator-created users is far inside
#     the free tier, which is why this is created whenever the mode asks for it rather than
#     being another thing to remember to enable.
#
# WHO USES IT
#     ecs.tf, which passes the pool and client ids to the API as plain environment variables -
#     neither is a secret - and outputs.tf, which is where the operator reads them for the
#     token procedure in docs/deployment.md.

resource "aws_cognito_user_pool" "main" {
  count = local.cognito_enabled ? 1 : 0

  name = local.name

  # **No self-service sign-up.** An account that may approve a research report is created by an
  # administrator, which for this deployment means one `admin-create-user` in docs/deployment.md.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  # OFF, and said out loud rather than left to the default. A second factor is the right answer
  # for an account that can approve an export, and it is also a phone number or an authenticator
  # enrolment in an environment that lives an hour. docs/deployment.md carries it as a
  # production difference.
  mfa_configuration = "OFF"

  # So `terraform destroy` is one command. AWS refuses to delete a protected pool.
  deletion_protection = "INACTIVE"

  tags = { Name = local.name }
}

resource "aws_cognito_user_pool_client" "api" {
  count = local.cognito_enabled ? 1 : 0

  name         = "${local.name}-api"
  user_pool_id = aws_cognito_user_pool.main[0].id

  # **A public client with no secret**, because the caller is a terminal or a script and a
  # client secret it has to hold would be exactly the shared secret Cognito is replacing. The
  # security of this flow is the user's password and the token's signature, not a client secret.
  generate_secret = false

  # The two flows the documented procedure uses and no others. `ALLOW_USER_PASSWORD_AUTH` is
  # what lets an operator exchange a password for a token with no AWS credentials at all - over
  # HTTPS to Cognito, which is a different endpoint from this deployment's plain-HTTP ALB.
  # `ALLOW_ADMIN_USER_PASSWORD_AUTH` is the same exchange made with AWS credentials instead.
  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # One hour is Cognito's minimum for an access token and the right number here: a token that
  # leaks off a plain-HTTP ALB stops working within the hour this deployment exists.
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 1

  # A wrong password and an unknown user answer the same way, so the pool cannot be used to
  # find out which addresses have accounts. The same argument `routes/auth.py` makes about
  # returning one 401 for three different failures.
  prevent_user_existence_errors = "ENABLED"

  # **No callback or logout URL, and no `allowed_oauth_flows`.** Those configure the hosted
  # login page, which this deployment does not use - the API takes a bearer token and serves no
  # browser.
}

resource "aws_cognito_user_group" "reviewer" {
  count = local.cognito_enabled ? 1 : 0

  name         = "reviewer"
  user_pool_id = aws_cognito_user_pool.main[0].id
  description  = "May decide at the human gate, and may read any job in order to decide."
}

resource "aws_cognito_user_group" "submitter" {
  count = local.cognito_enabled ? 1 : 0

  name         = "submitter"
  user_pool_id = aws_cognito_user_pool.main[0].id
  description  = "May submit research questions and read its own jobs."
}
