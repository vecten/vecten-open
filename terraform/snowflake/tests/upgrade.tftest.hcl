# An environment deployed by an earlier release holds ANALYST's reads in state. The upgraded
# module forgets them without revoking: the `terraform` tier reads this file's verbose plan for
# "will not be destroyed" and no destroy.
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

run "the_previous_release_granted_analyst_its_reads" {
  state_key = "upgrade"

  module {
    source = "./tests/previous"
  }

  assert {
    condition     = length(snowflake_grant_privileges_to_account_role.analyst_schema) == 2 && length(snowflake_grant_privileges_to_account_role.analyst_future) == 4
    error_message = "the earlier state holds USAGE on core and marts and four future grants"
  }
}

run "the_upgrade_forgets_them_without_revoking" {
  state_key = "upgrade"
  command   = plan

  assert {
    condition     = length(snowflake_grant_privileges_to_account_role.analyst_default_schema) == 0 && length(snowflake_grant_privileges_to_account_role.analyst_default_future) == 0
    error_message = "with the MCP no ANALYST grant takes their place"
  }
}
