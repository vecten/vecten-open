output "environment" {
  description = "Environment name."
  value       = var.environment
}

output "region" {
  description = "Scaleway region of the environment; zonal resources sit in <region>-1."
  value       = var.region
}

output "hosts" {
  description = "Per host key (platform, mcp or main): server ID without its zone, flexible IP, components, built-in hostnames and service hostnames."
  value = {
    for h, m in module.host : h => {
      instance_id       = m.instance_id
      public_ip         = m.public_ip
      components        = tolist(local.host_components[h])
      hostnames         = tolist(local.host_hostnames[h])
      service_hostnames = tolist(local.host_service_hostnames[h])
    }
  }
}

output "setup_host" {
  description = "Host key of the one VM that reads the admin secrets and runs the setup SQL."
  value       = local.setup_host
}

output "bucket" {
  description = "Artifacts bucket: bundles, per-component prefixes, Airflow logs and dbt artifacts."
  value       = scaleway_object_bucket.artifacts.name
}

output "secret_prefix" {
  description = "Secret Manager path prefix, /vecten/<project>/<env>/; a secret's path is the prefix plus its scope."
  value       = local.secret_prefix
}

output "postgres" {
  description = "Per instance key (state, warehouse): private endpoint, databases and the ID of the SETUP_<KEY>_ADMIN_PASSWORD secret."
  value = {
    for k, m in module.postgres : k => {
      host          = m.address
      port          = m.port
      databases     = tolist(local.instances[k].databases)
      master_secret = element(split("/", scaleway_secret.admin[k].id), length(split("/", scaleway_secret.admin[k].id)) - 1)
    }
  }
}
