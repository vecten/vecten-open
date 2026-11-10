terraform {
  required_version = "~> 1.16.0"

  required_providers {
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "~> 2.22.0"
    }
  }
}
