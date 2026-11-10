mock_provider "aws" {
  override_data {
    target = data.aws_availability_zones.available
    values = { names = ["eu-west-1a", "eu-west-1b"] }
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  override_data {
    target = data.aws_region.current
    values = { region = "eu-west-1", name = "eu-west-1" }
  }
  override_data {
    target = data.aws_ssm_parameter.al2023
    values = { value = "ami-0abc", insecure_value = "ami-0abc" }
  }
  override_data {
    target = data.aws_route53_zone.this
    values = { zone_id = "Z123" }
  }
}

variables {
  environment       = "prod"
  project_name      = "acme"
  domain            = "data.acme.com"
  dns_zone          = "acme.com"
  region            = "eu-west-1"
  github_repository = "acme/data"
  components        = ["platform", "mcp"]
  profile           = "standard"
  size              = "small"
  warehouse         = "postgres"
}

run "platform_role_reads_and_writes_platform_only" {
  command = plan

  assert {
    condition = alltrue([
      for r in flatten([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource if anytrue([for a in s.Action : startswith(a, "secretsmanager:")])]) :
      strcontains(r, ":secret:vecten/acme/prod/platform/")
    ])
    error_message = "every secrets resource of the platform role is under platform/"
  }
  assert {
    condition = !anytrue([
      for r in flatten([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource]) :
      strcontains(r, "/mcp/")
    ])
    error_message = "the platform role names nothing under mcp/"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource if s.Sid == "ReadScopeSecrets"]) == ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/*"]
    error_message = "the platform role reads platform/* in its own region and account"
  }
  assert {
    condition = toset(one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource if s.Sid == "WriteAirflowSecrets"])) == toset(flatten([
      for n in ["AIRFLOW_FERNET_KEY", "AIRFLOW_API_SECRET_KEY", "AIRFLOW_JWT_SECRET", "AIRFLOW_ADMIN_PASSWORD", "AIRFLOW_BASIC_AUTH_PASSWORD"] : [
        "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/${n}",
        "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/${n}-??????",
      ]
    ]))
    error_message = "the platform role writes exactly its five Airflow app secrets"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Action if s.Sid == "WriteAirflowSecrets"]) == ["secretsmanager:CreateSecret"]
    error_message = "the platform role may create its Airflow app secrets but never rewrite them"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource if s.Sid == "ComponentObjects"]) == ["arn:aws:s3:::vecten-acme-prod-artifacts-123456789012/platform/*"]
    error_message = "the platform role writes the platform/ prefix only"
  }
  assert {
    condition     = contains(one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Action if s.Sid == "ComponentObjects"]), "s3:PutObject")
    error_message = "the component prefix statement grants writes"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource if s.Sid == "ListArtifactsBucket"]) == ["arn:aws:s3:::vecten-acme-prod-artifacts-123456789012"]
    error_message = "the platform role lists the artifacts bucket"
  }
  assert {
    condition     = length([for s in jsondecode(module.host["platform"].scope_policy).Statement : s if s.Sid == "ListArtifactsBucket" && !can(s.Condition)]) == 1
    error_message = "listing carries no prefix condition, so a missing key answers 404, not 403"
  }
  assert {
    condition     = length([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s if s.Sid == "ListArtifactsBucket" && !can(s.Condition)]) == 1
    error_message = "every host role lists the artifacts bucket without a prefix condition"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource if s.Sid == "ReadBundles"]) == ["arn:aws:s3:::vecten-acme-prod-artifacts-123456789012/bundles/platform/*"]
    error_message = "the platform role reads its own bundles only"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Action if s.Sid == "ReadBundles"]) == ["s3:GetObject"]
    error_message = "bundles are read-only to hosts, written only by CI"
  }
  assert {
    condition     = length([for s in jsondecode(module.host["platform"].scope_policy).Statement : s if contains(["WriteGeneratedSecrets", "ReadBuiltCommit"], s.Sid)]) == 0
    error_message = "the platform host is not the setup host and does not read the built commit"
  }
}

run "mcp_role_reads_mcp_and_writes_the_generated_credentials" {
  command = plan

  assert {
    condition     = one([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource if s.Sid == "ReadScopeSecrets"]) == ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/*"]
    error_message = "the MCP role reads mcp/* only"
  }
  assert {
    condition = !anytrue([
      for r in flatten([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource]) :
      endswith(r, "/platform/*")
    ])
    error_message = "the MCP role holds no wildcard under platform/"
  }
  assert {
    condition = toset(one([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource if s.Sid == "WriteGeneratedSecrets"])) == toset([
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/AIRFLOW_DB_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/AIRFLOW_DB_PASSWORD-??????",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/LOADER_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/LOADER_PASSWORD-??????",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_STATE_DB_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_STATE_DB_PASSWORD-??????",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_PROVISIONER_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_PROVISIONER_PASSWORD-??????",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_IMPERSONATOR_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_IMPERSONATOR_PASSWORD-??????",
    ])
    error_message = "the setup host writes exactly the generated credentials"
  }
  assert {
    condition     = toset(one([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Action if s.Sid == "WriteGeneratedSecrets"])) == toset(["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"])
    error_message = "the setup host creates, writes and reads back the generated credentials"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource if s.Sid == "ReadBuiltCommit"]) == ["arn:aws:s3:::vecten-acme-prod-artifacts-123456789012/platform/dbt/built_commit"]
    error_message = "the MCP role reads the built-commit marker"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource if s.Sid == "ComponentObjects"]) == ["arn:aws:s3:::vecten-acme-prod-artifacts-123456789012/mcp/*"]
    error_message = "the MCP role writes the mcp/ prefix only"
  }
  assert {
    condition     = length([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s if s.Sid == "WriteAirflowSecrets"]) == 0
    error_message = "the MCP role does not write the Airflow app secrets"
  }
}

run "master_secrets_go_to_the_setup_host_only" {
  command = plan

  assert {
    condition     = aws_iam_role_policy.master_secrets.role == "vecten-acme-prod-mcp"
    error_message = "only the setup host reads the master secrets"
  }
}

run "platform_only_setup_host_writes_platform_names" {
  command = plan

  variables {
    components = ["platform"]
  }

  assert {
    condition     = aws_iam_role_policy.master_secrets.role == "vecten-acme-prod-platform"
    error_message = "without mcp the platform host is the setup host"
  }
  assert {
    condition = toset(one([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource if s.Sid == "WriteGeneratedSecrets"])) == toset([
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/AIRFLOW_DB_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/AIRFLOW_DB_PASSWORD-??????",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/LOADER_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/LOADER_PASSWORD-??????",
    ])
    error_message = "without mcp the generated credentials are the platform ones"
  }
  assert {
    condition = !anytrue([
      for r in flatten([for s in jsondecode(module.host["platform"].scope_policy).Statement : s.Resource]) :
      strcontains(r, "/mcp/")
    ])
    error_message = "without mcp nothing names mcp/"
  }
}

run "snowflake_setup_host_writes_state_credentials_only" {
  command = plan

  variables {
    warehouse = "snowflake"
  }

  assert {
    condition = toset(one([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource if s.Sid == "WriteGeneratedSecrets"])) == toset([
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/AIRFLOW_DB_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/AIRFLOW_DB_PASSWORD-??????",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_STATE_DB_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_STATE_DB_PASSWORD-??????",
    ])
    error_message = "without a Postgres warehouse the setup host writes the state credentials only"
  }
}

run "minimal_role_carries_both_scopes" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition = toset(one([for s in jsondecode(module.host["main"].scope_policy).Statement : s.Resource if s.Sid == "ReadScopeSecrets"])) == toset([
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/platform/*",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/*",
    ])
    error_message = "the minimal host reads both scopes"
  }
  assert {
    condition = toset(one([for s in jsondecode(module.host["main"].scope_policy).Statement : s.Resource if s.Sid == "ComponentObjects"])) == toset([
      "arn:aws:s3:::vecten-acme-prod-artifacts-123456789012/platform/*",
      "arn:aws:s3:::vecten-acme-prod-artifacts-123456789012/mcp/*",
    ])
    error_message = "the minimal host writes both prefixes"
  }
  assert {
    condition     = one([for s in jsondecode(module.host["main"].scope_policy).Statement : s.Resource if s.Sid == "ReadBundles"]) == ["arn:aws:s3:::vecten-acme-prod-artifacts-123456789012/bundles/main/*"]
    error_message = "the minimal host reads the main bundles"
  }
  assert {
    condition     = aws_iam_role_policy.master_secrets.role == "vecten-acme-prod-main"
    error_message = "the minimal host reads the master secret"
  }
  assert {
    condition = !anytrue([
      for r in flatten([for s in jsondecode(module.host["main"].scope_policy).Statement : s.Resource]) :
      strcontains(r, "/deploy/") || strcontains(r, "/admin/")
    ])
    error_message = "no host reads deploy/ or admin/"
  }
  assert {
    condition     = module.host["main"].permissions_boundary == "arn:aws:iam::123456789012:policy/vecten-acme-prod-boundary"
    error_message = "the minimal host role carries the boundary bootstrap creates"
  }
}

run "every_host_role_carries_the_environment_boundary" {
  command = plan

  assert {
    condition = alltrue([
      for h in ["platform", "mcp"] :
      module.host[h].permissions_boundary == "arn:aws:iam::123456789012:policy/vecten-acme-prod-boundary"
    ])
    error_message = "every host role carries the boundary bootstrap creates"
  }
}

run "hosts_run_with_ssm_and_imdsv2" {
  command = plan

  assert {
    condition     = alltrue([for h in module.host : h.settings.http_tokens == "required" && h.settings.http_put_response_hop_limit == 2])
    error_message = "every host requires IMDSv2 with a hop limit of 2 so containers reach the role"
  }
  assert {
    condition     = alltrue([for h in module.host : h.settings.managed_policy_arns == ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]])
    error_message = "every host carries the SSM core policy and no other managed policy"
  }
  assert {
    condition     = alltrue([for h in module.host : h.settings.root_volume.encrypted && h.settings.root_volume.volume_type == "gp3" && h.settings.root_volume.volume_size == 30])
    error_message = "every root volume is an encrypted 30 GB gp3 volume"
  }
  assert {
    condition     = alltrue([for h in module.host : h.settings.ami == "ami-0abc"])
    error_message = "hosts boot the AL2023 AMI from the SSM parameter"
  }
}

run "user_data_installs_the_pinned_stack" {
  command = plan

  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "dnf install -y docker python3 postgresql17")
    error_message = "user data installs Docker, Python and the Postgres 16 client"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "https://github.com/docker/compose/releases/download/v${local.compose_plugin_version}/docker-compose-linux-x86_64")
    error_message = "user data fetches the pinned Compose plugin"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "/usr/libexec/docker/cli-plugins/docker-compose")
    error_message = "the Compose plugin lands where Docker looks for CLI plugins"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "https://github.com/docker/buildx/releases/download/v${local.buildx_plugin_version}/buildx-v${local.buildx_plugin_version}.linux-amd64")
    error_message = "user data fetches the pinned buildx plugin"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "https://github.com/docker/buildx/releases/download/v${local.buildx_plugin_version}/checksums.txt")
    error_message = "user data verifies buildx against its published checksums"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "/usr/libexec/docker/cli-plugins/docker-buildx")
    error_message = "the buildx plugin lands where Docker looks for CLI plugins"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, local.certbot_image)
    error_message = "renewal runs the pinned certbot image"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "python3 /opt/vecten/releases/current/.vecten/ship/apply.py boot")
    error_message = "the boot unit rebuilds the run files from the current release"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "vecten-certbot-renew.timer")
    error_message = "a timer renews the certificates"
  }
  # The position of a step is the length of what precedes its first occurrence.
  assert {
    condition = alltrue([
      for step in ["systemctl enable --now docker", "systemctl enable vecten-boot.service", "systemctl enable --now vecten-certbot-renew.timer"] :
      length(split(step, module.host["platform"].settings.user_data)[0]) < length(split("retry curl", module.host["platform"].settings.user_data)[0])
    ])
    error_message = "Docker, the boot unit and the renewal timer are enabled before any plugin download can fail"
  }
  assert {
    condition     = strcontains(module.host["platform"].settings.user_data, "grep -E \"^[0-9a-f]{64} [ *]$buildx_asset\\$\" checksums.txt")
    error_message = "the buildx checksum line matches with or without the binary marker"
  }
}

run "mcp_rotates_only_standing_identity_secrets" {
  command = plan
  assert {
    condition = toset(one([
      for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource
      if s.Sid == "RotateMcpIdentitySecrets"
      ])) == toset(flatten([
      for n in ["MCP_SIGNING_KEY", "MCP_IMPERSONATOR_PASSWORD"] : [
        "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/${n}",
        "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/${n}-??????",
      ]
    ]))
    error_message = "MCP can rotate only its two rotating identity secrets."
  }
}
