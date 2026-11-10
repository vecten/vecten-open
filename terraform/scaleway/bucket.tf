locals {
  bucket_resource = local.artifacts_bucket
  objects         = "${local.artifacts_bucket}/*"

  principal = { for k, id in var.application_ids : k => { SCW = "application_id:${id}" } }

  # The prefixes each host may reach. The MCP host also reads the commit dbt last built, which
  # the platform host writes.
  host_object_grants = {
    for h, cs in local.host_components : h => concat(
      [{ actions = ["s3:GetObject"], prefix = "bundles/${h}/*" }],
      [for c in cs : { actions = ["s3:GetObject", "s3:PutObject"], prefix = "${c}/*" }],
      contains(cs, "mcp") ? [{ actions = ["s3:GetObject"], prefix = "platform/dbt/built_commit" }] : [],
    )
  }

  # Version 2023-04-17 denies whatever no statement allows, so each host key's project-wide
  # object rights narrow to its own prefixes here; the Organization Owner keeps the right to
  # edit the policy whatever it says. The plan application has no statement: pull-request
  # plans run without a refresh and read nothing in this bucket.
  bucket_policy = jsonencode({
    Version = "2023-04-17"
    Id      = "vecten-artifacts"
    Statement = concat(
      [
        {
          Sid       = "DeployFullAccess"
          Effect    = "Allow"
          Principal = local.principal["deploy"]
          Action    = ["s3:*"]
          Resource  = [local.bucket_resource, local.objects]
        },
      ],
      # A bucket policy denies every principal it does not name, the Organization Owner too.
      # The operator's laptop plan refreshes the bucket, `vecten destroy` empties and deletes
      # it, and `vecten issues` reads dbt's results and the logs.
      var.operator_principal == null ? [] : [
        {
          Sid       = "OperatorFullAccess"
          Effect    = "Allow"
          Principal = { SCW = var.operator_principal }
          Action    = ["s3:*"]
          Resource  = [local.bucket_resource, local.objects]
        },
      ],
      flatten([
        for h, grants in local.host_object_grants : concat(
          [
            for i, g in grants : {
              Sid       = "${title(h)}Objects${i}"
              Effect    = "Allow"
              Principal = local.principal[h]
              Action    = g.actions
              Resource  = ["${local.artifacts_bucket}/${g.prefix}"]
            }
          ],
          # A missing key answers 403 instead of 404 unless ListBucket is granted with no
          # prefix condition, and the watcher's first pointer read and Airflow's log writer
          # treat a 403 as a failure. Key names are not secret; reads and writes stay scoped.
          [
            {
              Sid       = "${title(h)}ListsTheBucket"
              Effect    = "Allow"
              Principal = local.principal[h]
              Action    = ["s3:ListBucket"]
              Resource  = [local.bucket_resource]
            },
          ],
        )
      ]),
    )
  })
}

resource "scaleway_object_bucket" "artifacts" {
  name       = local.artifacts_bucket
  project_id = var.project_id
  region     = var.region
  # Only vecten destroy sets destroying, after it has snapshotted the databases.
  force_destroy = var.destroying

  versioning {
    enabled = true
  }

  lifecycle_rule {
    id      = "expire-noncurrent-bundles"
    enabled = true
    prefix  = "bundles/"

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  tags = {
    "vecten:project" = var.project_name
    "vecten:env"     = var.environment
  }

  lifecycle {
    precondition {
      condition     = length(local.artifacts_project_part) > 0 && length(local.artifacts_bucket) <= 63
      error_message = "The environment name leaves no room for the project in a 63-character bucket name; use a shorter environment name."
    }
  }
}

resource "scaleway_object_bucket_policy" "artifacts" {
  bucket     = scaleway_object_bucket.artifacts.name
  project_id = var.project_id
  region     = var.region
  policy     = local.bucket_policy
}
