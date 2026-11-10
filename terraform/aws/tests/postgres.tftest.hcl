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
  override_resource {
    target          = aws_subnet.private
    override_during = plan
    values          = { id = "subnet-private" }
  }
  override_resource {
    target          = aws_subnet.public
    override_during = plan
    values          = { id = "subnet-public" }
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

run "every_instance_is_encrypted_backed_up_and_private" {
  command = plan

  assert {
    condition     = alltrue([for m in module.postgres : m.settings.storage_encrypted])
    error_message = "every instance is encrypted at rest"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.backup_retention_period == 7])
    error_message = "every instance keeps seven days of backups"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.manage_master_user_password])
    error_message = "RDS manages every master password, so none sits in state"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.username == "vecten_master"])
    error_message = "the master login is vecten_master"
  }
  assert {
    condition     = alltrue([for m in module.postgres : !m.settings.publicly_accessible])
    error_message = "no instance is publicly accessible by default"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.subnet_ids == toset(["subnet-private"])])
    error_message = "every subnet group uses the private subnets by default"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.max_allocated_storage > m.settings.allocated_storage])
    error_message = "storage auto-grows on every instance"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.engine == "postgres" && m.settings.engine_version == "17"])
    error_message = "every instance runs Postgres 17"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.storage_type == "gp3"])
    error_message = "every instance uses gp3 storage"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.skip_final_snapshot && m.settings.copy_tags_to_snapshot])
    error_message = "destroy takes its own final snapshot, so Terraform skips one; tags reach snapshots"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.parameter_group_family == "postgres17"])
    error_message = "parameter groups are postgres17"
  }
}

run "prod_has_deletion_protection" {
  command = plan

  assert {
    condition     = alltrue([for m in module.postgres : m.settings.deletion_protection])
    error_message = "prod instances have deletion protection"
  }
}

run "destroying_lifts_deletion_protection" {
  command = plan

  variables {
    destroying = true
  }

  assert {
    condition     = alltrue([for m in module.postgres : !m.settings.deletion_protection])
    error_message = "destroying = true lifts deletion protection"
  }
  assert {
    condition     = aws_s3_bucket.artifacts.force_destroy
    error_message = "destroying = true lets destroy empty the artifacts bucket"
  }
}

run "staging_has_no_deletion_protection" {
  command = plan

  variables {
    environment = "staging"
  }

  assert {
    condition     = alltrue([for m in module.postgres : !m.settings.deletion_protection])
    error_message = "only prod instances have deletion protection"
  }
  assert {
    condition     = !aws_s3_bucket.artifacts.force_destroy
    error_message = "the artifacts bucket keeps its objects unless destroying"
  }
}

run "log_line_prefix_only_where_warehouse_lives" {
  command = plan

  assert {
    condition     = module.postgres["warehouse"].settings.parameters == { log_line_prefix = "%m:%r:%u@%d:[%p]:%l:%e:%s:%v:%x:%c:%q%a:" }
    error_message = "the warehouse instance names the session user and application in every log line"
  }
  assert {
    condition     = module.postgres["state"].settings.parameters == {}
    error_message = "the state instance keeps the RDS default log prefix"
  }
}

run "log_line_prefix_is_overridable" {
  command = plan

  variables {
    log_line_prefix = "%t:%r:%u@%d:[%p]:"
  }

  assert {
    condition     = module.postgres["warehouse"].settings.parameters["log_line_prefix"] == "%t:%r:%u@%d:[%p]:"
    error_message = "tfvars can fall back to another prefix"
  }
}

run "minimal_instance_carries_the_prefix_on_postgres" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition     = module.postgres["state"].settings.parameters == { log_line_prefix = "%m:%r:%u@%d:[%p]:%l:%e:%s:%v:%x:%c:%q%a:" }
    error_message = "the minimal instance holds warehouse and so carries the prefix"
  }
  assert {
    condition     = module.postgres["state"].settings.max_allocated_storage == 200
    error_message = "the minimal instance holding warehouse grows to the tier's warehouse ceiling"
  }
}

run "minimal_snowflake_instance_has_no_prefix" {
  command = plan

  variables {
    profile   = "minimal"
    warehouse = "snowflake"
  }

  assert {
    condition     = module.postgres["state"].settings.parameters == {}
    error_message = "without a warehouse database no instance carries the prefix"
  }
}

run "public_cidrs_open_the_database_explicitly" {
  command = plan

  variables {
    database_public_cidrs = ["203.0.113.0/24"]
  }

  assert {
    condition     = alltrue([for m in module.postgres : m.settings.publicly_accessible])
    error_message = "a public CIDR list makes the instances publicly accessible"
  }
  assert {
    condition     = alltrue([for m in module.postgres : m.settings.subnet_ids == toset(["subnet-public"])])
    error_message = "a publicly accessible instance sits in the public subnets"
  }
  assert {
    condition     = keys(aws_vpc_security_group_ingress_rule.database_public) == ["203.0.113.0/24"]
    error_message = "each public CIDR gets its own ingress rule"
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.database_public["203.0.113.0/24"].from_port == 5432 && aws_vpc_security_group_ingress_rule.database_public["203.0.113.0/24"].cidr_ipv4 == "203.0.113.0/24"
    error_message = "the public rule opens 5432 to that CIDR only"
  }
}

run "rejects_a_malformed_cidr" {
  command = plan

  variables {
    database_public_cidrs = ["not-a-cidr"]
  }

  expect_failures = [var.database_public_cidrs]
}

run "rejects_an_open_world_cidr" {
  command = plan

  variables {
    database_public_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.database_public_cidrs]
}
