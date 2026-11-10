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
  size              = "medium"
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

run "medium_tier_moves_mcp_and_warehouse_only" {
  command = plan

  assert {
    condition     = module.host["mcp"].instance_type == "POP2-2C-8G"
    error_message = "the MCP VM follows size"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.node_type == "DB-PRO2-XXS"
    error_message = "the warehouse instance follows size"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.volume_size_in_gb == 200
    error_message = "the warehouse volume follows size"
  }
  assert {
    condition     = module.host["platform"].instance_type == "DEV1-M"
    error_message = "the platform VM stays small by default"
  }
  assert {
    condition     = module.postgres["state"].settings.node_type == "DB-PLAY2-NANO"
    error_message = "the state instance is the table's state class"
  }
  assert {
    condition     = module.postgres["state"].settings.volume_size_in_gb == 20
    error_message = "the state instance takes the table's state storage"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.volume_type == "sbs_5k"])
    error_message = "every instance sits on the table's SBS volume type, which takes a fixed size"
  }
}

run "root_volume_follows_the_instance_type" {
  command = plan

  assert {
    condition     = module.host["platform"].settings.root_volume.volume_type == "l_ssd"
    error_message = "a DEV1 server boots from its local SSD"
  }
  assert {
    condition     = module.host["mcp"].settings.root_volume.volume_type == "sbs_volume"
    error_message = "a POP2 server has no local disk and boots from an SBS volume"
  }
  assert {
    condition     = alltrue([for h in module.host : h.settings.root_volume.size_in_gb == 30 && h.settings.root_volume.delete_on_termination])
    error_message = "every root volume is 30 GB and goes with its server"
  }
}

run "small_tier" {
  command = plan

  variables {
    size = "small"
  }

  assert {
    condition     = module.host["mcp"].instance_type == "DEV1-M" && module.postgres["warehouse"].settings.node_type == "DB-PLAY2-PICO"
    error_message = "small picks the small entries"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.volume_size_in_gb == 50
    error_message = "small storage follows the table"
  }
}

run "large_tier" {
  command = plan

  variables {
    size = "large"
  }

  assert {
    condition     = module.host["mcp"].instance_type == "POP2-4C-16G" && module.postgres["warehouse"].settings.node_type == "DB-PRO2-XS"
    error_message = "large picks the large entries"
  }
  assert {
    condition     = module.postgres["warehouse"].settings.volume_size_in_gb == 500
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
    condition     = module.host["platform"].instance_type == "POP2-2C-8G"
    error_message = "platform_size picks the platform VM's tier"
  }
  assert {
    condition     = module.host["mcp"].instance_type == "DEV1-M"
    error_message = "platform_size leaves the MCP VM on size"
  }
}

run "overrides_win" {
  command = plan

  variables {
    instance_type_overrides = {
      platform  = "POP2-4C-16G"
      mcp       = "DEV1-L"
      state     = "DB-PLAY2-PICO"
      warehouse = "DB-PRO2-S"
    }
  }

  assert {
    condition     = module.host["platform"].instance_type == "POP2-4C-16G" && module.host["mcp"].instance_type == "DEV1-L"
    error_message = "host overrides win over the table"
  }
  assert {
    condition     = module.host["mcp"].settings.root_volume.volume_type == "l_ssd" && module.host["platform"].settings.root_volume.volume_type == "sbs_volume"
    error_message = "the root volume follows the overridden type"
  }
  assert {
    condition     = module.postgres["state"].settings.node_type == "DB-PLAY2-PICO" && module.postgres["warehouse"].settings.node_type == "DB-PRO2-S"
    error_message = "instance overrides win over the table"
  }
}

run "minimal_takes_the_minimal_vm_and_the_tier_minimal_class" {
  command = plan

  variables {
    profile = "minimal"
    size    = "large"
  }

  assert {
    condition     = module.host["main"].instance_type == "DEV1-L"
    error_message = "the minimal VM is minimal_vm whatever the size"
  }
  assert {
    condition     = module.postgres["state"].settings.node_type == "DB-PRO2-XS"
    error_message = "the minimal instance holding the warehouse takes the tier's minimal_class"
  }
  assert {
    condition     = module.postgres["state"].settings.volume_size_in_gb == 500
    error_message = "the minimal instance holding the warehouse takes the tier's warehouse storage"
  }
}

run "minimal_small_takes_minimal_class_not_warehouse_class" {
  command = plan

  variables {
    profile = "minimal"
    size    = "small"
  }

  assert {
    condition     = module.postgres["state"].settings.node_type == "DB-PLAY2-NANO"
    error_message = "on small the minimal instance is minimal_class, which holds the shared state connections"
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
    condition     = module.postgres["state"].settings.node_type == "DB-PLAY2-NANO"
    error_message = "without the warehouse database the minimal instance is the state class"
  }
  assert {
    condition     = module.postgres["state"].settings.volume_size_in_gb == 20
    error_message = "without the warehouse database the minimal instance takes the state storage"
  }
}

run "minimal_override" {
  command = plan

  variables {
    profile                 = "minimal"
    instance_type_overrides = { main = "POP2-2C-8G" }
  }

  assert {
    condition     = module.host["main"].instance_type == "POP2-2C-8G"
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

run "rejects_an_arm_vm_override" {
  command = plan

  variables {
    instance_type_overrides = { platform = "COPARM1-4C-16G", mcp = "basic2-a2c-4g" }
  }

  expect_failures = [var.instance_type_overrides]
}

run "accepts_an_x86_vm_override_with_an_x_core_count" {
  command = plan

  variables {
    instance_type_overrides = { platform = "BASIC3-X4C-8G" }
  }

  assert {
    condition     = module.host["platform"].instance_type == "BASIC3-X4C-8G"
    error_message = "an x86_64 -X<n>C- type is accepted"
  }
}

run "rejects_an_unknown_override_key" {
  command = plan

  variables {
    instance_type_overrides = { airflow = "DEV1-S" }
  }

  expect_failures = [var.instance_type_overrides]
}

run "a_type_change_replaces_the_server" {
  command = plan

  # The provider migrates a server on a type change unless told to replace it, and a DEV1
  # server's local volume cannot migrate to a POP2 type, which boots from Block Storage.
  assert {
    condition     = alltrue([for h in module.host : h.settings.replace_on_type_change])
    error_message = "every host replaces its server on a type change rather than migrating it"
  }
}
