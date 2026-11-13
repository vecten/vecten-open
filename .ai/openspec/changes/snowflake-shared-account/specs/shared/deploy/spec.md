## ADDED Requirements

### Requirement: Snowflake layout per environment

A Snowflake project SHALL choose, through `[warehouse] shared_account` in `vecten.toml`, whether its environments' Snowflake accounts may hold anything besides one environment; it SHALL default to `false`. `deploy` SHALL record the value an environment was first deployed with under `[environments.<env>]` as `snowflake_shared_account`, read an environment with no record as deployed with `false`, and refuse, naming the recorded value and the migration, an environment whose record differs from the setting, unless the setting is `true` and the recorded fixed environment's account holds no other environment of the project. `shared_account` SHALL be refused on a project without the `platform` component.

#### Scenario: Default

- **WHEN** a Snowflake project with no `shared_account` setting deploys `prod`
- **THEN** every Snowflake object carries the name "Fixed Snowflake names" lists, and `[environments.prod]` records `snowflake_shared_account = false`

#### Scenario: Setting changed after the first deploy

- **WHEN** `prod` was deployed with `shared_account = false`, its account is also named by `deploy/envs/dev.tfvars`, `shared_account` is set to `true`, and `deploy --env prod` runs
- **THEN** it exits non-zero before any Terraform runs, naming the recorded layout and saying that moving an environment to a shared account is a new deployment

#### Scenario: Fixed prod beside a shared dev in another account

- **WHEN** `prod` was deployed with fixed names in account `ACME-PROD`, `shared_account` is then set to `true`, and `dev` is deployed in account `ACME-SHARED`
- **THEN** `dev` deploys with suffixed names, `prod` keeps its fixed names, and both deploy without refusal

#### Scenario: MCP-only project

- **WHEN** `shared_account = true` is set in a project whose `components` is `["mcp"]`
- **THEN** every command reading `vecten.toml` exits non-zero naming `shared_account` and the `platform` component

### Requirement: One source for Snowflake names

One function in `vecten-shared` SHALL derive every name Vecten gives an object in Snowflake from `vecten.toml`, the environment's name and its account, and the Snowflake root, the platform, the MCP and the CLI SHALL take the names from it rather than spell any. Names SHALL be upper case with hyphens as underscores, the loader's user and role SHALL share one name, and every object the Snowflake root creates SHALL carry the comment `vecten <project> <env>`.

#### Scenario: No name spelled outside the function

- **WHEN** the runtime packages and the CLI are searched for a Snowflake role, user or integration name
- **THEN** none is found outside the naming function and its tests

### Requirement: Fixed Snowflake names

Without a shared account the database SHALL be `[warehouse] database` or the project name, the warehouse `<PROJECT>_<ENV>_WH`, the roles `ANALYST`, `LOADER` and `PROVISIONER`, the users `LOADER` and `VECTEN_PROVISIONER`, the security integration `VECTEN_EXTERNAL_OAUTH` and the procedures' schema `<DATABASE>.VECTEN_ACCESS`, and group roles and person and service users SHALL carry no suffix.

#### Scenario: Existing names kept

- **WHEN** the names are derived for project `acme`, environment `prod`, with fixed names and no `[warehouse] database`
- **THEN** they are exactly `ACME`, `ACME_PROD_WH`, `ANALYST`, `LOADER`, `PROVISIONER`, `VECTEN_PROVISIONER` and `VECTEN_EXTERNAL_OAUTH`

### Requirement: Suffixed Snowflake names in a shared account

With a shared account, where `BASE` is `[warehouse] database` or the project name and `ENV` the environment's name, both upper case with hyphens as underscores, the database SHALL be `<BASE>_<ENV>`, the warehouse `<BASE>_<ENV>_WH`, the procedures' schema `<BASE>_<ENV>.VECTEN_ACCESS` and the authentication policy `<BASE>_<ENV>.VECTEN_ACCESS.MCP_SIGN_IN`, and every account-level name SHALL end in the environment's suffix `_<BASE>_<ENV>`: the roles `ANALYST_<BASE>_<ENV>`, `LOADER_<BASE>_<ENV>` and `PROVISIONER_<BASE>_<ENV>`, the users `LOADER_<BASE>_<ENV>` and `PROVISIONER_<BASE>_<ENV>`, the integration `EXTERNAL_OAUTH_<BASE>_<ENV>`, a group's role `<GROUP>_<BASE>_<ENV>` and a principal's user `<WAREHOUSE NAME>_<BASE>_<ENV>`. `deploy` SHALL refuse an environment name holding a hyphen and a suffix longer than 64 characters.

#### Scenario: Names in a shared account

- **WHEN** the names are derived for project `acme`, environment `dev`, with a shared account
- **THEN** the database is `ACME_DEV`, the warehouse `ACME_DEV_WH`, the roles `ANALYST_ACME_DEV`, `LOADER_ACME_DEV` and `PROVISIONER_ACME_DEV`, the users `LOADER_ACME_DEV` and `PROVISIONER_ACME_DEV`, and the integration `EXTERNAL_OAUTH_ACME_DEV`

#### Scenario: Environment name with a hyphen

- **WHEN** `deploy --env dev-2` runs with a shared account
- **THEN** it exits non-zero before any Terraform runs, naming the hyphen and suggesting `dev2`

#### Scenario: Name taken by another project

- **WHEN** a role the Snowflake root would create already exists in the account
- **THEN** the apply fails naming the role, and the existing role is neither altered nor granted

### Requirement: Snowflake account per environment

Each environment's Snowflake account SHALL be `snowflake_account` in its `deploy/envs/<env>.tfvars`, and `deploy` SHALL refuse `prod` when that differs from `[warehouse] account` in `vecten.toml`. `ship` SHALL hand the environment's account to every host it ships, so the platform and the MCP connect to the environment's own account. With fixed names `deploy` SHALL refuse an environment whose account another environment's tfvars also name; with a shared account it SHALL refuse an environment whose account a fixed-name environment of the project names.

#### Scenario: Two environments in two accounts

- **WHEN** `shared_account` is `false`, `deploy/envs/prod.tfvars` names `ACME-PROD` and `deploy/envs/dev.tfvars` names `ACME-DEV`
- **THEN** both deploy, and `dev`'s loader and MCP connect to `ACME-DEV`

#### Scenario: Two fixed-name environments in one account

- **WHEN** `shared_account` is `false` and `deploy/envs/dev.tfvars` names the account `prod.tfvars` names
- **THEN** `deploy --env dev` exits non-zero before any Terraform runs, naming both files and `shared_account`

#### Scenario: Two environments in one shared account

- **WHEN** `shared_account` is `true` and `dev` and `prod` both name `ACME-SHARED`
- **THEN** both deploy, each with its own database, warehouse, roles and loader user

## MODIFIED Requirements

### Requirement: Snowflake module

When the warehouse is Snowflake, `deploy` SHALL also apply a Snowflake module that creates, in the account the environment's tfvars name, a database, the schemas named by the convention, an `analyst` role, a `loader` role and service user used by dlt and dbt, a `provisioner` role when `mcp` is a component, and a warehouse sized by the tier with auto-suspend, each named as "One source for Snowflake names" derives it. Each environment's Snowflake root SHALL keep its own state, so applying or destroying one environment changes no object of another. `destroy` SHALL keep the Snowflake database unless `--delete-data <env>` is given. With a shared account `--delete-data` SHALL refuse while the environment's `provisioner` owns any user or role, printing the statements that drop them, the provisioner user and the security integration; it SHALL find them by ownership, never by a name pattern. The runbook SHALL state whether the environment's account may hold anything else and name the environment's objects.

#### Scenario: Snowflake project deploy

- **WHEN** `deploy --env prod` runs for a Snowflake project with both components
- **THEN** the cloud module and the Snowflake module are both applied
- **AND** the database holds `staging`, `snapshots`, `core` and `marts` schemas, and the environment's `analyst`, `loader` and `provisioner` roles and warehouse exist

#### Scenario: Postgres project deploy

- **WHEN** `deploy --env prod` runs for a Postgres project
- **THEN** no Snowflake module is applied

#### Scenario: One environment per Snowflake account

- **WHEN** the runbook of a Snowflake project without a shared account is read
- **THEN** it states that the environment's account holds this one environment and nothing else of the same names, and names the fixed roles, users, integration and database

#### Scenario: Runbook with a shared account

- **WHEN** the runbook of a Snowflake project with a shared account is read
- **THEN** it names the environment's suffix and its suffixed objects, the authentication policy, the cleanup that data deletion needs, and advises one deploy user per environment

#### Scenario: Destroy keeps the Snowflake database

- **WHEN** `destroy --env prod --confirm prod` runs for a Snowflake project
- **THEN** the Snowflake module is not destroyed and the result says it kept the database

#### Scenario: Destroy one environment of a shared account

- **WHEN** `dev` and `prod` share an account and `destroy --env dev --confirm dev --delete-data dev` runs after an administrator dropped the users and roles `dev`'s provisioner owned
- **THEN** `dev`'s database, warehouse, roles and loader user are gone, and every object of `prod` is unchanged

#### Scenario: Data deletion while people remain

- **WHEN** `destroy --env dev --confirm dev --delete-data dev` runs with a shared account while `PROVISIONER_ACME_DEV` owns the user `JANE_DOE_ACME_DEV`
- **THEN** it exits non-zero before any Terraform runs, and prints a `DROP USER "JANE_DOE_ACME_DEV"` and the statements that drop `dev`'s provisioner user and integration, for an administrator to run
