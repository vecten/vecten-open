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

run "instances_are_private_postgres_16_with_seven_day_backups" {
  command = plan

  assert {
    condition     = alltrue([for p in module.postgres : p.settings.engine == "PostgreSQL-17"])
    error_message = "every instance runs PostgreSQL 17"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.backup_schedule_retention == 7 && p.settings.backup_schedule_frequency == 24 && !p.settings.disable_backup])
    error_message = "every instance keeps daily backups for seven days"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.encryption_at_rest])
    error_message = "every instance is encrypted at rest"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.private_network_enable_ipam && p.settings.private_network_count == 1])
    error_message = "every instance has one private network endpoint registered in IPAM"
  }
  assert {
    condition     = alltrue([for p in module.postgres : !p.settings.public_endpoint && p.settings.acl_ips == null])
    error_message = "no instance has a public endpoint or an ACL by default"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.is_ha_cluster == false])
    error_message = "instances are single-node"
  }
}

run "admin_password_is_write_only" {
  command = plan

  assert {
    condition     = alltrue([for p in module.postgres : p.settings.user_name == "vecten_admin"])
    error_message = "the first user is vecten_admin"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.password_is_null])
    error_message = "no instance sets the stored password attribute"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.password_wo_version == 1])
    error_message = "the write-only password carries version 1 by default"
  }
}

run "password_versions_move_each_instance" {
  command = plan

  variables {
    state_admin_password_version     = 3
    warehouse_admin_password_version = 2
  }

  assert {
    condition     = module.postgres["state"].settings.password_wo_version == 3
    error_message = "state_admin_password_version rotates the state instance"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.password_wo_version == 2
    error_message = "warehouse_admin_password_version rotates the warehouse instance"
  }
}

run "instances_keep_the_engine_settings" {
  command = plan

  assert {
    condition     = length(module.postgres["warehouse"].settings.engine_settings) == 0
    error_message = "Managed Databases accept no log_line_prefix, so the warehouse instance sets nothing"
  }
  assert {
    condition     = length(module.postgres["state"].settings.engine_settings) == 0
    error_message = "the state instance keeps the engine's settings"
  }
}

run "minimal_instance_keeps_the_engine_settings" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition     = length(module.postgres["state"].settings.engine_settings) == 0
    error_message = "the one minimal instance holds the warehouse and still sets nothing"
  }
}

run "public_cidrs_open_a_public_endpoint_with_an_acl" {
  command = plan

  variables {
    database_public_cidrs = ["203.0.113.0/24", "198.51.100.7/32"]
  }

  assert {
    condition     = alltrue([for p in module.postgres : p.settings.public_endpoint])
    error_message = "every instance gets a public endpoint when CIDRs are listed"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.acl_ips == ["203.0.113.0/24", "198.51.100.7/32"]])
    error_message = "the ACL admits exactly the listed CIDRs"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.private_network_count == 1])
    error_message = "the private endpoint stays beside the public one"
  }
}

run "rejects_an_open_cidr" {
  command = plan

  variables {
    database_public_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.database_public_cidrs]
}

run "rejects_a_password_version_below_one" {
  command = plan

  variables {
    state_admin_password_version = 0
  }

  expect_failures = [var.state_admin_password_version]
}
