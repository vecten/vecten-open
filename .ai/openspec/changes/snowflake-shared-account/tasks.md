## 1. One source for every Snowflake name (no behaviour change)

- [x] 1.1 `snowflake_names(config, env)` returns the account, suffix, database, warehouse, `analyst`, `loader` and `provisioner` roles, loader and provisioner users, integration, access schema and authentication policy; with fixed names each equals today's literal
- [x] 1.2 `vecten_platform.sync.destination.loader_connection` takes the loader's user and role from it; `SNOWFLAKE_LOADER_ROLE` goes
- [x] 1.3 `OutputsRuntimeProvider._snowflake_warehouse` takes the analyst role, database, warehouse and account from it; `SNOWFLAKE_ANALYST_ROLE` goes
- [x] 1.4 `vecten_mcp.runtime_setup.configured_runtime` signs in as the names' provisioner user and role, with the names' account and audience
- [x] 1.5 `identity_setup.provisioner_script`, `snowflake_access_helper` and `_leftover_grants` take the names; `snowflake_access.js` receives the analyst role, provisioner role and suffix as baked values
- [x] 1.6 `SnowflakeAdapter.check_scope` and `_grant` compare with the names; the CLI messages in `deploy/snowflake.py`, `deploy/run.py` and `cli/commands/deploy.py` name them
- [x] 1.7 Tests: the fixed names equal the literals on `release/0.1.1` for several projects and environments, and every caller reads them from the function (a test that greps the runtime packages for the old literals)

## 2. Terraform

- [x] 2.1 `terraform/snowflake`: role names and the loader user's name become variables, with `local.roles` a map keyed by purpose; every object keeps the `vecten <project> <env>` comment
- [x] 2.2 The scaffolded Snowflake thin root declares the new variables; `deploy` passes them with `-var` beside `database_name` and `warehouse_name`
- [x] 2.3 `terraform test`: fixed names give today's resource names; a shared-account run gives the suffixed names; two runs with different environments share no name
- [x] 2.4 The rendered-root static test counts the new variables

## 3. The layout setting and the account per environment

- [x] 3.1 `[warehouse] shared_account` in `vecten.toml` (default `false`), refused without the `platform` component; `[environments.<env>] snowflake_shared_account` recorded by `deploy`
- [x] 3.2 `deploy` refuses an environment whose recorded layout differs from the setting, except a fixed environment alone in its account
- [x] 3.3 `check_tfvars`: the account comparison applies to `prod` only; fixed environments refuse a shared account; shared environments refuse a hyphen in the environment name, a suffix over 64 characters, and an account a fixed environment names
- [x] 3.4 `generate_compose` writes the environment's account into every deployed host's Compose environment; the runtime reads it; the laptop is unchanged
- [x] 3.5 `next` asks `snowflake_shared_account` after the Snowflake account; `answer` accepts it; `init --deploy` writes it
- [x] 3.6 Tests: config validation, the record and its refusal, each `check_tfvars` rule, the Compose environment per host, the `next` item and its order

## 4. Identities in a shared account

- [x] 4.1 The provisioner script in the shared layout: suffixed roles and users, the authentication policy and `APPLY` on it, the environment's integration, comments on every object, and the stop on a foreign comment
- [x] 4.2 `READ_GRANT` refuses a target whose name does not end in the environment's suffix before the ownership check, which stays the guard; its TARGET pattern (`^[A-Z][A-Z0-9_]{0,62}$` today) must admit a suffixed group role, up to 63 characters before a suffix of up to 64
- [x] 4.3 `SnowflakeAdapter` appends the suffix when it names users and roles, strips it from `SHOW GRANTS` before registry lookups, sets the authentication policy on every user it creates, and refuses adoption in a shared account
- [x] 4.4 `check_scope` accepts `APPLY` on the environment's policy and nothing of another environment
- [x] 4.5 Tests: the script's statements for fixed and shared layouts (no fixed name in the shared script); `READ_GRANT` refusals; adapter names, adoption refusal and scope check against a mocked connector

## 5. The semantic model's database

- [ ] 5.1 The model loader reads a source naming the project's database as the environment's database, for source checks, compiled SQL, dataset grants and `describe`
- [ ] 5.2 Tests: one `osi/` loaded for two environments of a shared account; fixed names read as written; a source naming another database untouched

## 6. Destroy

- [ ] 6.1 With a shared account, `destroy` lists by ownership what stays outside Terraform; `--delete-data` refuses while the provisioner owns a user or role and prints the `DROP` statements
- [ ] 6.2 Tests: the listing from mocked `SHOW GRANTS`, no name-pattern match, the refusal and the printed statements

## 7. dbt and #53

- [x] 7.1 `vecten dbt --env` (#53, merged in #60) takes every connection name from `snowflake_names`
- [x] 7.2 Test: `profile_env` for a shared-account environment carries the suffixed user and role, the database and the warehouse

## 8. Docs

- [ ] 8.1 Runbook template: the Snowflake section per layout, the authentication policy, per-environment deploy users, data deletion's cleanup, and moving an environment into a shared account
- [ ] 8.2 `docs/mcp-identity.md`: per-environment users in a shared account, role-keyed policies, no adoption
- [ ] 8.3 `docs/GLOSSARY.md`: "Shared account" and "Environment suffix"
- [ ] 8.4 `CHANGELOG.md` under `[Unreleased]`

## 9. Proven only on a live Snowflake account (the `snowflake_live` tier and a sandbox deploy)

- [ ] 9.1 Two `CUSTOM` External OAuth integrations with different issuers coexist in one account, and each accepts only its own issuer's tokens
- [ ] 9.2 An authentication policy with `SECURITY_INTEGRATIONS` naming one integration refuses a token from the other for that user
- [ ] 9.3 A role holding `APPLY` on the policy and ownership of a user can set the policy on it, without `APPLY AUTHENTICATION POLICY` on the account
- [ ] 9.4 What `DROP DATABASE` does while users carry a policy that the database holds
- [ ] 9.5 An existing fixed-name sandbox environment plans empty after `vecten update`
- [ ] 9.6 Two environments in one account: `dev`'s loader key, provisioner key and MCP signing key each fail to read `prod`'s marts; `destroy --env dev --delete-data dev` leaves every `prod` object in place
