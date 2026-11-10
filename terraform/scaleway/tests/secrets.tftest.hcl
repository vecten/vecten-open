mock_provider "scaleway" {
  # The provider checks that these IDs are zoned or regional UUIDs when an apply run uses them.
  mock_resource "scaleway_vpc_private_network" {
    defaults = { id = "fr-par/0d4c7a52-1b2e-4f3a-9c8d-112233445566" }
  }
  mock_resource "scaleway_instance_ip" {
    defaults = { id = "fr-par-1/1e5d8b63-2c3f-4a4b-8d9e-223344556677" }
  }
  mock_resource "scaleway_instance_security_group" {
    defaults = { id = "fr-par-1/2f6e9c74-3d4a-4b5c-9eaf-334455667788" }
  }
  mock_resource "scaleway_rdb_instance" {
    defaults = { id = "fr-par/3a7fad85-4e5b-4c6d-8fb0-445566778899" }
  }
}

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

run "shells_for_every_generated_and_airflow_secret" {
  command = plan

  assert {
    condition = toset(keys(scaleway_secret.shell)) == toset([
      "platform/AIRFLOW_DB_PASSWORD",
      "platform/LOADER_PASSWORD",
      "mcp/MCP_STATE_DB_PASSWORD",
      "mcp/MCP_SIGNING_KEY",
      "mcp/MCP_PROVISIONER_PASSWORD",
      "mcp/MCP_IMPERSONATOR_PASSWORD",
      "platform/AIRFLOW_FERNET_KEY",
      "platform/AIRFLOW_API_SECRET_KEY",
      "platform/AIRFLOW_JWT_SECRET",
      "platform/AIRFLOW_ADMIN_PASSWORD",
      "platform/AIRFLOW_BASIC_AUTH_PASSWORD",
    ])
    error_message = "Terraform creates a shell for each name a VM writes, and no other"
  }
  assert {
    condition     = alltrue([for k, s in scaleway_secret.shell : "${s.path}/${s.name}" == "/vecten/acme/prod/${k}"])
    error_message = "a shell NAME of scope s sits at path /vecten/<project>/<env>/<s> with name NAME"
  }
  assert {
    condition     = alltrue([for s in scaleway_secret.shell : s.project_id == "1a2b3c4d-2222-4333-8444-555555555555" && s.region == "fr-par"])
    error_message = "secrets live in the resources project and region"
  }
}

run "only_the_admin_passwords_get_a_version_and_it_is_write_only" {
  command = plan

  assert {
    condition     = keys(scaleway_secret.admin) == ["state", "warehouse"]
    error_message = "each instance gets an admin password secret"
  }
  assert {
    condition     = scaleway_secret.admin["state"].name == "SETUP_STATE_ADMIN_PASSWORD" && scaleway_secret.admin["warehouse"].name == "SETUP_WAREHOUSE_ADMIN_PASSWORD"
    error_message = "admin passwords are SETUP_<KEY>_ADMIN_PASSWORD"
  }
  assert {
    condition     = alltrue([for s in scaleway_secret.admin : s.path == "/vecten/acme/prod/setup"])
    error_message = "admin passwords sit in the setup scope"
  }
  assert {
    condition     = keys(scaleway_secret_version.admin) == keys(scaleway_secret.admin)
    error_message = "every admin password, and nothing else, gets a version from Terraform"
  }
  assert {
    condition     = alltrue([for v in scaleway_secret_version.admin : v.data == null])
    error_message = "no version sets the stored data attribute"
  }
  assert {
    condition     = alltrue([for v in scaleway_secret_version.admin : v.data_wo_version == 1])
    error_message = "the write-only data carries version 1 by default"
  }
}

run "password_version_moves_the_secret_with_the_instance" {
  command = plan

  variables {
    warehouse_admin_password_version = 4
  }

  assert {
    condition     = scaleway_secret_version.admin["warehouse"].data_wo_version == 4 && module.postgres["warehouse"].settings.password_wo_version == 4
    error_message = "the instance and its secret rotate together"
  }
  assert {
    condition     = scaleway_secret_version.admin["state"].data_wo_version == 1
    error_message = "the other instance keeps its version"
  }
}

run "mcp_only_writes_the_state_credential_alone" {
  command = plan

  variables {
    components = ["mcp"]
  }

  assert {
    condition     = keys(scaleway_secret.shell) == ["mcp/MCP_SIGNING_KEY", "mcp/MCP_STATE_DB_PASSWORD"]
    error_message = "MCP only generates the state credential; the provisioner credential is stored by hand"
  }
  assert {
    condition     = keys(scaleway_secret.admin) == ["state"]
    error_message = "MCP only has one instance, so one admin password"
  }
}

run "platform_on_snowflake_has_no_loader_shell" {
  command = plan

  variables {
    components = ["platform"]
    warehouse  = "snowflake"
  }

  assert {
    condition = toset(keys(scaleway_secret.shell)) == toset([
      "platform/AIRFLOW_DB_PASSWORD",
      "platform/AIRFLOW_FERNET_KEY",
      "platform/AIRFLOW_API_SECRET_KEY",
      "platform/AIRFLOW_JWT_SECRET",
      "platform/AIRFLOW_ADMIN_PASSWORD",
      "platform/AIRFLOW_BASIC_AUTH_PASSWORD",
    ])
    error_message = "a Snowflake warehouse has no Postgres loader credential"
  }
  assert {
    condition     = keys(scaleway_secret.admin) == ["state"]
    error_message = "a Snowflake project has no warehouse admin password"
  }
}

run "minimal_has_one_admin_password" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition     = keys(scaleway_secret.admin) == ["state"] && keys(scaleway_secret_version.admin) == ["state"]
    error_message = "the one minimal instance has one admin password"
  }
}

run "master_secret_is_the_admin_secret_id" {
  command = apply

  override_resource {
    target = scaleway_secret.admin
    values = { id = "fr-par/3f1c9a52-7d6e-4b8a-9c21-0e5f4d3b2a10" }
  }

  assert {
    condition     = alltrue([for p in output.postgres : p.master_secret == "3f1c9a52-7d6e-4b8a-9c21-0e5f4d3b2a10"])
    error_message = "master_secret is the ID of the instance's admin password secret, without its region"
  }
  assert {
    condition     = alltrue([for k, d in terraform_data.admin_instance : d.triggers_replace == module.postgres[k].id])
    error_message = "each admin secret version is tied to its instance's ID, so replacing the instance rewrites the secret"
  }
  assert {
    condition     = keys(terraform_data.admin_instance) == keys(scaleway_secret_version.admin)
    error_message = "every admin secret version has a tracker"
  }
}

run "replacing_an_instance_replaces_its_admin_secret_version" {
  command = apply

  plan_options {
    replace = [module.postgres["state"].scaleway_rdb_instance.this]
  }

  override_resource {
    target = module.postgres["state"].scaleway_rdb_instance.this
    values = { id = "fr-par/9c0d1e2f-3a4b-4c5d-8e6f-7a8b9c0d1e2f" }
  }

  assert {
    condition     = terraform_data.admin_instance["state"].triggers_replace == "fr-par/9c0d1e2f-3a4b-4c5d-8e6f-7a8b9c0d1e2f"
    error_message = "the replaced instance's new ID reaches the tracker, which replaces the secret version"
  }
  assert {
    condition     = terraform_data.admin_instance["warehouse"].triggers_replace == "fr-par/3a7fad85-4e5b-4c6d-8fb0-445566778899"
    error_message = "the other instance and its secret stay as they are"
  }
}
