locals {
  warehouse_size = coalesce(var.warehouse_size, jsondecode(file("${path.module}/sizing.json"))[var.size])
  comment        = "vecten ${var.project_name} ${var.environment} in ${var.account}"
  deployer_role  = upper(data.snowflake_current_role.deployer.name)

  # Names are quoted by the provider, so they are upper case to match the unquoted identifiers
  # dbt and dlt use.
  schemas         = toset(["STAGING", "SNAPSHOTS", "CORE", "MARTS"])
  analyst_schemas = toset(["CORE", "MARTS"])
  with_mcp        = contains(var.components, "mcp")
  roles           = concat(["ANALYST", "LOADER"], local.with_mcp ? ["PROVISIONER"] : [])
  database_privileges = merge(
    { ANALYST = ["USAGE"], LOADER = ["USAGE", "CREATE SCHEMA"] },
    local.with_mcp ? { PROVISIONER = ["USAGE"] } : {},
  )
  # With the MCP, its state database is the only owner of ANALYST's reads, so a revoke made
  # there holds; without it, nothing else grants them.
  analyst_default_schemas = local.with_mcp ? toset([]) : local.analyst_schemas
  analyst_default_future = {
    for pair in setproduct(local.analyst_default_schemas, ["TABLES", "VIEWS"]) :
    "${pair[0]}/${pair[1]}" => { schema = pair[0], objects = pair[1] }
  }
}

resource "snowflake_database" "this" {
  name                           = var.database_name
  comment                        = local.comment
  drop_public_schema_on_creation = true
}

resource "snowflake_schema" "convention" {
  for_each = local.schemas

  database = snowflake_database.this.name
  name     = each.key
  comment  = local.comment
}

resource "snowflake_warehouse" "this" {
  name                = var.warehouse_name
  comment             = local.comment
  warehouse_size      = local.warehouse_size
  auto_suspend        = 60
  auto_resume         = "true"
  initially_suspended = true
}

resource "snowflake_account_role" "this" {
  for_each = toset(local.roles)

  name    = each.key
  comment = local.comment
}

# Reading this data source needs `snowflake_current_role_datasource` among the provider's
# preview features, which the thin root enables.
data "snowflake_current_role" "deployer" {}

# Ownership of the schemas moves to LOADER, so the deploy role keeps reading them on the next
# plan only through a role it holds: every role created here is granted to SYSADMIN, as
# Snowflake's role hierarchy expects, and to the role Terraform runs as.
resource "snowflake_grant_account_role" "sysadmin" {
  for_each = snowflake_account_role.this

  role_name        = each.value.name
  parent_role_name = "SYSADMIN"
}

resource "snowflake_grant_account_role" "deployer" {
  # A deploy role that is SYSADMIN already holds every role through the grants above, and a
  # second resource for the same grant would revoke it for both on destroy.
  for_each = local.deployer_role == "SYSADMIN" ? {} : snowflake_account_role.this

  role_name        = each.value.name
  parent_role_name = local.deployer_role
}

resource "snowflake_service_user" "loader" {
  name              = "LOADER"
  comment           = local.comment
  default_role      = snowflake_account_role.this["LOADER"].name
  default_warehouse = snowflake_warehouse.this.name
  # Snowflake takes the key body on one line, without the PEM header and trailer.
  rsa_public_key = join("", [
    for line in split("\n", var.loader_public_key) : trimspace(line)
    if !startswith(trimspace(line), "-----")
  ])
}

resource "snowflake_grant_account_role" "loader_user" {
  role_name = snowflake_account_role.this["LOADER"].name
  user_name = snowflake_service_user.loader.name
}

resource "snowflake_grant_privileges_to_account_role" "warehouse" {
  for_each = snowflake_account_role.this

  account_role_name = each.value.name
  privileges        = ["USAGE"]

  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.this.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "database" {
  for_each = local.database_privileges

  account_role_name = snowflake_account_role.this[each.key].name
  privileges        = each.value

  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.this.name
  }
}

# State written by earlier releases holds ANALYST's reads under these addresses. Forgetting
# them keeps the grants in Snowflake: with the MCP its reconciliation owns them from here, and
# without it the resources below take them over with an idempotent GRANT.
removed {
  from = snowflake_grant_privileges_to_account_role.analyst_schema

  lifecycle {
    destroy = false
  }
}

removed {
  from = snowflake_grant_privileges_to_account_role.analyst_future

  lifecycle {
    destroy = false
  }
}

resource "snowflake_grant_privileges_to_account_role" "analyst_default_schema" {
  for_each = local.analyst_default_schemas

  account_role_name = snowflake_account_role.this["ANALYST"].name
  privileges        = ["USAGE"]

  on_schema {
    schema_name = "\"${snowflake_schema.convention[each.key].database}\".\"${snowflake_schema.convention[each.key].name}\""
  }
}

# Future grants are set before ownership moves to LOADER, whose dbt runs then create the
# tables and views ANALYST reads.
resource "snowflake_grant_privileges_to_account_role" "analyst_default_future" {
  for_each = local.analyst_default_future

  account_role_name = snowflake_account_role.this["ANALYST"].name
  privileges        = ["SELECT"]

  on_schema_object {
    future {
      object_type_plural = each.value.objects
      in_schema          = "\"${snowflake_schema.convention[each.value.schema].database}\".\"${snowflake_schema.convention[each.value.schema].name}\""
    }
  }
}

resource "snowflake_grant_ownership" "schema" {
  for_each = local.schemas

  account_role_name   = snowflake_account_role.this["LOADER"].name
  outbound_privileges = "COPY"

  on {
    object_type = "SCHEMA"
    object_name = "\"${snowflake_schema.convention[each.key].database}\".\"${snowflake_schema.convention[each.key].name}\""
  }

  depends_on = [
    snowflake_grant_privileges_to_account_role.analyst_default_schema,
    snowflake_grant_privileges_to_account_role.analyst_default_future,
    snowflake_grant_account_role.sysadmin,
    snowflake_grant_account_role.deployer,
  ]
}
