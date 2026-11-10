locals {
  # The names the VMs write. Each gets an empty secret here, so a VM key only ever adds a
  # version to a secret that exists, and the same rule as the AWS module decides which exist.
  generated_secrets = concat(
    local.has_platform ? ["platform/AIRFLOW_DB_PASSWORD"] : [],
    local.managed_warehouse ? ["platform/LOADER_PASSWORD"] : [],
    local.has_mcp ? ["mcp/MCP_STATE_DB_PASSWORD"] : [],
    local.has_mcp && local.managed_warehouse ? ["mcp/MCP_PROVISIONER_PASSWORD", "mcp/MCP_IMPERSONATOR_PASSWORD"] : [],
  )
  airflow_app_secrets = local.has_platform ? [
    "platform/AIRFLOW_FERNET_KEY",
    "platform/AIRFLOW_API_SECRET_KEY",
    "platform/AIRFLOW_JWT_SECRET",
    "platform/AIRFLOW_ADMIN_PASSWORD",
    "platform/AIRFLOW_BASIC_AUTH_PASSWORD",
  ] : []
  shell_secrets = {
    for n in concat(local.generated_secrets, local.airflow_app_secrets, local.has_mcp ? ["mcp/MCP_SIGNING_KEY"] : []) : n => {
      scope = split("/", n)[0]
      name  = split("/", n)[1]
    }
  }
}

resource "scaleway_secret" "shell" {
  for_each = local.shell_secrets

  name        = each.value.name
  path        = "${local.secret_root}/${each.value.scope}"
  description = "Written by a vecten VM; Terraform creates it without a version."
  project_id  = var.project_id
  region      = var.region
  tags        = local.tags
}

resource "scaleway_secret" "admin" {
  for_each = local.instances

  name        = "SETUP_${upper(each.key)}_ADMIN_PASSWORD"
  path        = "${local.secret_root}/setup"
  description = "Admin credential of the ${each.key} database instance, as username/password JSON."
  project_id  = var.project_id
  region      = var.region
  tags        = local.tags
}

# Ephemeral, so the value lives only in Terraform's memory during one apply: the instance and
# its secret version take it through write-only arguments, and neither state nor plan holds it.
# The 8 to 128 characters with every class present are what the RDB API demands.
ephemeral "random_password" "admin" {
  for_each = local.instances

  length           = 32
  min_upper        = 1
  min_lower        = 1
  min_numeric      = 1
  min_special      = 1
  override_special = "!#%*-_=+."
}

# The JSON matches the RDS master secret, so the setup agent and add-admin read one format.
resource "scaleway_secret_version" "admin" {
  for_each = local.instances

  secret_id       = scaleway_secret.admin[each.key].id
  region          = var.region
  description     = "Admin credential, version ${each.value.password_version}."
  data_wo_version = each.value.password_version
  data_wo = jsonencode({
    "username" = module.postgres[each.key].user_name
    "password" = ephemeral.random_password.admin[each.key].result
  })

  lifecycle {
    replace_triggered_by = [terraform_data.admin_instance[each.key]]
  }
}

# A replaced instance is created with this apply's password, which is new because the
# ephemeral value is drawn again on every apply, while the version number stays put; the
# changed ID replaces the secret version in the same apply, so both hold the same password.
# The instance needs no such trigger: create always sends password_wo.
resource "terraform_data" "admin_instance" {
  for_each = local.instances

  triggers_replace = module.postgres[each.key].id
}
