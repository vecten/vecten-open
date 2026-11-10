locals {
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    compose_plugin_version = var.compose_plugin_version
    buildx_plugin_version  = var.buildx_plugin_version
    certbot_image          = var.certbot_image
    agent_gzip_base64      = base64gzip(file("${path.module}/agent/apply.py"))
    watch_config           = jsonencode(var.watch_config)
  })

  # DEV1 types boot from their local SSD; POP2, PRO2, PLAY2 and the other current types have
  # no local disk and boot from an SBS volume.
  root_volume_type = startswith(upper(var.instance_type), "DEV1-") ? "l_ssd" : "sbs_volume"
}

resource "scaleway_instance_ip" "this" {
  project_id = var.project_id
  zone       = var.zone
  tags       = var.tags
}

resource "scaleway_instance_security_group" "this" {
  name                    = var.name
  project_id              = var.project_id
  zone                    = var.zone
  inbound_default_policy  = "drop"
  outbound_default_policy = "accept"
  stateful                = true
  tags                    = var.tags

  inbound_rule {
    action   = "accept"
    protocol = "TCP"
    port     = 80
    ip_range = "0.0.0.0/0"
  }

  inbound_rule {
    action   = "accept"
    protocol = "TCP"
    port     = 443
    ip_range = "0.0.0.0/0"
  }
}

resource "scaleway_instance_server" "this" {
  name              = var.name
  project_id        = var.project_id
  zone              = var.zone
  type              = var.instance_type
  image             = "ubuntu_noble"
  ip_id             = scaleway_instance_ip.this.id
  security_group_id = scaleway_instance_security_group.this.id
  tags              = var.tags
  # A type change would otherwise migrate the server, which Scaleway refuses between a DEV1
  # type's local volume and a Block Storage boot volume; deploy hands a new server its key
  # and release, and the flexible IP outlives it.
  replace_on_type_change = true

  root_volume {
    volume_type           = local.root_volume_type
    size_in_gb            = 30
    delete_on_termination = true
  }

  private_network {
    pn_id = var.private_network_id
  }

  # User data lands in state and in the metadata server in clear, so it holds no credential.
  user_data = {
    cloud-init = local.user_data
  }

  # deploy adds the vecten-key entry through the Instances API; the provider writes every
  # user data entry at once, so an update from here would delete that key.
  lifecycle {
    ignore_changes = [user_data]

    precondition {
      condition     = !can(regex("^(AMP2|COPARM1|ARM64)-|^[A-Z]+[0-9]+-A[0-9]+C-", upper(var.instance_type)))
      error_message = "${var.instance_type} is an ARM server type; user data installs amd64 Docker plugins, so the server needs an x86_64 type."
    }
  }
}
