output "name" {
  description = "Database instance name."
  value       = scaleway_rdb_instance.this.name
}

output "id" {
  description = "Database instance ID, which changes when the instance is replaced."
  value       = scaleway_rdb_instance.this.id
}

output "address" {
  description = "Private network endpoint address the hosts connect to."
  value       = scaleway_rdb_instance.this.private_network[0].ip
}

output "port" {
  description = "Private network endpoint port."
  value       = scaleway_rdb_instance.this.private_network[0].port
}

output "user_name" {
  description = "The instance's admin user."
  value       = scaleway_rdb_instance.this.user_name
}

output "settings" {
  description = "Configuration the module tests assert."
  value = {
    project_id                  = scaleway_rdb_instance.this.project_id
    region                      = scaleway_rdb_instance.this.region
    engine                      = scaleway_rdb_instance.this.engine
    node_type                   = scaleway_rdb_instance.this.node_type
    is_ha_cluster               = scaleway_rdb_instance.this.is_ha_cluster
    volume_type                 = scaleway_rdb_instance.this.volume_type
    volume_size_in_gb           = scaleway_rdb_instance.this.volume_size_in_gb
    encryption_at_rest          = scaleway_rdb_instance.this.encryption_at_rest
    disable_backup              = scaleway_rdb_instance.this.disable_backup
    backup_schedule_frequency   = scaleway_rdb_instance.this.backup_schedule_frequency
    backup_schedule_retention   = scaleway_rdb_instance.this.backup_schedule_retention
    user_name                   = scaleway_rdb_instance.this.user_name
    password_is_null            = nonsensitive(scaleway_rdb_instance.this.password == null)
    password_wo_version         = scaleway_rdb_instance.this.password_wo_version
    engine_settings             = scaleway_rdb_instance.this.settings
    private_network_count       = length(scaleway_rdb_instance.this.private_network)
    private_network_enable_ipam = scaleway_rdb_instance.this.private_network[0].enable_ipam
    public_endpoint             = length(scaleway_rdb_instance.this.load_balancer) > 0
    acl_ips                     = local.public ? [for r in scaleway_rdb_acl.public[0].acl_rules : r.ip] : null
  }
}
