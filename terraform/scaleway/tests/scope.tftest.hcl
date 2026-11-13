mock_provider "scaleway" {}

variables {
  environment       = "prod"
  project_name      = "acme"
  domain            = "data.acme.com"
  dns_zone          = "acme.com"
  region            = "fr-par"
  github_repository = "acme/data"
  components        = ["platform", "mcp"]
  profile           = "standard"
  size              = "small"
  warehouse         = "postgres"
  project_id        = "1a2b3c4d-2222-4333-8444-555555555555"
  application_ids = {
    deploy   = "aaaaaaaa-0000-4000-8000-000000000001"
    plan     = "aaaaaaaa-0000-4000-8000-000000000002"
    platform = "aaaaaaaa-0000-4000-8000-000000000003"
    mcp      = "aaaaaaaa-0000-4000-8000-000000000004"
    main     = "aaaaaaaa-0000-4000-8000-000000000005"
  }
}

run "policy_uses_the_current_version_and_application_principals" {
  command = plan

  assert {
    condition     = jsondecode(scaleway_object_bucket_policy.artifacts.policy).Version == "2023-04-17"
    error_message = "the policy uses Version 2023-04-17, under which anything not allowed is denied"
  }
  assert {
    condition     = alltrue([for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s.Effect == "Allow" && startswith(s.Principal.SCW, "application_id:")])
    error_message = "without an operator principal, every statement allows one IAM application"
  }
  assert {
    condition     = scaleway_object_bucket_policy.artifacts.bucket == scaleway_object_bucket.artifacts.name
    error_message = "the policy is on the artifacts bucket"
  }
}

run "deploy_application_has_full_access" {
  command = plan

  assert {
    condition = [
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : [s.Action, s.Resource]
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000001"
      ] == [[
        ["s3:*"],
        ["vecten-acme-prod-artifacts-1a2b3c4d", "vecten-acme-prod-artifacts-1a2b3c4d/*"],
    ]]
    error_message = "the deploy application may do anything on the bucket and its objects"
  }
}

run "plan_application_reaches_nothing_in_the_bucket" {
  command = plan

  # Pull-request plans run without a refresh, so the plan key reads nothing here.
  assert {
    condition = [
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s.Sid
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000002"
    ] == []
    error_message = "no statement names the plan application"
  }
}

run "every_action_is_one_scaleway_bucket_policies_accept" {
  command = plan

  # Scaleway's documented bucket-policy actions; it rejects a policy naming any other.
  assert {
    condition = alltrue([
      for a in flatten([for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s.Action]) :
      contains([
        "s3:*",
        "s3:ListBucket",
        "s3:GetBucketAcl",
        "s3:GetBucketCORS",
        "s3:GetBucketLocation",
        "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetLifecycleConfiguration",
        "s3:GetObject",
        "s3:PutObject",
      ], a)
    ])
    error_message = "the policy names only actions from Scaleway's supported list"
  }
}

run "platform_host_reaches_its_own_prefixes_exactly" {
  command = plan

  assert {
    condition = toset(flatten([
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : [for r in s.Resource : "${join(",", s.Action)} ${r}"]
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000003" && !contains(s.Action, "s3:ListBucket")
      ])) == toset([
      "s3:GetObject vecten-acme-prod-artifacts-1a2b3c4d/bundles/platform/*",
      "s3:GetObject,s3:PutObject vecten-acme-prod-artifacts-1a2b3c4d/platform/*",
    ])
    error_message = "the platform host reads its bundles and reads and writes its component prefix, nothing else"
  }
  assert {
    condition = [
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : [s.Action, s.Resource, lookup(s, "Condition", null)]
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000003" && contains(s.Action, "s3:ListBucket")
      ] == [[
        ["s3:ListBucket"],
        ["vecten-acme-prod-artifacts-1a2b3c4d"],
        null,
    ]]
    error_message = "the platform host lists the bucket with no prefix condition, so a missing key answers 404"
  }
}

run "mcp_host_reaches_its_own_prefixes_and_the_built_commit" {
  command = plan

  assert {
    condition = toset(flatten([
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : [for r in s.Resource : "${join(",", s.Action)} ${r}"]
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000004" && !contains(s.Action, "s3:ListBucket")
      ])) == toset([
      "s3:GetObject vecten-acme-prod-artifacts-1a2b3c4d/bundles/mcp/*",
      "s3:GetObject,s3:PutObject vecten-acme-prod-artifacts-1a2b3c4d/mcp/*",
      "s3:GetObject vecten-acme-prod-artifacts-1a2b3c4d/platform/dbt/built_commit",
      "s3:GetObject vecten-acme-prod-artifacts-1a2b3c4d/platform/dbt/build_finished_at",
    ])
    error_message = "the MCP host reads its bundles, reads and writes mcp/, and reads the built commit and the build finish time"
  }
  assert {
    condition = [
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : [s.Action, s.Resource, lookup(s, "Condition", null)]
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000004" && contains(s.Action, "s3:ListBucket")
      ] == [[
        ["s3:ListBucket"],
        ["vecten-acme-prod-artifacts-1a2b3c4d"],
        null,
    ]]
    error_message = "the MCP host lists the bucket with no prefix condition, so a missing key answers 404"
  }
}

run "no_host_statement_names_another_host_prefix" {
  command = plan

  assert {
    condition = alltrue([
      for r in flatten([
        for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s.Resource
        if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000003"
      ]) : !strcontains(r, "/mcp/") && !strcontains(r, "bundles/mcp")
    ])
    error_message = "the platform host reaches nothing of the MCP host"
  }
  assert {
    condition = alltrue([
      for r in flatten([
        for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s.Resource
        if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000004"
      ]) : !endswith(r, "/platform/*") && !strcontains(r, "bundles/platform")
    ])
    error_message = "the MCP host reaches nothing of the platform host beyond the built commit"
  }
  assert {
    condition = length([
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000005"
    ]) == 0
    error_message = "an application for a host this profile does not run gets nothing"
  }
}

run "minimal_host_reaches_both_component_prefixes" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition = toset(flatten([
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : [for r in s.Resource : "${join(",", s.Action)} ${r}"]
      if s.Principal.SCW == "application_id:aaaaaaaa-0000-4000-8000-000000000005" && !contains(s.Action, "s3:ListBucket")
      ])) == toset([
      "s3:GetObject vecten-acme-prod-artifacts-1a2b3c4d/bundles/main/*",
      "s3:GetObject,s3:PutObject vecten-acme-prod-artifacts-1a2b3c4d/platform/*",
      "s3:GetObject,s3:PutObject vecten-acme-prod-artifacts-1a2b3c4d/mcp/*",
      "s3:GetObject vecten-acme-prod-artifacts-1a2b3c4d/platform/dbt/built_commit",
      "s3:GetObject vecten-acme-prod-artifacts-1a2b3c4d/platform/dbt/build_finished_at",
    ])
    error_message = "the minimal host carries both components' prefixes"
  }
  assert {
    condition = length([
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s
      if contains(["application_id:aaaaaaaa-0000-4000-8000-000000000003", "application_id:aaaaaaaa-0000-4000-8000-000000000004"], s.Principal.SCW)
    ]) == 0
    error_message = "under minimal the per-component host applications get nothing"
  }
}

run "the_operator_manages_the_bucket" {
  command = plan

  variables {
    operator_principal = "user_id:0bb0bb0b-0000-4000-8000-000000000002"
  }

  assert {
    condition = [
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : [s.Sid, s.Action, s.Resource]
      if s.Principal.SCW == "user_id:0bb0bb0b-0000-4000-8000-000000000002"
      ] == [[
        "OperatorFullAccess",
        ["s3:*"],
        ["vecten-acme-prod-artifacts-1a2b3c4d", "vecten-acme-prod-artifacts-1a2b3c4d/*"],
    ]]
    error_message = "the operator who ran bootstrap may do anything on the bucket, which a laptop plan refreshes and destroy empties and deletes"
  }
}

run "an_operator_application_manages_the_bucket" {
  command = plan

  variables {
    operator_principal = "application_id:0cc0cc0c-0000-4000-8000-000000000003"
  }

  assert {
    condition = contains([
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s.Sid
      if s.Principal.SCW == "application_id:0cc0cc0c-0000-4000-8000-000000000003"
    ], "OperatorFullAccess")
    error_message = "an operator signed in with an application key manages the bucket too"
  }
}

run "no_operator_statement_without_an_operator" {
  command = plan

  assert {
    condition = !contains([
      for s in jsondecode(scaleway_object_bucket_policy.artifacts.policy).Statement : s.Sid
    ], "OperatorFullAccess")
    error_message = "a project bootstrapped before the operator was recorded gets no operator statement"
  }
}

run "rejects_an_operator_principal_of_another_shape" {
  command = plan

  variables {
    operator_principal = "0bb0bb0b-0000-4000-8000-000000000002"
  }

  expect_failures = [var.operator_principal]
}
