# A thin root over the earlier release's ANALYST grants, so state holds them at
# module.snowflake addresses as a deployed environment's does.
terraform {
  required_providers {
    snowflake = {
      source = "snowflakedb/snowflake"
    }
  }
}

module "snowflake" {
  source = "../../previous"
}
