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

run "standard_both_components_on_postgres" {
  command = plan

  assert {
    condition     = keys(output.hosts) == ["mcp", "platform"]
    error_message = "standard with both components runs one host per component"
  }
  assert {
    condition     = output.hosts["platform"].components == tolist(["platform"]) && output.hosts["mcp"].components == tolist(["mcp"])
    error_message = "each standard host carries its own component only"
  }
  assert {
    condition     = output.hosts["platform"].hostnames == tolist(["airflow.data.acme.com"]) && output.hosts["mcp"].hostnames == tolist(["mcp.data.acme.com"])
    error_message = "airflow.<domain> belongs to the platform host and mcp.<domain> to the MCP host"
  }
  assert {
    condition     = keys(output.postgres) == ["state", "warehouse"]
    error_message = "standard on Postgres runs a state and a warehouse instance"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state"])
    error_message = "the state instance holds airflow and mcp_state"
  }
  assert {
    condition     = output.postgres["warehouse"].databases == tolist(["warehouse"])
    error_message = "the warehouse instance holds warehouse only"
  }
  assert {
    condition     = output.postgres["state"].port == 5432
    error_message = "Postgres listens on 5432"
  }
  assert {
    condition     = output.setup_host == "mcp"
    error_message = "the MCP host is the setup host when mcp is a component"
  }
  assert {
    condition     = keys(aws_route53_record.hostname) == ["airflow.data.acme.com", "mcp.data.acme.com"]
    error_message = "both hostnames get a record"
  }
  assert {
    condition     = alltrue([for r in aws_route53_record.hostname : r.type == "A" && r.ttl == 60 && r.zone_id == "Z123"])
    error_message = "records are A records with TTL 60 in the named zone"
  }
  assert {
    condition     = output.environment == "prod" && output.region == "eu-west-1"
    error_message = "environment and region echo the inputs"
  }
  assert {
    condition     = output.secret_prefix == "vecten/acme/prod/"
    error_message = "the secret prefix is vecten/<project>/<env>/"
  }
  assert {
    condition     = output.bucket == "vecten-acme-prod-artifacts-123456789012"
    error_message = "the artifacts bucket is vecten-<project>-<env>-artifacts-<account>"
  }
  assert {
    condition     = module.postgres["state"].identifier == "vecten-acme-prod-state" && module.postgres["warehouse"].identifier == "vecten-acme-prod-warehouse"
    error_message = "RDS identifiers are <name prefix>-<key>, the names destroy looks up"
  }
  assert {
    condition     = module.host["platform"].role_name == "vecten-acme-prod-platform" && module.host["mcp"].role_name == "vecten-acme-prod-mcp"
    error_message = "host roles are named <name prefix>-<host>"
  }
}

run "host_ingress_is_80_and_443_only" {
  command = plan

  assert {
    condition     = length(aws_security_group.hosts.ingress) == 2
    error_message = "the host security group has exactly two ingress rules"
  }
  assert {
    condition     = toset([for r in aws_security_group.hosts.ingress : r.from_port]) == toset([80, 443])
    error_message = "hosts accept 80 and 443"
  }
  assert {
    condition     = alltrue([for r in aws_security_group.hosts.ingress : r.from_port == r.to_port && r.protocol == "tcp" && r.cidr_blocks == tolist(["0.0.0.0/0"])])
    error_message = "each host ingress rule opens one TCP port to the internet"
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.database_public) == 0
    error_message = "databases have no public ingress by default"
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.database_from_hosts.from_port == 5432 && aws_vpc_security_group_ingress_rule.database_from_hosts.to_port == 5432
    error_message = "databases accept 5432 from the hosts"
  }
}

run "artifacts_bucket_is_private_versioned_and_encrypted" {
  command = plan

  assert {
    condition = alltrue([
      for r in aws_s3_bucket_server_side_encryption_configuration.artifacts.rule :
      r.apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    ]) && length(aws_s3_bucket_server_side_encryption_configuration.artifacts.rule) == 1
    error_message = "the bucket is encrypted with SSE AES256"
  }
  assert {
    condition = (
      aws_s3_bucket_public_access_block.artifacts.block_public_acls
      && aws_s3_bucket_public_access_block.artifacts.block_public_policy
      && aws_s3_bucket_public_access_block.artifacts.ignore_public_acls
      && aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets
    )
    error_message = "every public access block setting is on"
  }
  assert {
    condition     = aws_s3_bucket_versioning.artifacts.versioning_configuration[0].status == "Enabled"
    error_message = "the bucket is versioned"
  }
  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.artifacts.rule) == 1
    error_message = "the bucket has one lifecycle rule"
  }
  assert {
    condition = (
      aws_s3_bucket_lifecycle_configuration.artifacts.rule[0].status == "Enabled"
      && aws_s3_bucket_lifecycle_configuration.artifacts.rule[0].filter[0].prefix == "bundles/"
      && aws_s3_bucket_lifecycle_configuration.artifacts.rule[0].noncurrent_version_expiration[0].noncurrent_days == 30
    )
    error_message = "noncurrent bundle versions expire after 30 days"
  }
}

run "network_has_two_public_and_two_private_subnets" {
  command = plan

  assert {
    condition     = aws_vpc.this.cidr_block == "10.42.0.0/16" && aws_vpc.this.enable_dns_hostnames
    error_message = "the VPC is 10.42.0.0/16 with DNS hostnames"
  }
  assert {
    condition     = [for s in aws_subnet.public : s.availability_zone] == ["eu-west-1a", "eu-west-1b"]
    error_message = "public subnets span the first two availability zones"
  }
  assert {
    condition     = [for s in aws_subnet.private : s.availability_zone] == ["eu-west-1a", "eu-west-1b"]
    error_message = "private subnets span the first two availability zones"
  }
  assert {
    condition     = alltrue([for s in aws_subnet.private : !s.map_public_ip_on_launch])
    error_message = "private subnets assign no public address"
  }
}

run "minimal_runs_one_host_and_one_instance" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition     = keys(output.hosts) == ["main"]
    error_message = "minimal runs one host named main"
  }
  assert {
    condition     = module.host["main"].instance_type == "t3.large"
    error_message = "the minimal VM is the minimal_vm entry, 8 GB"
  }
  assert {
    condition     = output.hosts["main"].components == tolist(["platform", "mcp"])
    error_message = "the minimal host carries both components"
  }
  assert {
    condition     = output.hosts["main"].hostnames == tolist(["airflow.data.acme.com", "mcp.data.acme.com"])
    error_message = "the minimal host serves both hostnames"
  }
  assert {
    condition     = keys(output.postgres) == ["state"]
    error_message = "minimal runs one instance"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state", "warehouse"])
    error_message = "the minimal instance holds all three databases"
  }
  assert {
    condition     = output.setup_host == "main"
    error_message = "the minimal host is the setup host"
  }
  assert {
    condition     = length(aws_route53_record.hostname) == 2
    error_message = "both hostnames get a record under minimal"
  }
}

run "mcp_only" {
  command = plan

  variables {
    components = ["mcp"]
  }

  assert {
    condition     = keys(output.hosts) == ["mcp"]
    error_message = "MCP only runs one MCP host"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["mcp_state"])
    error_message = "the state instance holds mcp_state only"
  }
  assert {
    condition     = keys(aws_route53_record.hostname) == ["mcp.data.acme.com"]
    error_message = "MCP only issues no airflow record"
  }
  assert {
    condition     = output.setup_host == "mcp"
    error_message = "the MCP host is the setup host"
  }
  assert {
    condition     = keys(output.postgres) == ["state"]
    error_message = "MCP only sits over an existing warehouse, so no warehouse instance exists"
  }
  assert {
    condition     = module.postgres["state"].settings.parameters == {}
    error_message = "without a warehouse instance no parameter group carries the log prefix"
  }
  assert {
    condition = toset(one([for s in jsondecode(module.host["mcp"].scope_policy).Statement : s.Resource if s.Sid == "WriteGeneratedSecrets"])) == toset([
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_STATE_DB_PASSWORD",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:vecten/acme/prod/mcp/MCP_STATE_DB_PASSWORD-??????",
    ])
    error_message = "MCP only generates the state credential; the provisioner credential is stored by hand"
  }
}

run "mcp_only_minimal" {
  command = plan

  variables {
    components = ["mcp"]
    profile    = "minimal"
  }

  assert {
    condition     = keys(output.postgres) == ["state"]
    error_message = "minimal runs one instance"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["mcp_state"])
    error_message = "MCP only under minimal holds mcp_state alone"
  }
  assert {
    condition     = module.postgres["state"].settings.parameters == {}
    error_message = "the one instance holds no warehouse, so it keeps the RDS log prefix"
  }
}

run "rejects_a_provider_in_another_region" {
  command = plan

  variables {
    region = "us-east-1"
  }

  expect_failures = [aws_vpc.this]
}

run "rejects_a_project_name_vecten_init_would_refuse" {
  command = plan

  variables {
    project_name = "Acme Corp"
  }

  expect_failures = [var.project_name]
}

run "platform_only" {
  command = plan

  variables {
    components = ["platform"]
  }

  assert {
    condition     = keys(output.hosts) == ["platform"]
    error_message = "platform only runs one platform host"
  }
  assert {
    condition     = output.setup_host == "platform"
    error_message = "the platform host is the setup host without mcp"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow"])
    error_message = "the state instance holds airflow only"
  }
}

run "snowflake_keeps_state_only" {
  command = plan

  variables {
    warehouse         = "snowflake"
    snowflake_account = "ORG-ACCOUNT"
  }

  assert {
    condition     = keys(output.postgres) == ["state"]
    error_message = "a Snowflake project runs no warehouse instance"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state"])
    error_message = "the state instance still holds airflow and mcp_state"
  }
}

run "minimal_snowflake_holds_no_warehouse_database" {
  command = plan

  variables {
    profile   = "minimal"
    warehouse = "snowflake"
  }

  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state"])
    error_message = "minimal on Snowflake keeps the warehouse out of Postgres"
  }
}

run "long_project_name_keeps_names_legal" {
  command = plan

  variables {
    project_name = "a_very_long_project_name--with_symbols_and_more_words_than_fit"
    environment  = "staging"
  }

  assert {
    condition     = module.postgres["warehouse"].identifier == "vecten-a-very-long-project-name-with-symbols-staging-warehouse"
    error_message = "the name prefix follows vecten.deploy.names.name_prefix"
  }
  assert {
    condition     = output.bucket == "vecten-a-very-long-project-name-staging-artifacts-123456789012"
    error_message = "the artifacts bucket is cut like the state bucket, without a hyphen before the env"
  }
  assert {
    condition     = length(output.bucket) <= 63
    error_message = "the artifacts bucket stays within 63 characters"
  }
}

run "rejects_an_unknown_component" {
  command = plan

  variables {
    components = ["platform", "airflow"]
  }

  expect_failures = [var.components]
}

run "rejects_no_component" {
  command = plan

  variables {
    components = []
  }

  expect_failures = [var.components]
}

run "rejects_an_unknown_profile" {
  command = plan

  variables {
    profile = "tiny"
  }

  expect_failures = [var.profile]
}

run "rejects_an_unknown_warehouse" {
  command = plan

  variables {
    warehouse = "bigquery"
  }

  expect_failures = [var.warehouse]
}

run "rejects_a_domain_outside_the_zone" {
  command = plan

  variables {
    domain = "data.other.com"
  }

  expect_failures = [var.domain]
}

run "rejects_an_environment_ending_in_a_hyphen" {
  command = plan

  variables {
    environment = "prod-"
  }

  expect_failures = [var.environment]
}

run "rejects_an_environment_with_doubled_hyphens" {
  command = plan

  variables {
    environment = "prod--eu"
  }

  expect_failures = [var.environment]
}
