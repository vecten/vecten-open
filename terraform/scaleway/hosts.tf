module "host" {
  source   = "./modules/host"
  for_each = local.host_components

  name                   = "${local.name_prefix}-${each.key}"
  project_id             = var.project_id
  zone                   = local.zone
  instance_type          = local.host_instance_types[each.key]
  private_network_id     = scaleway_vpc_private_network.this.id
  tags                   = concat(local.tags, ["vecten:host=${each.key}"])
  compose_plugin_version = local.compose_plugin_version
  buildx_plugin_version  = local.buildx_plugin_version
  certbot_image          = local.certbot_image
  watch_config = {
    host          = each.key
    components    = tolist(each.value)
    bucket        = local.artifacts_bucket
    region        = var.region
    project_id    = var.project_id
    secret_prefix = local.secret_prefix
  }
}
