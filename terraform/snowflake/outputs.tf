output "database" {
  description = "Database holding the convention's schemas."
  value       = snowflake_database.this.name
}

output "warehouse" {
  description = "Warehouse every role computes on."
  value       = snowflake_warehouse.this.name
}

output "roles" {
  description = "Account roles the module created."
  value       = sort([for role in snowflake_account_role.this : role.name])
}
