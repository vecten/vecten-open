locals {
  # The module is sourced by tag and cannot read versions.toml; a test holds these equal to it.
  compose_plugin_version = "5.5.1"
  buildx_plugin_version  = "0.37.2"
  certbot_image          = "certbot/certbot:v5.8.0"

  sizing = jsondecode(file("${path.module}/sizing.json"))
  tier   = local.sizing.tiers[var.size]

  zone           = "${var.region}-1"
  dns_project_id = coalesce(var.dns_project_id, var.project_id)
  tags           = ["vecten", "vecten:project=${var.project_name}", "vecten:env=${var.environment}"]

  # Must stay identical to vecten.deploy.names.name_prefix: destroy snapshots the database
  # instances it finds by this name.
  env_slug     = trim(replace(lower(var.environment), "/[^a-z0-9]+/", "-"), "-")
  project_slug = trim(replace(lower(var.project_name), "/[^a-z0-9]+/", "-"), "-")
  project_part = trim(substr(local.project_slug, 0, 44 - length(local.env_slug)), "-")
  name_prefix  = "vecten-${local.project_part}-${local.env_slug}"

  # Must stay identical to vecten.deploy.names.scaleway_bucket_name.
  artifacts_tail         = "-${local.env_slug}-artifacts-${substr(lower(var.project_id), 0, 8)}"
  artifacts_room         = 63 - length("vecten-") - length(local.artifacts_tail)
  artifacts_project_part = trim(substr(local.project_slug, 0, max(0, min(local.artifacts_room, 44 - length(local.env_slug)))), "-")
  artifacts_bucket       = "vecten-${local.artifacts_project_part}${local.artifacts_tail}"

  # A logical secret NAME of scope s is the secret named NAME at path <secret_root>/<s>, the
  # layout vecten_shared.provider.scaleway reads and the host IAM conditions match.
  secret_root   = "/vecten/${var.project_name}/${var.environment}"
  secret_prefix = "${local.secret_root}/"

  components   = [for c in ["platform", "mcp"] : c if contains(var.components, c)]
  has_platform = contains(local.components, "platform")
  has_mcp      = contains(local.components, "mcp")

  hostname_of = {
    platform = "airflow.${var.domain}"
    mcp      = "mcp.${var.domain}"
  }

  host_components = var.profile == "minimal" ? { main = local.components } : { for c in local.components : c => [c] }
  host_of         = { for c in local.components : c => (var.profile == "minimal" ? "main" : c) }
  host_hostnames = {
    for h, cs in local.host_components : h => [for c in cs : local.hostname_of[c]]
  }
  setup_host = var.profile == "minimal" ? "main" : (local.has_mcp ? "mcp" : "platform")

  default_host_types = {
    platform = local.sizing.tiers[var.platform_size].vm
    mcp      = local.tier.vm
    main     = local.sizing.minimal_vm
  }
  host_instance_types = {
    for h in keys(local.host_components) : h => lookup(var.instance_type_overrides, h, local.default_host_types[h])
  }

  state_databases = concat(
    local.has_platform ? ["airflow"] : [],
    local.has_mcp ? ["mcp_state"] : [],
  )
  # An MCP-only project always sits over an existing warehouse, so only the platform brings one.
  managed_warehouse = local.has_platform && var.warehouse == "postgres"
  instance_databases = var.profile == "minimal" ? {
    state = concat(local.state_databases, local.managed_warehouse ? ["warehouse"] : [])
    } : merge(
    { state = local.state_databases },
    local.managed_warehouse ? { warehouse = ["warehouse"] } : {},
  )
  # The instance holding the warehouse database serves the loaders and every person's MCP
  # pool, so it takes the class the tier's connection budget was sized for; under minimal it
  # also holds the state databases, which minimal_class leaves room for. An SBS volume has a
  # fixed size and grows only by an edit, so the volume is the table's figure.
  instances = {
    for key, dbs in local.instance_databases : key => {
      databases = dbs
      node_type = lookup(
        var.instance_type_overrides,
        key,
        contains(dbs, "warehouse") ? (var.profile == "minimal" ? local.tier.minimal_class : local.tier.warehouse_class) : local.sizing.state_class,
      )
      volume_size_gb   = contains(dbs, "warehouse") ? local.tier.warehouse_storage_gb : local.sizing.state_storage_gb
      password_version = key == "warehouse" ? var.warehouse_admin_password_version : var.state_admin_password_version
    }
  }
}
