locals {
  # Credentials the setup host generates for the databases the instances hold. It reads them
  # back to re-create a role after an instance replacement; it holds the master credential
  # anyway, so reading these exact names, and no wildcard, grants nothing beyond it.
  generated_secrets = concat(
    local.has_platform ? ["platform/AIRFLOW_DB_PASSWORD"] : [],
    local.managed_warehouse ? ["platform/LOADER_PASSWORD"] : [],
    local.has_mcp ? ["mcp/MCP_STATE_DB_PASSWORD"] : [],
    local.has_mcp && local.managed_warehouse ? ["mcp/MCP_PROVISIONER_PASSWORD", "mcp/MCP_IMPERSONATOR_PASSWORD"] : [],
  )
  airflow_app_secrets = [
    "platform/AIRFLOW_FERNET_KEY",
    "platform/AIRFLOW_API_SECRET_KEY",
    "platform/AIRFLOW_JWT_SECRET",
    "platform/AIRFLOW_ADMIN_PASSWORD",
    "platform/AIRFLOW_BASIC_AUTH_PASSWORD",
  ]
  # Secrets Manager appends six random characters to a secret's ARN, so an existing secret
  # matches NAME-??????; before the secret exists there is no suffix to match, so the bare
  # name is granted too. Both forms name exactly one secret.
  generated_secret_arns = flatten([
    for n in local.generated_secrets : ["${local.secret_arn_base}${n}", "${local.secret_arn_base}${n}-??????"]
  ])
  airflow_app_secret_arns = flatten([
    for n in local.airflow_app_secrets : ["${local.secret_arn_base}${n}", "${local.secret_arn_base}${n}-??????"]
  ])

  mcp_rotation_arns = flatten([
    for n in ["MCP_SIGNING_KEY", "MCP_IMPERSONATOR_PASSWORD"] : [
      "${local.secret_arn_base}mcp/${n}", "${local.secret_arn_base}mcp/${n}-??????"
    ]
  ])

  # Built with jsonencode rather than a policy document data source, so the tests can decode
  # them at plan time.
  scope_policies = {
    for h, cs in local.host_components : h => jsonencode({
      Version = "2012-10-17"
      Statement = [
        for s in [
          {
            include = true
            statement = {
              Sid      = "ReadScopeSecrets"
              Effect   = "Allow"
              Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
              Resource = [for c in cs : "${local.secret_arn_base}${c}/*"]
            }
          },
          {
            include = true
            statement = {
              Sid      = "ReadBundles"
              Effect   = "Allow"
              Action   = ["s3:GetObject"]
              Resource = ["${local.artifacts_bucket_arn}/bundles/${h}/*"]
            }
          },
          {
            include = true
            statement = {
              Sid      = "ComponentObjects"
              Effect   = "Allow"
              Action   = ["s3:GetObject", "s3:PutObject"]
              Resource = [for c in cs : "${local.artifacts_bucket_arn}/${c}/*"]
            }
          },
          {
            include = true
            # S3 answers 403 instead of 404 for a missing key unless ListBucket is granted with
            # no prefix condition, and Airflow's log writer and the artifact sink treat a 403 as
            # a failure. Key names are not secret; reads and writes stay scoped by prefix.
            statement = {
              Sid      = "ListArtifactsBucket"
              Effect   = "Allow"
              Action   = ["s3:ListBucket"]
              Resource = [local.artifacts_bucket_arn]
            }
          },
          {
            include = h == local.setup_host
            statement = {
              Sid    = "WriteGeneratedSecrets"
              Effect = "Allow"
              Action = [
                "secretsmanager:CreateSecret",
                "secretsmanager:PutSecretValue",
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
              ]
              Resource = local.generated_secret_arns
            }
          },
          {
            include = contains(cs, "platform")
            # Create only: DAG code runs on this host and must not rewrite Airflow's own keys.
            statement = {
              Sid      = "WriteAirflowSecrets"
              Effect   = "Allow"
              Action   = ["secretsmanager:CreateSecret"]
              Resource = local.airflow_app_secret_arns
            }
          },
          {
            include = contains(cs, "mcp")
            statement = {
              Sid      = "RotateMcpIdentitySecrets"
              Effect   = "Allow"
              Action   = ["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue"]
              Resource = local.mcp_rotation_arns
            }
          },
          {
            include = contains(cs, "mcp")
            statement = {
              Sid    = "ReadBuiltCommit"
              Effect = "Allow"
              Action = ["s3:GetObject"]
              # Every record vecten_shared.osi.revision reads: the commit dbt last built, and
              # when its last build finished, on which a Postgres MCP reconciles.
              Resource = [
                "${local.artifacts_bucket_arn}/platform/dbt/built_commit",
                "${local.artifacts_bucket_arn}/platform/dbt/build_finished_at",
              ]
            }
          },
        ] : s.statement if s.include
      ]
    })
  }
}

# Separate from the scope policy because the master secret ARNs exist only after the
# instances do; the setup host is the one VM that reads them.
resource "aws_iam_role_policy" "master_secrets" {
  name = "master-secrets"
  role = module.host[local.setup_host].role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadMasterSecrets"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = [for m in module.postgres : m.master_secret_arn]
    }]
  })
}
