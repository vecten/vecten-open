module "postgres" {
  source   = "./modules/postgres"
  for_each = local.instances

  name                   = "${local.name_prefix}-${each.key}"
  project_id             = var.project_id
  region                 = var.region
  node_type              = each.value.node_type
  volume_type            = local.sizing.volume_type
  volume_size_gb         = each.value.volume_size_gb
  private_network_id     = scaleway_vpc_private_network.this.id
  public_cidrs           = var.database_public_cidrs
  engine_settings        = {}
  admin_password         = ephemeral.random_password.admin[each.key].result
  admin_password_version = each.value.password_version
  tags                   = concat(local.tags, ["vecten:instance=${each.key}"])
}
