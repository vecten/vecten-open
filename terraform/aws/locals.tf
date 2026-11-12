locals {
  # The module is sourced by tag and cannot read versions.toml; a test holds these equal to it.
  compose_plugin_version = "5.5.1"
  buildx_plugin_version  = "0.37.2"
  certbot_image          = "certbot/certbot:v5.8.0"

  sizing = jsondecode(file("${path.module}/sizing.json"))
  tier   = local.sizing.tiers[var.size]

  # Must stay identical to vecten.deploy.names.name_prefix: destroy finds the RDS instances
  # by rds_identifier, which builds the same string in Python.
  env_slug     = trim(replace(lower(var.environment), "/[^a-z0-9]+/", "-"), "-")
  project_slug = trim(replace(lower(var.project_name), "/[^a-z0-9]+/", "-"), "-")
  project_part = trim(substr(local.project_slug, 0, 44 - length(local.env_slug)), "-")
  name_prefix  = "vecten-${local.project_part}-${local.env_slug}"

  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  # bootstrap creates this policy under the same name (vecten.deploy.names.boundary_policy_name);
  # the deploy role may create and edit host roles only when they carry it.
  permissions_boundary_arn = "arn:aws:iam::${local.account_id}:policy/${local.name_prefix}-boundary"

  # S3 names are global and at most 63 characters: the account id stays whole, the project shrinks.
  artifacts_tail         = "-${local.env_slug}-artifacts-${local.account_id}"
  artifacts_room         = 63 - length("vecten-") - length(local.artifacts_tail)
  artifacts_project_part = trim(substr(local.project_slug, 0, max(0, min(local.artifacts_room, 44 - length(local.env_slug)))), "-")
  artifacts_bucket       = "vecten-${local.artifacts_project_part}${local.artifacts_tail}"
  artifacts_bucket_arn   = "arn:aws:s3:::${local.artifacts_bucket}"

  secret_prefix   = "vecten/${var.project_name}/${var.environment}/"
  secret_arn_base = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:${local.secret_prefix}"

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
  service_hostname_of = {
    for label, c in var.service_hostnames : (label == "@" ? var.domain : "${label}.${var.domain}") => local.host_of[c]
  }
  host_service_hostnames = {
    for h in keys(local.host_components) : h => sort([for name, host in local.service_hostname_of : name if host == h])
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
  # A state instance without the warehouse database holds Airflow metadata and the MCP audit,
  # which grow slowly; the ceiling only keeps auto-grow on.
  state_max_storage_gb = 100
  # The instance holding the warehouse database (under minimal, the one instance) serves the
  # loaders and every person's MCP pool, so it takes the class the tier's connection budget
  # was sized for.
  instances = {
    for key, dbs in local.instance_databases : key => {
      databases = dbs
      instance_class = lookup(
        var.instance_type_overrides,
        key,
        contains(dbs, "warehouse") ? local.tier.warehouse_class : local.sizing.state_class,
      )
      allocated_storage     = contains(dbs, "warehouse") ? local.tier.warehouse_storage_gb : local.sizing.state_storage_gb
      max_allocated_storage = contains(dbs, "warehouse") ? local.tier.warehouse_max_storage_gb : local.state_max_storage_gb
      log_line_prefix       = contains(dbs, "warehouse") ? var.log_line_prefix : null
    }
  }
}
