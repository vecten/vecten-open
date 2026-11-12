# A thin root over this module, as `vecten deploy` renders one, for the upgrade plan.
terraform {
  required_providers {
    snowflake = {
      source = "snowflakedb/snowflake"
    }
  }
}

module "snowflake" {
  source = "../../.."

  environment       = "prod"
  project_name      = "acme"
  size              = "small"
  components        = ["platform", "mcp"]
  account           = "ACME-PROD"
  database_name     = "ACME"
  warehouse_name    = "ACME_PROD_WH"
  loader_public_key = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A\nMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo\n-----END PUBLIC KEY-----\n"
}
