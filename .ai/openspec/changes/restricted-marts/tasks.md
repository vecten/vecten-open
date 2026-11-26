Tiers: **unit** (no services), **docker**, **terraform**, **live** (`snowflake_live`, maintainers
only), **deploy** (a real deploy on a maintainer sandbox before release). Each section is
meant as one pull request, in this order. Sections 1 and 2 are the client's stage 1 and can
ship before areas.

## 0. Settle what the design could not verify (before section 1)

- [x] 0.1 terraform: whether a `removed` block with `destroy = false` works in the fetched child module, or must sit in the thin root with a `module.` address; otherwise use the `terraform state rm` fallback of Decision 4
- [ ] 0.2 live: which `SHOW TERSE OBJECTS` or `INFORMATION_SCHEMA.TABLES` value names a materialized view, a dynamic table, an external table and an Iceberg table
  - Partly run live for 0.1.1 (rc2, AWS): table, view, dynamic table and Iceberg table named. A materialized view not run: the sandbox account is Standard edition, and materialized views need Enterprise. An external table needs a stage and was not tried.
  - Partly run live for 0.1.2 (rc3, AWS): an external table is `EXTERNAL TABLE` in `INFORMATION_SCHEMA.TABLES.TABLE_TYPE` and `TABLE` in `SHOW TERSE OBJECTS`. A materialized view still needs an Enterprise account.
- [x] 0.3 live: `CREATE OR REPLACE … COPY GRANTS` keeps a per-object grant and skips the schema's future grants for a table, a view and a dynamic table; and whether dbt-snowflake 1.12 adds `COPY GRANTS` on `--full-refresh` of an incremental model
  - Live: `COPY GRANTS` kept the per-object grant and skipped the future grants for a table, a view and a dynamic table. dbt-snowflake 1.12 emits `copy grants` when `copy_grants` is set, read from its macros.
- [x] 0.4 unit: whether fakesnow 0.11 accepts `COPY GRANTS`
- [x] 0.5 live: an owner's-rights JavaScript procedure can run `SHOW GRANTS TO ROLE` and `SHOW FUTURE GRANTS IN SCHEMA` for a role it does not own
  - Live, 0.1.1rc2 on AWS: the owner's-rights procedure read both listings for a role it does not own.

## 1. One owner for the default grants; reconciliation converges

- [x] 1.1 `terraform/snowflake/main.tf`: `removed` blocks for `analyst_schema` and `analyst_future`; `analyst_default_schema` and `analyst_default_future` only when `mcp` is not a component; ownership transfer ordering unchanged
- [x] 1.2 terraform: `terraform test` cases: with the MCP, no `ANALYST` grant resource; without it, the same grants as before; a state holding the old addresses plans no destroy
- [x] 1.3 `identity_setup.provisioner_script`: Snowflake grants `ANALYST` only database and warehouse `USAGE`; Postgres grants `analyst` only `CONNECT`
- [x] 1.4 `warehouse_setup.sql`: the `analyst` `USAGE` and default privileges run only when `with_mcp` is false; `vm/apply.py` and its byte-identical copy unchanged
- [x] 1.5 `snowflake_access.js`: a version constant; new `READ_GRANTS(TARGET)` returning the target's read grants and schema-level future grants in the database, with the same target bounds as `READ_GRANT`; `READER` added to the refused targets
- [x] 1.6 `AccessService.reconcile`: read actual grants per managed role (Snowflake through `READ_GRANTS`, Postgres from `relacl`, `nspacl` and `pg_default_acl`), revoke undesired ones in scope, honour `reconcile_drift`, log each, and record added, revoked and drift lists in access health
- [x] 1.7 `check_scope` accepts USAGE on `READ_GRANTS`; the startup check compares the procedure's version and limits features when it is older
- [x] 1.8 `next` reports the provisioner-script rerun while the installed version is older. `--confirm-run` on Snowflake records `access_version` in the environment's facts and the run record; `next` reports `vecten mcp provisioner-script --env <env>` after deploy (platform and MCP-only) and in the MCP-only local loop while the recorded version is missing or below `ACCESS_VERSION`; `vecten update` needs no change, the comparison is against the installed CLI. Delta: `specs/cli/agent-guide`. Unit: `test_next_deploy`, `test_next_project`, `test_mcp_onboarding`
- [x] 1.9 unit: setup SQL and script text for both warehouses with and without the MCP; reconciliation against a fake adapter (revoke of an undesired schema grant, untouched grant outside scope, report mode, revokes before grants); `READ_GRANTS` bounds
- [x] 1.10 docker: Postgres stack: revoke `marts` from default, ship again, `analyst` holds nothing on `marts`; a hand grant in `marts` is revoked; a grant in another schema stays
- [x] 1.11 live: revoke `marts` from default, `deploy`, rerun the script, rebuild a mart: `ANALYST` cannot read it
  - Live, 0.1.1rc1: after the rerun and a rebuild, `ANALYST` could not read the mart.
- [x] 1.12 deploy: upgrade a sandbox deployed with the previous release: the plan revokes nothing; the first reconciliation's access health is as expected
  - Live, 0.1.1rc1: the plan revoked nothing; the first reconciliation listed the expected drift and revoked nothing.
- [x] 1.13 Changelog: the upgrade steps and the warning about hand-made grants
- [x] 1.14 `next` reports a deployed environment's post-deploy items after the local loop's items while those are outstanding, so the rerun of 1.8 reaches a clone that never ran the local stack; an environment not yet deployed still waits. Delta: `specs/cli/agent-guide`. Unit: `test_next_promotion`

## 2. Grants survive rebuilds

- [x] 2.1 Scaffold: `+copy_grants: true` in the Snowflake `dbt_project.yml`; `vecten update` merges it into existing projects; emulation profile per 0.4
- [x] 2.2 `vecten_convention`: `convention_no_grants` test (any node with `grants`; on Snowflake any model with `copy_grants` false)
- [x] 2.3 dbt DAG: write a finish time beside the built commit; the MCP reconciles when it changes (Postgres; harmless on Snowflake)
- [x] 2.4 unit: scaffold render and update merge; the convention test's failures and names; the reconcile trigger
- [x] 2.5 docker: Postgres dataset grant restored after a dbt rebuild, by the triggered reconciliation
- [x] 2.6 live: dataset grant on a mart survives `dbt build` on Snowflake with no reconciliation running
  - Live, 0.1.1rc2 on AWS: the dataset grant survived `dbt build` with no reconciliation running.

## 3. Restricted areas

- [x] 3.1 `vecten_shared.config`: `[areas.<name>]` with `access.groups`; name rule `[a-z][a-z0-9]*`; refuse names colliding with a connector's `raw_` source
- [x] 3.2 `vecten_convention`: naming, description and primary-key tests treat `core_<area>` as core and `marts_<area>` as marts; the naming test fails undeclared areas; the declared list arrives as a dbt var from `vecten-platform`
- [x] 3.3 `vecten_shared.osi.generate`: marts in `marts_<area>` become datasets; `vecten.next.modelling` described layers include areas
- [x] 3.4 `docs/schema-convention.md` template: version 2 and the area rules; the `model` skill's rule for `core_<area>`; then `uv run vecten dev render-plugin --repo .` and commit the plugin and `docs/agent-guide.md`
- [x] 3.5 MCP: `AccessService` area grants (kind `area`, two schemas, applied and revoked together), `_source` accepting declared area schemas; admin API, panel and `vecten grant --area`
- [x] 3.6 `snowflake_access.js`: area pattern; Postgres helper installer pattern; setup SQL creates declared areas' schemas as `loader` (host config carries the list)
- [x] 3.7 `next`: `ask` for missing `access.groups`; post-deploy `command` item seeding the grant; report relations in convention schemas and areas that no model builds. The report is access health's, not `next`'s (Decision 12): the dbt DAG writes `platform/dbt/built_relations.json` after a full green build, the MCP host's bucket policy reads it, and reconciliation lists each relation in `core`, `marts` and declared areas the record lacks, with its `DROP`; `vecten issues` shows them. Snowflake lists through `READ_GRANTS` with no target, procedures version 6 (`GRANTS_VERSION` 5 keeps areas and drift). Unit: `test_dbt_dag_tasks`, `test_osi_revision`, `test_snowflake_access`, `test_warehouse_read_grants`, `test_access_unbuilt`, `test_issues_adapters`, `test_scaffold_render`; terraform: `scope.tftest.hcl` on both clouds
- [x] 3.8 unit: config validation; convention tests on fixture projects (version-1 project passes, undeclared area fails, area mart becomes a dataset); MCP area grant translation; `next` items
- [x] 3.9 docker: Postgres: area granted to `finance`, rebuilt mart readable by `finance` and refused for default, with no reconciliation in between
- [x] 3.10 live: the same on Snowflake, plus an area built before any grant is unreadable by every group role
  - Live, 0.1.1rc2 on AWS: an area built before any grant was unreadable by every group role.
- [ ] 3.11 deploy: declare an area, ship, grant, rebuild on a sandbox of each cloud
  - Partly run live for 0.1.1 (rc2, AWS): declare, ship, grant and rebuild on AWS with Snowflake. Scaleway not run.
  - Partly run live for 0.1.2 (rc3, AWS): an area with restricted source layers declared, shipped and built; its grant reached `staging_`, `snapshots_` and `core_<area>` with future grants, and its revoke left none. Scaleway not run (sign-in).
- [x] 3.13 A marts-only area ([#94](https://github.com/vecten/vecten-open/issues/94)): an area grant covers the area schemas the warehouse finds and names the others, which reconciliation grants once found; the applied record keeps the granted schemas, and a revoke or rollback revokes only those. On a grant `READ_GRANT` answers `absent` when `INFORMATION_SCHEMA` does not list the schema to its owner; a revoke never does, so the access procedures are version 5. The Postgres adapter reads `pg_namespace` and `pg_class`, so a revoke on an object that is gone there is done. Unit: fake adapters, the adapters over recorded answers, the procedure under Node
  - Live, 2026-11-20 (#116, #117): a restricted procedure owner saw neither the schema nor the reader's SELECT that an administrator saw. UNDROP restored table SELECT and schema USAGE, current SELECT and future SELECT. The version 7 procedure kept the refused revokes pending, and retrying after restoration removed the grants.
- [x] 3.14 live: `vecten grant --area` on a Snowflake area with `MARTS_<AREA>` only; then dbt creates `CORE_<AREA>` and the next reconciliation grants it
  - Live, 0.1.1rc2 on AWS: `grant --area` waited for `core_<area>`, which the next reconciliation granted once dbt created it.
- [ ] 3.15 live: `READ_GRANTS` with no target names a view, a materialized view, a dynamic table and an Iceberg table with the type its `DROP` takes, and lists the relations `LOADER` owns in `MARTS` and an area
  - Partly run live for 0.1.1 (rc2, AWS): a view, a dynamic table, an Iceberg table and a table listed with the type their `DROP` takes, among the relations `LOADER` owns. A materialized view not run: the sandbox account is Standard edition, and materialized views need Enterprise.
- [x] 3.16 docker: Postgres: a relation no model builds is listed once a full build is recorded, not after the admin drops it, and not after a later narrowed build (`test_postgres_default_grants_docker`)
- [x] 3.12 Glossary: "Restricted area"; "Mart" covers `marts_<area>`

## 4. Every relation kind

- [x] 4.1 `snowflake_access.js`: per-kind `ALL`/`FUTURE` grants and revokes; single-relation kind from 0.2; `READ_GRANTS` covers the same kinds
- [x] 4.2 `check_scope` and the Postgres adapter's relation lookup unchanged except where 4.1 needs
- [x] 4.3 unit: generated statements per kind; refusal surfaced in access health
- [ ] 4.4 live: a dynamic table and a materialized view in a granted schema are readable; a dataset grant on each works, and its revoke removes it (read back with `SHOW GRANTS TO ROLE`)
  - Partly run live for 0.1.1 (rc2, AWS): a dynamic table. A materialized view not run: the sandbox account is Standard edition, and materialized views need Enterprise.

## 5. Group roles: chosen names and bound roles

- [x] 5.1 `AccessService.create_group(name, role=None)`: override, reserved prefixes, refusal on an existing role naming both ways out
- [x] 5.2 Binding: state for bindings; admin API bind (prints the statement), confirm (checks the grant through `SHOW GRANTS TO ROLE <group role>` or `pg_auth_members`), unbind; access health for missing bindings; delete group leaves the bound role
- [x] 5.3 Panel and `vecten group add --role`, `vecten group bind|unbind`
- [x] 5.4 Under #34's shared layout (if merged first): refuse a bound role holding privileges on another environment's database of the project
- [x] 5.5 `docs/mcp-identity.md`: bound roles, `IS_ROLE_IN_SESSION` rather than `CURRENT_ROLE()`
- [x] 5.6 unit: name override and refusals; bind, confirm and unbind flows against fake adapters
- [x] 5.7 docker: Postgres bound role: a group member reads through it; the provisioner holds no membership in it
- [x] 5.8 live: Snowflake bound role: a member reads through it; `SHOW GRANTS TO ROLE PROVISIONER` unchanged; the startup check passes
  - Partly run live for 0.1.1 (rc2, AWS): binding, review query, no new provisioner grant, reconciliation leaves the role alone, the startup check passes. A member reading through the role not run.
  - Live, 0.1.2rc3 on AWS with a shared Snowflake account: a member read a table through a bound role confirmed in the admin panel; `SHOW GRANTS TO ROLE PROVISIONER_…` kept every earlier grant and gained none on the bound role; the MCP restarted healthy with the binding in place. The CLI's bind and confirm time out on this account (#202).

## 6. Release checks

- [x] 6.1 deploy: full stage-1 and stage-2 migration rehearsal on a sandbox seeded like the client (revoked `marts`, post-hooks, per-mart grants), following design.md's migration plan to the letter
  - Live, 0.1.1rc1: both stages followed design.md's plan on a sandbox seeded like the client; findings are in the live-check results.
- [x] 6.2 Runbook: the migration plan, the old-relation drop statements, the provisioner-script rerun (runbook "Upgrading this project" and "Moving restricted data into an area", following the 0.1.1rc1 rehearsal)
