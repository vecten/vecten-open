## Why

A Snowflake project cannot run more than one environment today (issue #34).

- The Snowflake module and the runtime packages use fixed, account-wide names: the roles `ANALYST`, `LOADER` and `PROVISIONER`, the users `LOADER` and `VECTEN_PROVISIONER`, the security integration `VECTEN_EXTERNAL_OAUTH`, one database name for every environment, and group roles and person users named without the environment. A second environment in the same account collides with the first, or shares its roles and so its access.
- The spec answers this with "one Snowflake account serves one environment", but the code does not offer that either: `deploy` refuses an environment whose `snowflake_account` differs from `[warehouse] account` in `vecten.toml`, and the runtime reads the account from `vecten.toml` alone. A project can therefore deploy one Snowflake environment, in one account.
- Many teams have one Snowflake account, often shared with other teams, and separate environments by database. A client is planning to move onto such an account.

## What Changes

- A project chooses, before its first deploy, whether its Snowflake account is shared: `[warehouse] shared_account` in `vecten.toml`, `false` by default. `next` asks it after the Snowflake account. An environment records the choice at its first deploy and keeps it for its life.
- Without a shared account every name stays as it is today, and each environment needs an account of its own. With one, the database is `<BASE>_<ENV>` and the warehouse `<BASE>_<ENV>_WH`, and every account-level object Vecten creates carries the environment's suffix, `_<BASE>_<ENV>`: the `analyst`, `loader` and `provisioner` roles, the loader and provisioner users, the External OAuth integration, the group roles and the people's and services' users (`JANE_DOE_ACME_DEV`).
- Each environment names its Snowflake account in its own tfvars. `[warehouse] account` names `prod`'s, and `ship` hands the environment's account to its hosts.
- One function in `vecten-shared`, `snowflake_names`, spells every Snowflake name. The Terraform module takes the names as variables, and the platform, the MCP and the CLI read them from that function instead of from constants.
- In a shared account each environment's MCP has its own provisioner, its own security integration, and its own users for people and services. An authentication policy binds those users to the environment's integration, so no other environment's signing key can sign in as them. The MCP does not adopt existing users in a shared account.
- `osi/` documents keep naming the project's database in their sources; the MCP reads that name as its own environment's database.
- `destroy --delete-data <env>` on a shared account removes only that environment's objects, and refuses while the environment's provisioner still owns users or roles, printing the statements an administrator runs to drop them.

Nothing changes for an existing project. An environment deployed with fixed names keeps them.

## Impact

- Specs: `shared/deploy`, `shared/identity-contract`, `mcp/warehouse-identity`, `mcp/access-control`, `mcp/semantic-model`, `cli/cli-lifecycle`, `cli/agent-guide`.
- Code: `vecten_shared.provider` (`snowflake_names`, `runtime`), `vecten_shared.identity_setup`, `vecten_shared/sql/snowflake_access.js`, `vecten_shared.config`, `vecten_platform.sync.destination`, `vecten_mcp.runtime_setup`, `vecten_mcp.warehouse.snowflake`, `vecten_mcp.model`, `vecten.deploy.snowflake`, `vecten.deploy.tfvars`, `vecten.deploy.run`, `vecten.stack.compose`, `vecten.mcp_onboarding`, `vecten.next.deploy`.
- Terraform: `terraform/snowflake` (role and user names become variables) and the scaffolded Snowflake thin root.
- Docs: the rendered runbook's Snowflake section, `docs/mcp-identity.md`.
- Builds on #56 (each environment's MCP issuer) and shares #58's way of handing a tfvars value to the hosts. Leaves room for #51 (restricted marts) and #4 (one reader role per person).
