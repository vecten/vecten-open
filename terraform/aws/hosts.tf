data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

module "host" {
  source   = "./modules/host"
  for_each = local.host_components

  name                   = "${local.name_prefix}-${each.key}"
  host                   = each.key
  ami_id                 = data.aws_ssm_parameter.al2023.insecure_value
  instance_type          = local.host_instance_types[each.key]
  subnet_id              = aws_subnet.public[0].id
  security_group_ids     = [aws_security_group.hosts.id]
  scope_policy           = local.scope_policies[each.key]
  permissions_boundary   = local.permissions_boundary_arn
  compose_plugin_version = local.compose_plugin_version
  buildx_plugin_version  = local.buildx_plugin_version
  certbot_image          = local.certbot_image
  root_volume_size_gb    = var.root_volume_size_gb

  # The Elastic IP is attached before boot, and AWS refuses it until the gateway exists.
  depends_on = [aws_internet_gateway.this, aws_route_table_association.public]
}
