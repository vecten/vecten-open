output "name" {
  description = "Server name."
  value       = scaleway_instance_server.this.name
}

output "instance_id" {
  description = "Server UUID without its zone, the ID the Instances API takes."
  value       = element(split("/", scaleway_instance_server.this.id), length(split("/", scaleway_instance_server.this.id)) - 1)
}

output "public_ip" {
  description = "The flexible IP the hostnames resolve to."
  value       = scaleway_instance_ip.this.address
}

output "instance_type" {
  description = "Scaleway commercial type of the server."
  value       = scaleway_instance_server.this.type
}

output "settings" {
  description = "Configuration the module tests assert: project, zone, image, root volume, network, security group, user data, watcher config."
  value = {
    project_id             = scaleway_instance_server.this.project_id
    replace_on_type_change = scaleway_instance_server.this.replace_on_type_change
    zone                   = scaleway_instance_server.this.zone
    image                  = scaleway_instance_server.this.image
    root_volume = {
      volume_type           = scaleway_instance_server.this.root_volume[0].volume_type
      size_in_gb            = scaleway_instance_server.this.root_volume[0].size_in_gb
      delete_on_termination = scaleway_instance_server.this.root_volume[0].delete_on_termination
    }
    private_network_ids  = [for n in scaleway_instance_server.this.private_network : n.pn_id]
    flexible_ip_attached = scaleway_instance_server.this.ip_id == scaleway_instance_ip.this.id
    security_group = {
      inbound_default_policy  = scaleway_instance_security_group.this.inbound_default_policy
      outbound_default_policy = scaleway_instance_security_group.this.outbound_default_policy
      inbound_rules = [
        for r in scaleway_instance_security_group.this.inbound_rule : {
          action   = r.action
          protocol = r.protocol
          port     = r.port
          ip_range = r.ip_range
        }
      ]
    }
    user_data      = local.user_data
    user_data_keys = { for k, v in scaleway_instance_server.this.user_data : k => length(v) }
    watch_config   = var.watch_config
  }
}
