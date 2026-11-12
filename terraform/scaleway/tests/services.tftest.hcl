# The random provider runs for real: mock providers cannot serve ephemeral resources, and
# random makes no network call.
mock_provider "scaleway" {
  # The provider checks that these IDs are zoned or regional UUIDs when an apply run uses them.
  mock_resource "scaleway_vpc_private_network" {
    defaults = { id = "fr-par/0d4c7a52-1b2e-4f3a-9c8d-112233445566" }
  }
  mock_resource "scaleway_instance_ip" {
    defaults = { id = "fr-par-1/1e5d8b63-2c3f-4a4b-8d9e-223344556677", address = "51.15.0.10" }
  }
  mock_resource "scaleway_instance_server" {
    defaults = { id = "fr-par-1/7b0e2c4d-1a2b-4c3d-8e9f-001122334455" }
  }
  mock_resource "scaleway_instance_security_group" {
    defaults = { id = "fr-par-1/2f6e9c74-3d4a-4b5c-9eaf-334455667788" }
  }
  mock_resource "scaleway_rdb_instance" {
    defaults = { id = "fr-par/3a7fad85-4e5b-4c6d-8fb0-445566778899" }
  }
}

variables {
  environment       = "prod"
  project_name      = "acme"
  domain            = "data.acme.com"
  dns_zone          = "acme.com"
  region            = "fr-par"
  github_repository = "acme/data"
  components        = ["platform", "mcp"]
  profile           = "standard"
  size              = "small"
  warehouse         = "postgres"
  project_id        = "1A2B3C4D-2222-4333-8444-555555555555"
  application_ids = {
    deploy   = "aaaaaaaa-0000-4000-8000-000000000001"
    plan     = "aaaaaaaa-0000-4000-8000-000000000002"
    platform = "aaaaaaaa-0000-4000-8000-000000000003"
    mcp      = "aaaaaaaa-0000-4000-8000-000000000004"
    main     = "aaaaaaaa-0000-4000-8000-000000000005"
  }
}

run "no_service_hostname_adds_nothing" {
  command = plan

  assert {
    condition     = keys(scaleway_domain_record.hostname) == ["airflow.data.acme.com", "mcp.data.acme.com"]
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
    target = module.host["platform"].scaleway_instance_ip.this
    values = { id = "fr-par-1/1e5d8b63-2c3f-4a4b-8d9e-223344556677", address = "198.51.100.1" }
  }
  override_resource {
    target = module.host["mcp"].scaleway_instance_ip.this
    values = { id = "fr-par-1/1e5d8b63-2c3f-4a4b-8d9e-223344556678", address = "198.51.100.2" }
  }

  variables {
    service_hostnames = { portal = "platform", "@" = "mcp", bot = "platform" }
  }

  assert {
    condition     = keys(scaleway_domain_record.hostname) == ["airflow.data.acme.com", "bot.data.acme.com", "data.acme.com", "mcp.data.acme.com", "portal.data.acme.com"]
    error_message = "each label gets a record beside the built-in ones, @ the domain itself"
  }
  assert {
    condition     = scaleway_domain_record.hostname["portal.data.acme.com"].data == "198.51.100.1" && scaleway_domain_record.hostname["data.acme.com"].data == "198.51.100.2"
    error_message = "a service hostname points at the host that runs its component"
  }
  assert {
    condition     = alltrue([for r in scaleway_domain_record.hostname : r.type == "A" && r.ttl == 60 && r.dns_zone == "acme.com"])
    error_message = "service records are A records with the built-in TTL in the named zone"
  }
  assert {
    condition     = scaleway_domain_record.hostname["portal.data.acme.com"].name == "portal.data"
    error_message = "service record names are relative to the zone"
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
    condition     = scaleway_domain_record.hostname["data.acme.com"].name == "data"
    error_message = "@ on a domain below its zone is a record at the domain, named relative to the zone"
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
