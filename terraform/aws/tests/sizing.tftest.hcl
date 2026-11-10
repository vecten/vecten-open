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
  size              = "medium"
  warehouse         = "postgres"
}

run "medium_tier_moves_mcp_and_warehouse_only" {
  command = plan

  assert {
    condition     = module.host["mcp"].instance_type == "t3.large"
    error_message = "the MCP VM follows size"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.instance_class == "db.m6g.large"
    error_message = "the warehouse instance follows size"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.allocated_storage == 200 && module.postgres["warehouse"].settings.max_allocated_storage == 1000
    error_message = "the warehouse storage and its ceiling follow size"
  }
  assert {
    condition     = module.host["platform"].instance_type == "t3.medium"
    error_message = "the platform VM stays small by default"
  }
  assert {
    condition     = module.postgres["state"].settings.instance_class == "db.t4g.micro"
    error_message = "the state instance is always the smallest class"
  }
  assert {
    condition     = module.postgres["state"].settings.allocated_storage == 20
    error_message = "the state instance starts at the table's state storage"
  }
}

run "small_tier" {
  command = plan

  variables {
    size = "small"
  }

  assert {
    condition     = module.host["mcp"].instance_type == "t3.medium" && module.postgres["warehouse"].settings.instance_class == "db.t4g.small"
    error_message = "small picks the small entries"
  }
}

run "large_tier" {
  command = plan

  variables {
    size = "large"
  }

  assert {
    condition     = module.host["mcp"].instance_type == "t3.xlarge" && module.postgres["warehouse"].settings.instance_class == "db.m6g.xlarge"
    error_message = "large picks the large entries"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.allocated_storage == 500 && module.postgres["warehouse"].settings.max_allocated_storage == 2000
    error_message = "large storage follows the table"
  }
}

run "platform_size_moves_the_platform_vm" {
  command = plan

  variables {
    platform_size = "medium"
    size          = "small"
  }

  assert {
    condition     = module.host["platform"].instance_type == "t3.large"
    error_message = "platform_size picks the platform VM's tier"
  }
  assert {
    condition     = module.host["mcp"].instance_type == "t3.medium"
    error_message = "platform_size leaves the MCP VM on size"
  }
}

run "overrides_win" {
  command = plan

  variables {
    instance_type_overrides = {
      platform  = "m7i.large"
      mcp       = "m7i.xlarge"
      state     = "db.t4g.small"
      warehouse = "db.r6g.large"
    }
  }

  assert {
    condition     = module.host["platform"].instance_type == "m7i.large" && module.host["mcp"].instance_type == "m7i.xlarge"
    error_message = "host overrides win over the table"
  }
  assert {
    condition     = module.postgres["state"].settings.instance_class == "db.t4g.small" && module.postgres["warehouse"].settings.instance_class == "db.r6g.large"
    error_message = "instance overrides win over the table"
  }
}

run "minimal_vm_ignores_size" {
  command = plan

  variables {
    profile = "minimal"
    size    = "large"
  }

  assert {
    condition     = module.host["main"].instance_type == "t3.large"
    error_message = "the minimal VM is minimal_vm whatever the size"
  }
  assert {
    condition     = module.postgres["state"].settings.instance_class == "db.m6g.xlarge"
    error_message = "the minimal instance holding the warehouse takes the tier's warehouse class"
  }
  assert {
    condition     = module.postgres["state"].settings.allocated_storage == 500 && module.postgres["state"].settings.max_allocated_storage == 2000
    error_message = "the minimal instance holding the warehouse takes the tier's warehouse storage"
  }
}

run "minimal_small_instance_carries_the_small_warehouse_class" {
  command = plan

  variables {
    profile = "minimal"
    size    = "small"
  }

  assert {
    condition     = module.postgres["state"].settings.instance_class == "db.t4g.small"
    error_message = "the minimal instance holding the warehouse takes the small tier's warehouse class"
  }
}

run "minimal_snowflake_instance_stays_the_state_class" {
  command = plan

  variables {
    profile   = "minimal"
    size      = "large"
    warehouse = "snowflake"
  }

  assert {
    condition     = module.postgres["state"].settings.instance_class == "db.t4g.micro"
    error_message = "without the warehouse database the minimal instance is the state class"
  }
  assert {
    condition     = module.postgres["state"].settings.allocated_storage == 20
    error_message = "without the warehouse database the minimal instance takes the state storage"
  }
}

run "minimal_override" {
  command = plan

  variables {
    profile                 = "minimal"
    instance_type_overrides = { main = "t3.xlarge" }
  }

  assert {
    condition     = module.host["main"].instance_type == "t3.xlarge"
    error_message = "the main override wins"
  }
}

run "rejects_an_unknown_size" {
  command = plan

  variables {
    size = "huge"
  }

  expect_failures = [var.size]
}

run "rejects_an_unknown_platform_size" {
  command = plan

  variables {
    platform_size = "tiny"
  }

  expect_failures = [var.platform_size]
}

run "rejects_an_unknown_override_key" {
  command = plan

  variables {
    instance_type_overrides = { airflow = "t3.small" }
  }

  expect_failures = [var.instance_type_overrides]
}
