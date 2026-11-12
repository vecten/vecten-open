output "environment" {
  description = "Environment name."
  value       = var.environment
}

output "region" {
  description = "AWS region of the environment, as the provider resolves it."
  value       = local.region
}

output "hosts" {
  description = "Per host key (platform, mcp or main): instance id, Elastic IP, components, built-in hostnames and service hostnames."
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
  description = "Host key of the one VM that reads the master secrets and runs the setup SQL."
  value       = local.setup_host
}

output "bucket" {
  description = "Artifacts bucket: bundles, per-component prefixes, Airflow logs and dbt artifacts."
  value       = aws_s3_bucket.artifacts.bucket
}

output "secret_prefix" {
  description = "Secrets Manager name prefix, vecten/<project>/<env>/."
  value       = local.secret_prefix
}

output "postgres" {
  description = "Per instance key (state, warehouse): host, port, databases and the master secret ARN."
  value = {
    for k, m in module.postgres : k => {
      host          = m.address
      port          = m.port
      databases     = tolist(local.instances[k].databases)
      master_secret = m.master_secret_arn
    }
  }
}
