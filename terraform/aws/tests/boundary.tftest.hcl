# The JSON fixture read below is the permissions boundary bootstrap creates for project acme,
# environment prod, account 123456789012, held equal to vecten's boundary_policy by a pytest.
# Every action and resource a host's scope policy grants must fall under one of its
# statements, or the boundary would silently deny it on the VM. Globs become anchored regular
# expressions: `.` is escaped, `*` matches any run of characters, `?` any one character.

mock_provider "aws" {
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

run "standard_scope_policies_fit_the_boundary" {
  command = plan

  assert {
    condition = alltrue(flatten([
      for h, m in module.host : [
        for s in jsondecode(m.scope_policy).Statement : [
          for pair in setproduct(flatten([s.Action]), flatten([s.Resource])) : anytrue([
            for b in jsondecode(file("${path.module}/tests/boundary.json")).Statement :
            anytrue([for a in flatten([b.Action]) : can(regex(format("^%s$", replace(replace(replace(a, ".", "\\."), "*", ".*"), "?", ".")), pair[0]))])
            && anytrue([for r in flatten([b.Resource]) : can(regex(format("^%s$", replace(replace(replace(r, ".", "\\."), "*", ".*"), "?", ".")), pair[1]))])
          ])
        ]
      ]
    ]))
    error_message = "a standard host's scope policy grants an action or resource the boundary denies"
  }
}

run "minimal_scope_policy_fits_the_boundary" {
  command = plan

  variables {
    profile = "minimal"
  }

  assert {
    condition = alltrue(flatten([
      for h, m in module.host : [
        for s in jsondecode(m.scope_policy).Statement : [
          for pair in setproduct(flatten([s.Action]), flatten([s.Resource])) : anytrue([
            for b in jsondecode(file("${path.module}/tests/boundary.json")).Statement :
            anytrue([for a in flatten([b.Action]) : can(regex(format("^%s$", replace(replace(replace(a, ".", "\\."), "*", ".*"), "?", ".")), pair[0]))])
            && anytrue([for r in flatten([b.Resource]) : can(regex(format("^%s$", replace(replace(replace(r, ".", "\\."), "*", ".*"), "?", ".")), pair[1]))])
          ])
        ]
      ]
    ]))
    error_message = "the minimal host's scope policy grants an action or resource the boundary denies"
  }
}

run "platform_only_scope_policy_fits_the_boundary" {
  command = plan

  variables {
    components = ["platform"]
  }

  assert {
    condition = alltrue(flatten([
      for h, m in module.host : [
        for s in jsondecode(m.scope_policy).Statement : [
          for pair in setproduct(flatten([s.Action]), flatten([s.Resource])) : anytrue([
            for b in jsondecode(file("${path.module}/tests/boundary.json")).Statement :
            anytrue([for a in flatten([b.Action]) : can(regex(format("^%s$", replace(replace(replace(a, ".", "\\."), "*", ".*"), "?", ".")), pair[0]))])
            && anytrue([for r in flatten([b.Resource]) : can(regex(format("^%s$", replace(replace(replace(r, ".", "\\."), "*", ".*"), "?", ".")), pair[1]))])
          ])
        ]
      ]
    ]))
    error_message = "the platform-only host's scope policy grants an action or resource the boundary denies"
  }
}

run "snowflake_scope_policies_fit_the_boundary" {
  command = plan

  variables {
    warehouse         = "snowflake"
    snowflake_account = "ACME-PROD"
  }

  assert {
    condition = alltrue(flatten([
      for h, m in module.host : [
        for s in jsondecode(m.scope_policy).Statement : [
          for pair in setproduct(flatten([s.Action]), flatten([s.Resource])) : anytrue([
            for b in jsondecode(file("${path.module}/tests/boundary.json")).Statement :
            anytrue([for a in flatten([b.Action]) : can(regex(format("^%s$", replace(replace(replace(a, ".", "\\."), "*", ".*"), "?", ".")), pair[0]))])
            && anytrue([for r in flatten([b.Resource]) : can(regex(format("^%s$", replace(replace(replace(r, ".", "\\."), "*", ".*"), "?", ".")), pair[1]))])
          ])
        ]
      ]
    ]))
    error_message = "a Snowflake project's scope policy grants an action or resource the boundary denies"
  }
}
