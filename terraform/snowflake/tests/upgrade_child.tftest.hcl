# An earlier release's ANALYST grants, held in state at module.snowflake addresses as a
# deployed environment's thin root holds them, are forgotten without a revoke: the removed
# blocks act inside the child module.
mock_provider "snowflake" {
  override_data {
    target = module.snowflake.data.snowflake_current_role.deployer
    values = { name = "VECTEN_DEPLOY_ROLE" }
  }
}

run "the_previous_release_granted_analyst_its_reads_through_the_root" {
  state_key = "upgrade_child"

  module {
    source = "./tests/child/previous"
  }
}

run "the_upgrade_forgets_them_inside_the_child_module" {
  state_key = "upgrade_child"
  command   = plan

  module {
    source = "./tests/child/current"
  }
}
