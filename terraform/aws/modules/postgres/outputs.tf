output "identifier" {
  description = "RDS instance identifier."
  value       = aws_db_instance.this.identifier
}

output "address" {
  description = "Hostname clients connect to."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "Port clients connect to."
  value       = aws_db_instance.this.port
}

output "master_secret_arn" {
  description = "ARN of the RDS-managed master secret; an ARN, never the value."
  value       = one(aws_db_instance.this.master_user_secret[*].secret_arn)

  # An instance restored from a snapshot has no managed secret until it is turned on, and the
  # provider plans the block as empty rather than unknown, so the setup host's policy would
  # name no secret.
  precondition {
    condition     = length(aws_db_instance.this.master_user_secret) == 1
    error_message = "${var.identifier} has no RDS-managed master secret; on a restored instance run `aws rds modify-db-instance --db-instance-identifier ${var.identifier} --manage-master-user-password --apply-immediately` and wait for the secret before Terraform adopts it."
  }
}

output "settings" {
  description = "Configuration the module tests assert."
  value = {
    engine                      = aws_db_instance.this.engine
    engine_version              = aws_db_instance.this.engine_version
    instance_class              = aws_db_instance.this.instance_class
    allocated_storage           = aws_db_instance.this.allocated_storage
    max_allocated_storage       = aws_db_instance.this.max_allocated_storage
    storage_type                = aws_db_instance.this.storage_type
    storage_encrypted           = aws_db_instance.this.storage_encrypted
    username                    = aws_db_instance.this.username
    manage_master_user_password = aws_db_instance.this.manage_master_user_password
    publicly_accessible         = aws_db_instance.this.publicly_accessible
    backup_retention_period     = aws_db_instance.this.backup_retention_period
    deletion_protection         = aws_db_instance.this.deletion_protection
    skip_final_snapshot         = aws_db_instance.this.skip_final_snapshot
    copy_tags_to_snapshot       = aws_db_instance.this.copy_tags_to_snapshot
    subnet_ids                  = aws_db_subnet_group.this.subnet_ids
    parameter_group_family      = aws_db_parameter_group.this.family
    parameters                  = { for p in aws_db_parameter_group.this.parameter : p.name => p.value }
  }
}
