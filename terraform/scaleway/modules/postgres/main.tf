locals {
  public = length(var.public_cidrs) > 0
}

# RDB creates no endpoint beyond the ones configured, so the instance is private until
# public_cidrs adds the load-balancer endpoint and its ACL. The provider suppresses the diff
# when the block goes away, so emptying public_cidrs drops the ACL but leaves the endpoint;
# vecten deploy deletes it through the RDB API. The admin password arrives write-only and
# never reaches state.
resource "scaleway_rdb_instance" "this" {
  name          = var.name
  project_id    = var.project_id
  region        = var.region
  engine        = "PostgreSQL-17"
  node_type     = var.node_type
  is_ha_cluster = false

  volume_type       = var.volume_type
  volume_size_in_gb = var.volume_size_gb

  encryption_at_rest = true

  disable_backup            = false
  backup_schedule_frequency = 24
  backup_schedule_retention = 7

  user_name           = "vecten_admin"
  password_wo         = var.admin_password
  password_wo_version = var.admin_password_version

  settings = var.engine_settings
  tags     = var.tags

  # IPAM registers the endpoint, so it gets an address without a static ip_net.
  private_network {
    pn_id       = var.private_network_id
    enable_ipam = true
  }

  dynamic "load_balancer" {
    for_each = local.public ? [1] : []

    content {}
  }
}

resource "scaleway_rdb_acl" "public" {
  count = local.public ? 1 : 0

  instance_id = scaleway_rdb_instance.this.id
  region      = var.region

  dynamic "acl_rules" {
    for_each = var.public_cidrs

    content {
      ip          = acl_rules.value
      description = "vecten: database_public_cidrs"
    }
  }
}
