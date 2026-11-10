mock_provider "snowflake" {
  override_data {
    target = data.snowflake_current_role.deployer
    values = { name = "VECTEN_DEPLOY_ROLE" }
  }
}

variables {
  environment       = "prod"
  project_name      = "acme"
  size              = "small"
  components        = ["platform", "mcp"]
  account           = "ACME-PROD"
  database_name     = "ACME"
  warehouse_name    = "ACME_PROD_WH"
  loader_public_key = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A\nMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo\n-----END PUBLIC KEY-----\n"
}

run "both_components_build_three_roles_four_schemas_and_a_suspending_warehouse" {
  command = plan

  assert {
    condition     = toset(keys(snowflake_account_role.this)) == toset(["ANALYST", "LOADER", "PROVISIONER"])
    error_message = "with the MCP the roles are ANALYST, LOADER and PROVISIONER"
  }
  assert {
    condition     = toset([for s in snowflake_schema.convention : s.name]) == toset(["STAGING", "SNAPSHOTS", "CORE", "MARTS"])
    error_message = "the schemas are the convention's four"
  }
  assert {
    condition     = alltrue([for s in snowflake_schema.convention : s.database == "ACME"])
    error_message = "every schema sits in the project's database"
  }
  assert {
    condition     = snowflake_database.this.name == "ACME"
    error_message = "the database takes database_name"
  }
  assert {
    condition     = snowflake_warehouse.this.name == "ACME_PROD_WH"
    error_message = "the warehouse takes warehouse_name"
  }
  assert {
    condition     = snowflake_warehouse.this.auto_suspend == 60
    error_message = "the warehouse suspends after 60 seconds"
  }
  assert {
    condition     = snowflake_warehouse.this.auto_resume == "true" && snowflake_warehouse.this.initially_suspended == true
    error_message = "the warehouse starts suspended and resumes on demand"
  }
  assert {
    condition     = snowflake_warehouse.this.warehouse_size == "XSMALL"
    error_message = "small is an XSMALL warehouse"
  }
  assert {
    condition     = output.database == "ACME" && output.warehouse == "ACME_PROD_WH"
    error_message = "the outputs name the database and the warehouse"
  }
  assert {
    condition     = toset(output.roles) == toset(["ANALYST", "LOADER", "PROVISIONER"])
    error_message = "the roles output lists every role"
  }
}

run "the_loader_is_a_key_pair_service_user" {
  command = plan

  assert {
    condition     = snowflake_service_user.loader.name == "LOADER" && snowflake_service_user.loader.default_role == "LOADER"
    error_message = "the loader is the LOADER service user acting as LOADER"
  }
  assert {
    condition     = snowflake_service_user.loader.default_warehouse == "ACME_PROD_WH"
    error_message = "the loader uses the environment's warehouse"
  }
  assert {
    condition     = snowflake_service_user.loader.rsa_public_key == "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo"
    error_message = "Snowflake takes the public key on one line without its PEM header and trailer"
  }
  assert {
    condition     = snowflake_grant_account_role.loader_user.role_name == "LOADER" && snowflake_grant_account_role.loader_user.user_name == "LOADER"
    error_message = "the LOADER user holds the LOADER role"
  }
}

run "grants_follow_the_convention" {
  command = plan

  assert {
    condition     = toset([for g in snowflake_grant_ownership.schema : g.account_role_name]) == toset(["LOADER"])
    error_message = "LOADER owns every convention schema"
  }
  assert {
    condition     = toset([for g in snowflake_grant_ownership.schema : g.on[0].object_name]) == toset(["\"ACME\".\"STAGING\"", "\"ACME\".\"SNAPSHOTS\"", "\"ACME\".\"CORE\"", "\"ACME\".\"MARTS\""])
    error_message = "ownership covers the four schemas"
  }
  assert {
    condition     = toset(keys(snowflake_grant_account_role.sysadmin)) == toset(["ANALYST", "LOADER", "PROVISIONER"]) && alltrue([for g in snowflake_grant_account_role.sysadmin : g.parent_role_name == "SYSADMIN"])
    error_message = "every role sits under SYSADMIN, through which the deploy user still reads the schemas LOADER owns"
  }
  assert {
    condition     = toset(snowflake_grant_privileges_to_account_role.database["LOADER"].privileges) == toset(["USAGE", "CREATE SCHEMA"])
    error_message = "LOADER may create the raw schemas dlt loads into"
  }
  assert {
    condition     = toset(keys(snowflake_grant_privileges_to_account_role.warehouse)) == toset(["ANALYST", "LOADER", "PROVISIONER"])
    error_message = "every role uses the warehouse"
  }
  assert {
    condition     = alltrue([for g in snowflake_grant_privileges_to_account_role.warehouse : toset(g.privileges) == toset(["USAGE"]) && g.on_account_object[0].object_type == "WAREHOUSE"])
    error_message = "warehouse grants are USAGE on the warehouse"
  }
  assert {
    condition     = toset(snowflake_grant_privileges_to_account_role.database["ANALYST"].privileges) == toset(["USAGE"]) && toset(snowflake_grant_privileges_to_account_role.database["PROVISIONER"].privileges) == toset(["USAGE"])
    error_message = "ANALYST and PROVISIONER use the database"
  }
  assert {
    condition     = toset(keys(snowflake_grant_privileges_to_account_role.analyst_schema)) == toset(["CORE", "MARTS"])
    error_message = "ANALYST uses core and marts only"
  }
  assert {
    condition     = toset([for g in snowflake_grant_privileges_to_account_role.analyst_future : "${g.on_schema_object[0].future[0].object_type_plural} ${g.on_schema_object[0].future[0].in_schema}"]) == toset(["TABLES \"ACME\".\"CORE\"", "TABLES \"ACME\".\"MARTS\"", "VIEWS \"ACME\".\"CORE\"", "VIEWS \"ACME\".\"MARTS\""])
    error_message = "ANALYST selects every future table and view in core and marts"
  }
  assert {
    condition     = alltrue([for g in snowflake_grant_privileges_to_account_role.analyst_future : g.account_role_name == "ANALYST" && toset(g.privileges) == toset(["SELECT"])])
    error_message = "future grants are SELECT to ANALYST"
  }
}

run "every_role_sits_under_the_role_terraform_runs_as" {
  command = plan

  assert {
    condition     = toset(keys(snowflake_grant_account_role.deployer)) == toset(["ANALYST", "LOADER", "PROVISIONER"])
    error_message = "every role is granted to the deploy role, which reads the schemas LOADER owns through it"
  }
  assert {
    condition     = alltrue([for g in snowflake_grant_account_role.deployer : g.parent_role_name == "VECTEN_DEPLOY_ROLE"])
    error_message = "the parent is the session's primary role"
  }
}

run "a_deploy_role_of_sysadmin_takes_no_second_grant" {
  command = plan

  override_data {
    target = data.snowflake_current_role.deployer
    values = { name = "SYSADMIN" }
  }

  assert {
    condition     = length(snowflake_grant_account_role.deployer) == 0 && length(snowflake_grant_account_role.sysadmin) == 3
    error_message = "a deploy role that is SYSADMIN holds the roles through the SYSADMIN grants alone"
  }
}

run "the_whole_graph_applies" {
  command = apply

  assert {
    condition     = length(snowflake_grant_ownership.schema) == 4 && output.database == "ACME"
    error_message = "every resource applies against the mocked provider"
  }
}

run "without_the_mcp_there_is_no_provisioner" {
  command = plan

  variables {
    components = ["platform"]
  }

  assert {
    condition     = toset(keys(snowflake_account_role.this)) == toset(["ANALYST", "LOADER"])
    error_message = "without the MCP no PROVISIONER role exists"
  }
  assert {
    condition     = !contains(keys(snowflake_grant_privileges_to_account_role.warehouse), "PROVISIONER") && !contains(keys(snowflake_grant_privileges_to_account_role.database), "PROVISIONER")
    error_message = "without the MCP nothing is granted to PROVISIONER"
  }
}

run "large_is_a_medium_warehouse" {
  command = plan

  variables {
    size = "large"
  }

  assert {
    condition     = snowflake_warehouse.this.warehouse_size == "MEDIUM"
    error_message = "large is a MEDIUM warehouse"
  }
}

run "medium_is_a_small_warehouse" {
  command = plan

  variables {
    size = "medium"
  }

  assert {
    condition     = snowflake_warehouse.this.warehouse_size == "SMALL"
    error_message = "medium is a SMALL warehouse"
  }
}

run "an_explicit_warehouse_size_wins" {
  command = plan

  variables {
    size           = "small"
    warehouse_size = "LARGE"
  }

  assert {
    condition     = snowflake_warehouse.this.warehouse_size == "LARGE"
    error_message = "warehouse_size, which deploy passes from the package's table, wins over the module's own lookup"
  }
}

run "an_account_without_its_organisation_is_refused" {
  command = plan

  variables {
    account = "xy12345"
  }

  expect_failures = [var.account]
}

run "an_unknown_size_is_refused" {
  command = plan

  variables {
    size = "huge"
  }

  expect_failures = [var.size]
}

run "a_private_key_is_refused" {
  command = plan

  variables {
    loader_public_key = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n"
  }

  expect_failures = [var.loader_public_key]
}

run "the_platform_is_required" {
  command = plan

  variables {
    components = ["mcp"]
  }

  expect_failures = [var.components]
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
