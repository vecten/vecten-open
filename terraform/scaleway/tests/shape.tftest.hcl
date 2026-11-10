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

run "standard_both_components_on_postgres" {
  command = plan

  assert {
    condition     = keys(output.hosts) == ["mcp", "platform"]
    error_message = "standard with both components runs one host per component"
  }
  assert {
    condition     = output.hosts["platform"].components == tolist(["platform"]) && output.hosts["mcp"].components == tolist(["mcp"])
    error_message = "each standard host carries its own component only"
  }
  assert {
    condition     = output.hosts["platform"].hostnames == tolist(["airflow.data.acme.com"]) && output.hosts["mcp"].hostnames == tolist(["mcp.data.acme.com"])
    error_message = "airflow.<domain> belongs to the platform host and mcp.<domain> to the MCP host"
  }
  assert {
    condition     = keys(output.postgres) == ["state", "warehouse"]
    error_message = "standard on Postgres runs a state and a warehouse instance"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state"])
    error_message = "the state instance holds airflow and mcp_state"
  }
  assert {
    condition     = output.postgres["warehouse"].databases == tolist(["warehouse"])
    error_message = "the warehouse instance holds warehouse only"
  }
  assert {
    condition     = output.setup_host == "mcp"
    error_message = "the MCP host is the setup host when mcp is a component"
  }
  assert {
    condition     = output.environment == "prod" && output.region == "fr-par"
    error_message = "environment and region echo the inputs"
  }
  assert {
    condition     = output.secret_prefix == "/vecten/acme/prod/"
    error_message = "the secret prefix is /vecten/<project>/<env>/"
  }
  assert {
    condition     = output.bucket == "vecten-acme-prod-artifacts-1a2b3c4d"
    error_message = "the artifacts bucket is vecten-<project>-<env>-artifacts-<first 8 hex of the project id>"
  }
  assert {
    condition     = module.postgres["state"].name == "vecten-acme-prod-state" && module.postgres["warehouse"].name == "vecten-acme-prod-warehouse"
    error_message = "database instances are named <name prefix>-<key>, the names destroy looks up"
  }
  assert {
    condition     = module.host["platform"].name == "vecten-acme-prod-platform" && module.host["mcp"].name == "vecten-acme-prod-mcp"
    error_message = "servers are named <name prefix>-<host>"
  }
}

run "records_are_a_records_in_the_zone" {
  command = plan

  assert {
    condition     = keys(scaleway_domain_record.hostname) == ["airflow.data.acme.com", "mcp.data.acme.com"]
    error_message = "both hostnames get a record"
  }
  assert {
    condition     = scaleway_domain_record.hostname["airflow.data.acme.com"].name == "airflow.data" && scaleway_domain_record.hostname["mcp.data.acme.com"].name == "mcp.data"
    error_message = "record names are relative to the zone"
  }
  assert {
    condition = alltrue([
      for r in scaleway_domain_record.hostname :
      r.type == "A" && r.ttl == 60 && r.dns_zone == "acme.com" && r.project_id == "1A2B3C4D-2222-4333-8444-555555555555"
    ])
    error_message = "records are A records with TTL 60 in the named zone, in the resources project by default"
  }
}

run "records_follow_the_dns_project" {
  command = plan

  variables {
    domain         = "acme.com"
    dns_project_id = "99999999-2222-4333-8444-555555555555"
  }

  assert {
    condition     = scaleway_domain_record.hostname["airflow.acme.com"].name == "airflow"
    error_message = "a domain equal to the zone gives a one-label record name"
  }
  assert {
    condition     = alltrue([for r in scaleway_domain_record.hostname : r.project_id == "99999999-2222-4333-8444-555555555555"])
    error_message = "records live in dns_project_id when it is set"
  }
}

run "everything_lives_in_the_resources_project_and_zone" {
  command = plan

  assert {
    condition     = alltrue([for h in module.host : h.settings.project_id == "1A2B3C4D-2222-4333-8444-555555555555" && h.settings.zone == "fr-par-1"])
    error_message = "servers sit in the resources project, in <region>-1"
  }
  assert {
    condition     = alltrue([for p in module.postgres : p.settings.project_id == "1A2B3C4D-2222-4333-8444-555555555555" && p.settings.region == "fr-par"])
    error_message = "database instances sit in the resources project and region"
  }
  assert {
    condition     = scaleway_object_bucket.artifacts.project_id == "1A2B3C4D-2222-4333-8444-555555555555" && scaleway_object_bucket_policy.artifacts.project_id == "1A2B3C4D-2222-4333-8444-555555555555"
    error_message = "the bucket and its policy name the resources project, which bucket children need"
  }
  assert {
    condition     = scaleway_vpc_private_network.this.project_id == "1A2B3C4D-2222-4333-8444-555555555555" && scaleway_vpc_private_network.this.region == "fr-par"
    error_message = "the private network sits in the resources project and region"
  }
}

run "hosts_boot_ubuntu_on_the_private_network" {
  command = plan

  assert {
    condition     = alltrue([for h in module.host : h.settings.image == "ubuntu_noble"])
    error_message = "servers boot Ubuntu 24.04"
  }
  assert {
    condition     = alltrue([for h in module.host : length(h.settings.private_network_ids) == 1])
    error_message = "each server has one private NIC"
  }
}

run "user_data_prepares_the_watcher_and_holds_no_secret" {
  command = plan

  assert {
    condition     = alltrue([for h in module.host : strcontains(h.settings.user_data, "install -y --no-install-recommends docker.io python3")])
    error_message = "user data installs Docker and python3 from apt"
  }
  assert {
    condition = alltrue([for h in module.host : alltrue([
      strcontains(h.settings.user_data, "https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-x86_64"),
      strcontains(h.settings.user_data, "https://github.com/docker/buildx/releases/download/v0.37.2/"),
      strcontains(h.settings.user_data, "sha256sum -c"),
      strcontains(h.settings.user_data, "/usr/local/lib/docker/cli-plugins/docker-compose"),
      strcontains(h.settings.user_data, "/usr/local/lib/docker/cli-plugins/docker-buildx"),
    ])])
    error_message = "user data installs the pinned Compose and buildx plugins, checksum-verified, into /usr/local/lib/docker/cli-plugins"
  }
  assert {
    condition = alltrue([for h in module.host : alltrue([
      strcontains(h.settings.user_data, "install -d -m 0755 /opt/vecten/releases /opt/vecten/agent /var/www/certbot /etc/letsencrypt"),
      strcontains(h.settings.user_data, "/etc/systemd/system/vecten-boot.service"),
      strcontains(h.settings.user_data, "/etc/systemd/system/vecten-watch.service"),
      strcontains(h.settings.user_data, "/etc/systemd/system/vecten-watch.timer"),
      strcontains(h.settings.user_data, "OnUnitActiveSec=15s"),
      strcontains(h.settings.user_data, "ExecStart=/usr/bin/python3 /opt/vecten/agent/apply.py watch"),
      strcontains(h.settings.user_data, "systemctl enable --now vecten-watch.timer"),
      strcontains(h.settings.user_data, "certbot/certbot:v5.8.0 renew"),
      strcontains(h.settings.user_data, "systemctl enable --now vecten-certbot-renew.timer"),
    ])])
    error_message = "user data creates the directories and installs the boot unit, the 15-second watcher and the renewal timer"
  }
  assert {
    condition     = alltrue([for h in module.host : strcontains(h.settings.user_data, base64gzip(file("${path.module}/modules/host/agent/apply.py")))])
    error_message = "user data carries the bootstrap agent, so the watcher runs before any bundle"
  }
  assert {
    condition     = alltrue([for h in module.host : strcontains(h.settings.user_data, "-d 169.254.42.42 -j DROP")])
    error_message = "containers cannot reach the metadata server that holds the VM key"
  }
  assert {
    condition     = alltrue([for h in module.host : !strcontains(lower(h.settings.user_data), "secret_key") && !strcontains(lower(h.settings.user_data), "password")])
    error_message = "user data holds no credential"
  }
  assert {
    condition = alltrue([for k, h in module.host : alltrue([
      h.settings.watch_config.host == k,
      h.settings.watch_config.components == output.hosts[k].components,
      h.settings.watch_config.bucket == scaleway_object_bucket.artifacts.name,
      h.settings.watch_config.region == var.region,
      h.settings.watch_config.project_id == var.project_id,
      h.settings.watch_config.secret_prefix == output.secret_prefix,
      strcontains(h.settings.user_data, "cat > /etc/vecten/watch.json <<'WATCH'\n${jsonencode(h.settings.watch_config)}\nWATCH"),
    ])])
    error_message = "user data writes the watcher's host, components, bucket, region, project and secret prefix to /etc/vecten/watch.json"
  }
  assert {
    condition     = alltrue([for h in module.host : keys(h.settings.user_data_keys) == ["cloud-init"]])
    error_message = "the script is the cloud-init user data key, the one Scaleway's cloud-init reads"
  }
}

run "host_inbound_is_80_and_443_only" {
  command = plan

  assert {
    condition     = alltrue([for h in module.host : h.settings.security_group.inbound_default_policy == "drop"])
    error_message = "inbound traffic is dropped unless a rule accepts it"
  }
  assert {
    condition     = alltrue([for h in module.host : h.settings.security_group.outbound_default_policy == "accept"])
    error_message = "outbound traffic is accepted"
  }
  assert {
    condition     = alltrue([for h in module.host : length(h.settings.security_group.inbound_rules) == 2])
    error_message = "each security group has exactly two inbound rules"
  }
  assert {
    condition     = alltrue([for h in module.host : toset([for r in h.settings.security_group.inbound_rules : r.port]) == toset([80, 443])])
    error_message = "hosts accept 80 and 443"
  }
  assert {
    condition = alltrue(flatten([
      for h in module.host : [
        for r in h.settings.security_group.inbound_rules : r.action == "accept" && r.protocol == "TCP" && r.ip_range == "0.0.0.0/0"
      ]
    ]))
    error_message = "each inbound rule accepts one TCP port from the internet"
  }
}

run "artifacts_bucket_is_versioned_with_a_bundle_lifecycle" {
  command = plan

  assert {
    condition     = scaleway_object_bucket.artifacts.versioning[0].enabled
    error_message = "the bucket is versioned"
  }
  assert {
    condition     = length(scaleway_object_bucket.artifacts.lifecycle_rule) == 1
    error_message = "the bucket has one lifecycle rule"
  }
  assert {
    condition = (
      scaleway_object_bucket.artifacts.lifecycle_rule[0].enabled
      && scaleway_object_bucket.artifacts.lifecycle_rule[0].prefix == "bundles/"
      && scaleway_object_bucket.artifacts.lifecycle_rule[0].noncurrent_version_expiration[0].noncurrent_days == 30
    )
    error_message = "noncurrent bundle versions expire after 30 days"
  }
  assert {
    condition     = !scaleway_object_bucket.artifacts.force_destroy
    error_message = "the bucket keeps its objects unless destroy runs"
  }
}

run "destroying_lets_the_bucket_be_emptied" {
  command = plan

  variables {
    destroying = true
  }

  assert {
    condition     = scaleway_object_bucket.artifacts.force_destroy
    error_message = "vecten destroy may empty the bucket"
  }
}

run "minimal_runs_one_host_and_one_instance" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition     = keys(output.hosts) == ["main"]
    error_message = "minimal runs one host named main"
  }
  assert {
    condition     = output.hosts["main"].components == tolist(["platform", "mcp"])
    error_message = "the minimal host carries both components"
  }
  assert {
    condition     = output.hosts["main"].hostnames == tolist(["airflow.data.acme.com", "mcp.data.acme.com"])
    error_message = "the minimal host serves both hostnames"
  }
  assert {
    condition     = keys(output.postgres) == ["state"]
    error_message = "minimal runs one instance"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state", "warehouse"])
    error_message = "the minimal instance holds all three databases"
  }
  assert {
    condition     = output.setup_host == "main"
    error_message = "the minimal host is the setup host"
  }
  assert {
    condition     = length(scaleway_domain_record.hostname) == 2
    error_message = "both hostnames get a record under minimal"
  }
}

run "mcp_only" {
  command = plan

  variables {
    components = ["mcp"]
  }

  assert {
    condition     = keys(output.hosts) == ["mcp"]
    error_message = "MCP only runs one MCP host"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["mcp_state"])
    error_message = "the state instance holds mcp_state only"
  }
  assert {
    condition     = keys(scaleway_domain_record.hostname) == ["mcp.data.acme.com"]
    error_message = "MCP only issues no airflow record"
  }
  assert {
    condition     = output.setup_host == "mcp"
    error_message = "the MCP host is the setup host"
  }
  assert {
    condition     = keys(output.postgres) == ["state"]
    error_message = "MCP only sits over an existing warehouse, so no warehouse instance exists"
  }
  assert {
    condition     = length(module.postgres["state"].settings.engine_settings) == 0
    error_message = "without a warehouse instance no instance carries the log prefix"
  }
}

run "platform_only" {
  command = plan

  variables {
    components = ["platform"]
  }

  assert {
    condition     = keys(output.hosts) == ["platform"]
    error_message = "platform only runs one platform host"
  }
  assert {
    condition     = output.setup_host == "platform"
    error_message = "the platform host is the setup host without mcp"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow"])
    error_message = "the state instance holds airflow only"
  }
}

run "snowflake_keeps_state_only" {
  command = plan

  variables {
    warehouse         = "snowflake"
    snowflake_account = "ORG-ACCOUNT"
  }

  assert {
    condition     = keys(output.postgres) == ["state"]
    error_message = "a Snowflake project runs no warehouse instance"
  }
  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state"])
    error_message = "the state instance still holds airflow and mcp_state"
  }
}

run "minimal_snowflake_holds_no_warehouse_database" {
  command = plan

  variables {
    profile   = "minimal"
    warehouse = "snowflake"
  }

  assert {
    condition     = output.postgres["state"].databases == tolist(["airflow", "mcp_state"])
    error_message = "minimal on Snowflake keeps the warehouse out of Postgres"
  }
}

run "long_project_name_keeps_names_legal" {
  command = plan

  variables {
    project_name = "a_very_long_project_name--with_symbols_and_more_words_than_fit"
    environment  = "staging"
  }

  assert {
    condition     = module.postgres["warehouse"].name == "vecten-a-very-long-project-name-with-symbols-staging-warehouse"
    error_message = "the name prefix follows vecten.deploy.names.name_prefix"
  }
  assert {
    condition     = output.bucket == "vecten-a-very-long-project-name-with-staging-artifacts-1a2b3c4d"
    error_message = "the artifacts bucket follows vecten.deploy.names.scaleway_bucket_name"
  }
  assert {
    condition     = length(output.bucket) <= 63
    error_message = "the artifacts bucket stays within 63 characters"
  }
}

run "other_region_moves_the_zone" {
  command = plan

  variables {
    region = "nl-ams"
  }

  assert {
    condition     = output.region == "nl-ams" && alltrue([for h in module.host : h.settings.zone == "nl-ams-1"])
    error_message = "the zone is <region>-1"
  }
}

run "rejects_a_missing_host_application" {
  command = plan

  variables {
    application_ids = {
      deploy   = "aaaaaaaa-0000-4000-8000-000000000001"
      plan     = "aaaaaaaa-0000-4000-8000-000000000002"
      platform = "aaaaaaaa-0000-4000-8000-000000000003"
    }
  }

  expect_failures = [var.application_ids]
}

run "rejects_a_missing_deploy_application" {
  command = plan

  variables {
    application_ids = {
      plan     = "aaaaaaaa-0000-4000-8000-000000000002"
      platform = "aaaaaaaa-0000-4000-8000-000000000003"
      mcp      = "aaaaaaaa-0000-4000-8000-000000000004"
    }
  }

  expect_failures = [var.application_ids]
}

run "rejects_an_application_id_that_is_not_a_uuid" {
  command = plan

  variables {
    application_ids = {
      deploy   = "aaaaaaaa-0000-4000-8000-000000000001"
      plan     = "aaaaaaaa-0000-4000-8000-000000000002"
      platform = "aaaaaaaa-0000-4000-8000-000000000003"
      mcp      = "not-an-id"
    }
  }

  expect_failures = [var.application_ids]
}

run "rejects_a_project_id_that_is_not_a_uuid" {
  command = plan

  variables {
    project_id = "acme"
  }

  expect_failures = [var.project_id]
}

run "rejects_a_region_name_that_is_not_scaleway_shaped" {
  command = plan

  variables {
    region = "eu-west-1"
  }

  expect_failures = [var.region]
}

run "rejects_a_project_name_vecten_init_would_refuse" {
  command = plan

  variables {
    project_name = "Acme Corp"
  }

  expect_failures = [var.project_name]
}

run "rejects_an_unknown_component" {
  command = plan

  variables {
    components = ["platform", "airflow"]
  }

  expect_failures = [var.components]
}

run "rejects_an_unknown_profile" {
  command = plan

  variables {
    profile = "tiny"
  }

  expect_failures = [var.profile]
}

run "rejects_an_unknown_warehouse" {
  command = plan

  variables {
    warehouse = "bigquery"
  }

  expect_failures = [var.warehouse]
}

run "rejects_a_domain_outside_the_zone" {
  command = plan

  variables {
    domain = "data.other.com"
  }

  expect_failures = [var.domain]
}

run "rejects_an_environment_with_doubled_hyphens" {
  command = plan

  variables {
    environment = "prod--eu"
  }

  expect_failures = [var.environment]
}

run "instance_id_is_the_bare_server_id" {
  command = apply

  assert {
    condition     = alltrue([for h in output.hosts : h.instance_id == "7b0e2c4d-1a2b-4c3d-8e9f-001122334455"])
    error_message = "instance_id is the server UUID the Instances API takes, without its zone"
  }
  assert {
    condition     = alltrue([for h in module.host : h.settings.flexible_ip_attached])
    error_message = "each server carries its own flexible IP"
  }
  assert {
    condition     = alltrue([for h in output.hosts : h.public_ip == "51.15.0.10"])
    error_message = "public_ip is the flexible IP address"
  }
  assert {
    condition     = alltrue([for r in scaleway_domain_record.hostname : r.data == "51.15.0.10"])
    error_message = "records point at the host's flexible IP"
  }
}
