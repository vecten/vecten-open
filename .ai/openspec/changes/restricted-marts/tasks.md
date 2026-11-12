Tiers: **unit** (no services), **docker**, **terraform**, **live** (`snowflake_live`, maintainers
only), **deploy** (a real deploy on a maintainer sandbox before release). Each section is
meant as one pull request, in this order. Sections 1 and 2 are the client's stage 1 and can
ship before areas.

## 0. Settle what the design could not verify (before section 1)

- [x] 0.1 terraform: whether a `removed` block with `destroy = false` works in the fetched child module, or must sit in the thin root with a `module.` address; otherwise use the `terraform state rm` fallback of Decision 4
- [ ] 0.2 live: which `SHOW TERSE OBJECTS` or `INFORMATION_SCHEMA.TABLES` value names a materialized view, a dynamic table, an external table and an Iceberg table
- [ ] 0.3 live: `CREATE OR REPLACE … COPY GRANTS` keeps a per-object grant and skips the schema's future grants for a table, a view and a dynamic table; and whether dbt-snowflake 1.12 adds `COPY GRANTS` on `--full-refresh` of an incremental model
- [x] 0.4 unit: whether fakesnow 0.11 accepts `COPY GRANTS`
- [ ] 0.5 live: an owner's-rights JavaScript procedure can run `SHOW GRANTS TO ROLE` and `SHOW FUTURE GRANTS IN SCHEMA` for a role it does not own

## 1. One owner for the default grants; reconciliation converges

- [x] 1.1 `terraform/snowflake/main.tf`: `removed` blocks for `analyst_schema` and `analyst_future`; `analyst_default_schema` and `analyst_default_future` only when `mcp` is not a component; ownership transfer ordering unchanged
- [x] 1.2 terraform: `terraform test` cases: with the MCP, no `ANALYST` grant resource; without it, the same grants as before; a state holding the old addresses plans no destroy
- [ ] 1.3 `identity_setup.provisioner_script`: Snowflake grants `ANALYST` only database and warehouse `USAGE`; Postgres grants `analyst` only `CONNECT`
- [ ] 1.4 `warehouse_setup.sql`: the `analyst` `USAGE` and default privileges run only when `with_mcp` is false; `vm/apply.py` and its byte-identical copy unchanged
- [ ] 1.5 `snowflake_access.js`: a version constant; new `READ_GRANTS(TARGET)` returning the target's read grants and schema-level future grants in the database, with the same target bounds as `READ_GRANT`; `READER` added to the refused targets
- [ ] 1.6 `AccessService.reconcile`: read actual grants per managed role (Snowflake through `READ_GRANTS`, Postgres from `relacl`, `nspacl` and `pg_default_acl`), revoke undesired ones in scope, honour `reconcile_drift`, log each, and record added, revoked and drift lists in access health
- [ ] 1.7 `check_scope` accepts USAGE on `READ_GRANTS`; the startup check compares the procedure's version and limits features when it is older
- [ ] 1.8 `next` reports the provisioner-script rerun while the installed version is older
- [ ] 1.9 unit: setup SQL and script text for both warehouses with and without the MCP; reconciliation against a fake adapter (revoke of an undesired schema grant, untouched grant outside scope, report mode, revokes before grants); `READ_GRANTS` bounds
- [ ] 1.10 docker: Postgres stack: revoke `marts` from default, ship again, `analyst` holds nothing on `marts`; a hand grant in `marts` is revoked; a grant in another schema stays
- [ ] 1.11 live: revoke `marts` from default, `deploy`, rerun the script, rebuild a mart: `ANALYST` cannot read it
- [ ] 1.12 deploy: upgrade a sandbox deployed with the previous release: the plan revokes nothing; the first reconciliation's access health is as expected
- [ ] 1.13 Changelog: the upgrade steps and the warning about hand-made grants

## 2. Grants survive rebuilds

- [ ] 2.1 Scaffold: `+copy_grants: true` in the Snowflake `dbt_project.yml`; `vecten update` merges it into existing projects; emulation profile per 0.4
- [ ] 2.2 `vecten_convention`: `convention_no_grants` test (any node with `grants`; on Snowflake any model with `copy_grants` false)
- [ ] 2.3 dbt DAG: write a finish time beside the built commit; the MCP reconciles when it changes (Postgres; harmless on Snowflake)
- [ ] 2.4 unit: scaffold render and update merge; the convention test's failures and names; the reconcile trigger
- [ ] 2.5 docker: Postgres dataset grant restored after a dbt rebuild, by the triggered reconciliation
- [ ] 2.6 live: dataset grant on a mart survives `dbt build` on Snowflake with no reconciliation running

## 3. Restricted areas

- [ ] 3.1 `vecten_shared.config`: `[areas.<name>]` with `access.groups`; name rule `[a-z][a-z0-9]*`; refuse names colliding with a connector's `raw_` source
- [ ] 3.2 `vecten_convention`: naming, description and primary-key tests treat `core_<area>` as core and `marts_<area>` as marts; the naming test fails undeclared areas; the declared list arrives as a dbt var from `vecten-platform`
- [ ] 3.3 `vecten_shared.osi.generate`: marts in `marts_<area>` become datasets; `vecten.next.modelling` described layers include areas
- [ ] 3.4 `docs/schema-convention.md` template: version 2 and the area rules; the `model` skill's rule for `core_<area>`; then `uv run vecten dev render-plugin --repo .` and commit the plugin and `docs/agent-guide.md`
- [ ] 3.5 MCP: `AccessService` area grants (kind `area`, two schemas, applied and revoked together), `_source` accepting declared area schemas; admin API, panel and `vecten grant --area`
- [ ] 3.6 `snowflake_access.js`: area pattern; Postgres helper installer pattern; setup SQL creates declared areas' schemas as `loader` (host config carries the list)
- [ ] 3.7 `next`: `ask` for missing `access.groups`; post-deploy `command` item seeding the grant; report relations in convention schemas and areas that no model builds
- [ ] 3.8 unit: config validation; convention tests on fixture projects (version-1 project passes, undeclared area fails, area mart becomes a dataset); MCP area grant translation; `next` items
- [ ] 3.9 docker: Postgres: area granted to `finance`, rebuilt mart readable by `finance` and refused for default, with no reconciliation in between
- [ ] 3.10 live: the same on Snowflake, plus an area built before any grant is unreadable by every group role
- [ ] 3.11 deploy: declare an area, ship, grant, rebuild on a sandbox of each cloud
- [ ] 3.12 Glossary: "Restricted area"; "Mart" covers `marts_<area>`

## 4. Every relation kind

- [ ] 4.1 `snowflake_access.js`: per-kind `ALL`/`FUTURE` grants and revokes; single-relation kind from 0.2; `READ_GRANTS` covers the same kinds
- [ ] 4.2 `check_scope` and the Postgres adapter's relation lookup unchanged except where 4.1 needs
- [ ] 4.3 unit: generated statements per kind; refusal surfaced in access health
- [ ] 4.4 live: a dynamic table and a materialized view in a granted schema are readable; a dataset grant on each works

## 5. Group roles: chosen names and bound roles

- [ ] 5.1 `AccessService.create_group(name, role=None)`: override, reserved prefixes, refusal on an existing role naming both ways out
- [ ] 5.2 Binding: state for bindings; admin API bind (prints the statement), confirm (checks the grant through `SHOW GRANTS TO ROLE <group role>` or `pg_auth_members`), unbind; access health for missing bindings; delete group leaves the bound role
- [ ] 5.3 Panel and `vecten group create --role`, `vecten group bind|unbind`
- [ ] 5.4 Under #34's shared layout (if merged first): refuse a bound role holding privileges on another environment's database of the project
- [ ] 5.5 `docs/mcp-identity.md`: bound roles, `IS_ROLE_IN_SESSION` rather than `CURRENT_ROLE()`
- [ ] 5.6 unit: name override and refusals; bind, confirm and unbind flows against fake adapters
- [ ] 5.7 docker: Postgres bound role: a group member reads through it; the provisioner holds no membership in it
- [ ] 5.8 live: Snowflake bound role: a member reads through it; `SHOW GRANTS TO ROLE PROVISIONER` unchanged; the startup check passes

## 6. Release checks

- [ ] 6.1 deploy: full stage-1 and stage-2 migration rehearsal on a sandbox seeded like the client (revoked `marts`, post-hooks, per-mart grants), following design.md's migration plan to the letter
- [ ] 6.2 Runbook: the migration plan, the old-relation drop statements, the provisioner-script rerun
