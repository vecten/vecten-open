## Why

A common need is a set of marts that only one group may read, such as finance figures. Today
that cannot be held on either warehouse. Issue #51 lists five problems. Each is restated here
as the cause the code shows on `release/0.1.1`:

1. **Default grants on `marts` come back.** Three places grant the default group's `analyst`
   role the `core` and `marts` schemas. The Snowflake module holds `USAGE` and future `SELECT`
   on `CORE` and `MARTS` as Terraform resources (`analyst_schema`, `analyst_future` in
   `terraform/snowflake/main.tf`). The provisioner script grants them again whenever an
   ACCOUNTADMIN reruns it. On Postgres, `warehouse_setup.sql` grants them on every ship. The
   MCP also records them as ordinary grants in `mcp_state` (`AccessService.seed_defaults`), so
   `vecten revoke --schema marts --group default` succeeds, and the next apply, rerun or ship
   undoes it. While `analyst` holds a future grant on `marts`, every rebuilt table in `marts` is
   readable by the default group again. That re-exposes a restricted mart without anyone seeing
   it.
2. **Rebuilt tables lose grants.** dbt rebuilds a table mart with `CREATE OR REPLACE` (rename
   and drop on Postgres). The new object carries none of the old one's grants, so a dataset
   grant is missing until the next reconciliation, up to fifteen minutes later.
3. **No separate schema is allowed.** The convention's naming test fails a model in any schema
   other than `staging`, `snapshots`, `core` and `marts`. The MCP's schema grants, `READ_GRANT`
   and the Postgres access helpers accept only the convention schemas.
4. **Existing roles cannot be adopted.** A group's role is created under the group's name and
   must be owned by the provisioner. `READ_GRANT` refuses any other target, so a group cannot
   use a role the client already runs. A group named like an existing role cannot be created.
5. **Grants cover tables and views only.** `READ_GRANT` grants on `TABLES` and `VIEWS`. Dynamic
   tables, materialized views, external tables and Iceberg tables in a granted schema stay
   unreadable.

The client's workaround has three parts: a dbt post-hook that revokes after every build,
per-dataset grants, and a check of the grants after every deploy. It is the P0 item. Each part
fails silently when it fails.

## What Changes

- **Restricted areas.** The convention gains named restricted areas, each a pair of schemas,
  `core_<area>` and `marts_<area>`. No role reads an area until an admin grants it to a group.
  The default group is never granted one by setup, and no future grant reaches an area except
  the ones made for granted groups. A model in an undeclared area fails the naming test. The
  convention moves to version 2, a superset of version 1.
- **Area access is declared in `vecten.toml`** (`[areas.<name>] access.groups`), and `next`
  seeds it the way it seeds connector access.
- **One owner for the default grants.** With the `mcp` component, neither the Snowflake module,
  nor the provisioner script, nor the Postgres setup SQL grants `analyst` anything on `core` or
  `marts` after this change. The MCP's state is the only owner, and its first reconciliation
  applies the seeded grants. Existing Terraform state forgets the two grant resources through
  `removed` blocks with `destroy = false`, so no grant is revoked by the upgrade. Without the
  `mcp` component the deploy keeps granting, as today.
- **Reconciliation converges in both directions.** For every role the MCP manages, it reads
  the read grants actually held inside the environment's database and revokes the ones the
  state does not hold. Access health lists every grant it added or removed. This replaces the
  client's "check after every deploy".
- **Rebuilds keep grants on Snowflake.** The scaffolded dbt project sets `+copy_grants: true`.
  A convention test refuses dbt `grants` config on any model. On Postgres, which has no
  equivalent, the MCP also reconciles after every green dbt build.
- **Read grants cover every relation kind.** Tables, views, materialized views, dynamic
  tables, external tables and Iceberg tables on Snowflake. On Postgres the helpers already
  cover tables, partitioned tables, views, materialized views and foreign tables.
- **A group can bind an existing role.** The group keeps its provisioner-owned role. An
  administrator grants the existing role to it with a statement the MCP prints, and the MCP
  records and checks the binding. The provisioner never owns or grants the existing role.
- **A group's role name can be chosen at creation**, so a group whose name matches an existing
  role can still be created.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp/access-control`: restricted areas, the single owner of default grants, two-way
  reconciliation, every relation kind, group role names and bound roles.
- `mcp/admin-api`: the group operations gain a role name and binding.
- `shared/schema-convention`: version 2 with restricted areas, and a test that refuses dbt
  `grants`.
- `shared/identity-contract`: the bounds of `READ_GRANT`, the new report procedure, and the
  Postgres helpers' schemas; the `analyst` setup without data grants.
- `shared/deploy`: who grants `analyst` its default reads, and the Terraform state migration.
- `platform/dbt-modelling`: the layout with areas, and `copy_grants` on Snowflake.

## Impact

- Code: `vecten_mcp.access`, `vecten_mcp.warehouse.snowflake`, `vecten_mcp.warehouse.postgres`,
  `vecten_mcp.state`, the admin API, the panel and `vecten group`/`vecten grant`;
  `vecten_shared.identity_setup`, `vecten_shared/sql/snowflake_access.js`,
  `vecten_shared/sql/warehouse_setup.sql`, `vecten_shared.osi.generate`, `vecten_shared.config`;
  the `vecten_convention` dbt package; `vecten.next`, `vecten.mcp_onboarding`; the scaffolded
  `dbt_project.yml`; the `model` skill.
- Terraform: `terraform/snowflake/main.tf`, which gains `removed` blocks and grants that apply
  only without the MCP.
- Operations: every Snowflake deployment with the MCP needs the provisioner script rerun by an
  ACCOUNTADMIN, because `READ_GRANT` changes and a report procedure is added. Every Postgres
  deployment gets the new helpers on its next ship.
- Docs: `docs/GLOSSARY.md` ("Restricted area", "Mart"), `docs/mcp-identity.md`, the rendered
  `docs/schema-convention.md` and runbook.
- Interacts with #34 (environment-scoped role names), #4 (one reader role per person) and
  PR #60 (`vecten dbt --env`); see design.md, "Interactions".
