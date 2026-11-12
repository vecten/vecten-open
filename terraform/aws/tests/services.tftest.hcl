mock_provider "aws" {
  # An applied instance needs the managed master secret the postgres module's output checks.
  mock_resource "aws_db_instance" {
    defaults = {
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:rds!db-1" }]
    }
  }
  override_data {
    target = data.aws_availability_zones.available
    values = { names = ["eu-west-1a", "eu-west-1b"] }
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  override_data {
    target = data.aws_region.current
    values = { region = "eu-west-1", name = "eu-west-1" }
  }
  override_data {
    target = data.aws_ssm_parameter.al2023
    values = { value = "ami-0abc", insecure_value = "ami-0abc" }
  }
  override_data {
    target = data.aws_route53_zone.this
    values = { zone_id = "Z123" }
  }
}

variables {
  environment       = "prod"
  project_name      = "acme"
  domain            = "data.acme.com"
  dns_zone          = "acme.com"
  region            = "eu-west-1"
  github_repository = "acme/data"
  components        = ["platform", "mcp"]
  profile           = "standard"
  size              = "small"
  warehouse         = "postgres"
}

run "no_service_hostname_adds_nothing" {
  command = plan

  assert {
    condition     = keys(aws_route53_record.hostname) == ["airflow.data.acme.com", "mcp.data.acme.com"]
    error_message = "without service hostnames only the built-in names get a record"
  }
  assert {
    condition     = alltrue([for h in output.hosts : length(h.service_hostnames) == 0])
    error_message = "every host lists an empty service_hostnames"
  }
}

run "standard_records_point_at_the_host_of_each_component" {
  command = apply

  override_resource {
    target = module.host["platform"].aws_eip.this
    values = { public_ip = "198.51.100.1" }
  }
  override_resource {
    target = module.host["mcp"].aws_eip.this
    values = { public_ip = "198.51.100.2" }
  }

  variables {
    service_hostnames = { portal = "platform", "@" = "mcp", bot = "platform" }
  }

  assert {
    condition     = keys(aws_route53_record.hostname) == ["airflow.data.acme.com", "bot.data.acme.com", "data.acme.com", "mcp.data.acme.com", "portal.data.acme.com"]
    error_message = "each label gets a record beside the built-in ones, @ the domain itself"
  }
  assert {
    condition     = aws_route53_record.hostname["portal.data.acme.com"].records == toset(["198.51.100.1"]) && aws_route53_record.hostname["data.acme.com"].records == toset(["198.51.100.2"])
    error_message = "a service hostname points at the host that runs its component"
  }
  assert {
    condition     = alltrue([for r in aws_route53_record.hostname : r.type == "A" && r.ttl == 60 && r.zone_id == "Z123"])
    error_message = "service records are A records with the built-in TTL in the named zone"
  }
  assert {
    condition     = output.hosts["platform"].service_hostnames == tolist(["bot.data.acme.com", "portal.data.acme.com"]) && output.hosts["mcp"].service_hostnames == tolist(["data.acme.com"])
    error_message = "each host lists the service hostnames it serves, sorted"
  }
  assert {
    condition     = output.hosts["platform"].hostnames == tolist(["airflow.data.acme.com"]) && output.hosts["mcp"].hostnames == tolist(["mcp.data.acme.com"])
    error_message = "the built-in hostnames keep their pairing with components"
  }
}

run "minimal_serves_every_service_hostname_from_the_one_host" {
  command = plan

  variables {
    profile           = "minimal"
    service_hostnames = { portal = "platform", bot = "mcp" }
  }

  assert {
    condition     = output.hosts["main"].service_hostnames == tolist(["bot.data.acme.com", "portal.data.acme.com"])
    error_message = "under minimal the one host serves every service hostname"
  }
}

run "apex_is_refused_where_the_domain_is_the_zone" {
  command = plan

  variables {
    domain            = "acme.com"
    service_hostnames = { "@" = "platform" }
  }

  expect_failures = [var.service_hostnames]
}

run "apex_is_admitted_below_the_zone" {
  command = plan

  variables {
    service_hostnames = { "@" = "platform" }
  }

  assert {
    condition     = contains(keys(aws_route53_record.hostname), "data.acme.com")
    error_message = "@ on a domain below its zone is a record at the domain"
  }
}

run "reserved_labels_are_refused" {
  command = plan

  variables {
    service_hostnames = { www = "platform" }
  }

  expect_failures = [var.service_hostnames]
}

run "underscore_labels_are_refused" {
  command = plan

  variables {
    service_hostnames = { "_acme-challenge" = "platform" }
  }

  expect_failures = [var.service_hostnames]
}

run "malformed_labels_are_refused" {
  command = plan

  variables {
    service_hostnames = { "Portal" = "platform" }
  }

  expect_failures = [var.service_hostnames]
}

run "a_component_the_project_lacks_is_refused" {
  command = plan

  variables {
    components        = ["platform"]
    service_hostnames = { portal = "mcp" }
  }

  expect_failures = [var.service_hostnames]
}
