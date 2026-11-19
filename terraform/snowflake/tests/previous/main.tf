# The ANALYST grant resources as release 0.1.1 of this module declared them, at the same
# addresses, so a run of the current module can plan over the state they leave.
terraform {
  required_providers {
    snowflake = {
      source = "snowflakedb/snowflake"
    }
  }
}

resource "snowflake_grant_privileges_to_account_role" "analyst_schema" {
  for_each = toset(["CORE", "MARTS"])

  account_role_name = "ANALYST"
  privileges        = ["USAGE"]

  on_schema {
    schema_name = "\"ACME\".\"${each.key}\""
  }
}

resource "snowflake_grant_privileges_to_account_role" "analyst_future" {
  for_each = {
    for pair in setproduct(["CORE", "MARTS"], ["TABLES", "VIEWS"]) :
    "${pair[0]}/${pair[1]}" => { schema = pair[0], objects = pair[1] }
  }

  account_role_name = "ANALYST"
  privileges        = ["SELECT"]

  on_schema_object {
    future {
      object_type_plural = each.value.objects
      in_schema          = "\"ACME\".\"${each.value.schema}\""
    }
  }
}
