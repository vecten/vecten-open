module "postgres" {
  source   = "./modules/postgres"
  for_each = local.instances

  identifier            = "${local.name_prefix}-${each.key}"
  environment           = var.environment
  destroying            = var.destroying
  instance_class        = each.value.instance_class
  allocated_storage     = each.value.allocated_storage
  max_allocated_storage = each.value.max_allocated_storage
  private_subnet_ids    = aws_subnet.private[*].id
  public_subnet_ids     = aws_subnet.public[*].id
  security_group_id     = aws_security_group.database.id
  public_cidrs          = var.database_public_cidrs
  log_line_prefix       = each.value.log_line_prefix
}
