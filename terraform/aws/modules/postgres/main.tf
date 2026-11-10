locals {
  public = length(var.public_cidrs) > 0
  parameters = var.log_line_prefix == null ? {} : {
    log_line_prefix = var.log_line_prefix
  }
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.identifier}-pg17"
  family = "postgres17"

  dynamic "parameter" {
    for_each = local.parameters

    content {
      name         = parameter.key
      value        = parameter.value
      apply_method = "pending-reboot"
    }
  }
}

resource "aws_db_subnet_group" "this" {
  name       = var.identifier
  subnet_ids = local.public ? var.public_subnet_ids : var.private_subnet_ids
}

# The master password lives in a Secrets Manager secret RDS creates and rotates, so no
# credential reaches Terraform state; the setup host reads it to run the setup SQL.
resource "aws_db_instance" "this" {
  identifier     = var.identifier
  engine         = "postgres"
  engine_version = "17"
  instance_class = var.instance_class
  port           = 5432

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  username                    = "vecten_master"
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = local.public

  backup_retention_period = 7
  copy_tags_to_snapshot   = true
  # vecten destroy takes and names the final snapshot itself before Terraform runs.
  skip_final_snapshot = true
  deletion_protection = var.environment == "prod" && !var.destroying
  apply_immediately   = true
}
