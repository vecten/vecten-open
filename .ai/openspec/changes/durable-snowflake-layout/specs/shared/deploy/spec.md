## MODIFIED Requirements

### Requirement: Snowflake layout per environment

A Snowflake project SHALL choose, through `[warehouse] shared_account` in `vecten.toml`, whether its environments' Snowflake accounts may hold anything besides one environment; it SHALL default to `false`. `deploy` SHALL record the value an environment was first deployed with under `[environments.<env>]` as `snowflake_shared_account`, after the deploy workflow succeeds, leaving it unset on dispatch or workflow failure, read an environment with no record as deployed with `false`, and refuse in `deploy` and `ship`, naming the recorded value and the migration, an environment whose record differs from the setting, unless the setting is `true` and the recorded fixed environment's account holds no other environment of the project. `shared_account` SHALL be refused on a project without the `platform` component.

Before applying any root, deploy SHALL refuse a saved plan that would rename an existing database, warehouse, role, or loader user managed by the scaffolded Snowflake module and explain how to record its existing layout. Project-owned resources in the same root do not establish the Vecten layout.

#### Scenario: Default

- **WHEN** a Snowflake project with no `shared_account` setting deploys `prod`
- **THEN** every Snowflake object carries the name "Fixed Snowflake names" lists, and `[environments.prod]` records `snowflake_shared_account = false`

#### Scenario: First deploy unsuccessful

- **WHEN** `deploy --env prod` runs for the first time with `shared_account = true` and the dispatch or the workflow fails
- **THEN** it exits non-zero and `[environments.prod]` records no layout, so `shared_account` may still be changed

#### Scenario: Setting changed after the first deploy

- **WHEN** `prod` was deployed with `shared_account = false`, its account is also named by `deploy/envs/dev.tfvars`, `shared_account` is set to `true`, and `deploy --env prod` runs
- **THEN** it exits non-zero before any Terraform runs, naming the recorded layout and saying that moving an environment to a shared account is a new deployment

#### Scenario: Fixed prod beside a shared dev in another account

- **WHEN** `prod` was deployed with fixed names in account `ACME-PROD`, `shared_account` is then set to `true`, and `dev` is deployed in account `ACME-SHARED`
- **THEN** `dev` deploys with suffixed names, `prod` keeps its fixed names, and both deploy without refusal

#### Scenario: MCP-only project

- **WHEN** `shared_account = true` is set in a project whose `components` is `["mcp"]`
- **THEN** every command reading `vecten.toml` exits non-zero naming `shared_account` and the `platform` component

#### Scenario: Workflows launched from GitHub

- **WHEN** the first workflow created shared-account objects and another checkout proposes fixed names
- **THEN** CI refuses the saved plan before applying any root, including when the checkout has no layout record

#### Scenario: First workflow refused before apply

- **WHEN** a first workflow failed before creating a Snowflake object
- **THEN** the operator can correct the layout and run deploy again

#### Scenario: Workflow failed after creating a role

- **WHEN** a failed workflow left a fixed loader role in state and the next checkout proposes a suffixed role
- **THEN** deploy refuses before applying any root and explains how to keep the fixed layout by recording `snowflake_shared_account = false` under that environment and pushing the change
