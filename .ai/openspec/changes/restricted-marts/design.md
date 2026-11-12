## Context

### How a person comes to read a table today

A person's warehouse user holds the roles of the person's groups. The default group maps to
`analyst`; every other group maps to a role the provisioner created under the group's name and
owns. A group can be granted two kinds of thing, both stored in `mcp_state` as desired state:

- **A schema grant** (`vecten grant --schema marts --group sales`): `USAGE` on the schema,
  `SELECT` on every existing table and view, and a future grant, so tables created later are
  covered.
- **A dataset grant** (`vecten grant orders --group sales`): `SELECT` on the one relation that
  is the dataset's source.

The MCP applies grants through administrator-installed helpers, so its own credential never
holds the right to grant. On Snowflake this is the `VECTEN_ACCESS.READ_GRANT` procedure, owned
by whoever ran the provisioner script (ACCOUNTADMIN) and run with the owner's rights. On
Postgres it is one `SECURITY DEFINER` function per schema and owner. Reconciliation runs every
fifteen minutes and on every model swap. It re-applies every desired grant and revokes the
grants it applied earlier that are no longer desired. It never looks at grants it did not
apply itself.

### The five problems, as the code shows them

| # | Problem | Cause on `release/0.1.1` |
|---|---|---|
| 1 | A revoke of `marts` from the default group does not hold | Three owners besides the MCP grant `analyst` its reads on `core` and `marts`. In `terraform/snowflake/main.tf`, `analyst_schema` (`USAGE`) and `analyst_future` (future `SELECT` on tables and views) are Terraform resources, and the next plan restores a revoked grant as drift. `identity_setup.provisioner_script` grants `USAGE` and `SELECT` on all and future tables whenever an ACCOUNTADMIN reruns it. `warehouse_setup.sql` grants `USAGE` and default privileges on every ship. `AccessService.seed_defaults` also records both schemas as ordinary grants of the default group, so the revoke is accepted and later undone. |
| 2 | Rebuilt tables lose dataset grants | dbt's table materialization replaces the relation: `CREATE OR REPLACE` on Snowflake, create-then-rename on Postgres. The scaffold does not set `copy_grants`, so Snowflake drops the old object's grants. A dataset grant is missing until the next reconciliation. A *schema* grant survives, because the new object receives the schema's future grants. |
| 3 | No separate schema is allowed | `convention_model_naming.sql` reports any model outside `staging`, `snapshots`, `core` and `marts`. `AccessService._source`, `snowflake_access.js` and the Postgres helper installer all accept only the convention schemas. |
| 4 | Existing roles cannot be adopted | `AccessService.create_group` creates the role under the group's name. `READ_GRANT` accepts as target only `ANALYST` or a role `PROVISIONER` owns. |
| 5 | Only tables and views | `READ_GRANT` loops over `TABLES` and `VIEWS`. For a single relation it chooses between `TABLE` and `VIEW`. |

The key fact behind problem 1 is that Snowflake has no "future grant except one table". While
`analyst` holds a future grant in `marts`, every table dbt creates or replaces there is
readable by the default group. That holds whether or not an earlier revoke removed the grant
from the previous object. Postgres default privileges behave the same way.

### Snowflake behaviour this design relies on

Each item was checked in the documentation on 2026-11-11. Items not verified are listed under
"Not verified".

- `CREATE OR REPLACE TABLE … COPY GRANTS` "copies all privileges, except OWNERSHIP, from the
  existing table to the new table", atomically in the same statement. With it, "the new table
  does not inherit any future grants defined for the object type in the schema". Without it,
  the new table does inherit them. Views behave the same way.
  ([CREATE TABLE](https://docs.snowflake.com/en/sql-reference/sql/create-table),
  [CREATE VIEW](https://docs.snowflake.com/en/sql-reference/sql/create-view))
- dbt-snowflake's `copy_grants: true` adds `COPY GRANTS` when it rebuilds a table, view or
  dynamic table; the default is `false`.
  ([dbt Snowflake configs](https://docs.getdbt.com/reference/resource-configs/snowflake-configs#copying-grants))
- When future grants exist on the same object type at both database and schema level, "the
  schema-level grants take precedence over the database level grants".
  ([GRANT <privileges>](https://docs.snowflake.com/en/sql-reference/sql/grant-privilege))
- Future and bulk grants exist for dynamic tables, external tables, Iceberg tables,
  materialized views and semantic views (same page).
- In a managed access schema "object owners lose the ability to make grant decisions"; only the
  schema owner or a role with `MANAGE GRANTS` can grant, future grants included.
  ([Access control configuration](https://docs.snowflake.com/en/user-guide/security-access-control-configure))
- "A role owner … does not inherit the privileges of the owned role."
  ([Access control overview](https://docs.snowflake.com/en/user-guide/security-access-control-overview))
- Granting a role to a user or a role needs `OWNERSHIP` of the role or `MANAGE GRANTS`.
  ([GRANT ROLE](https://docs.snowflake.com/en/sql-reference/sql/grant-role))
- Database roles cannot be activated in a session, and they can receive future grants (the
  overview page and GRANT <privileges>).

## Goals / Non-Goals

**Goals:**

- A group can read a set of marts, and the core models they are built from, that the default
  group cannot. No deploy, ship, script rerun or dbt rebuild can re-expose them.
- The restriction holds because of how the grants are laid out. Repeated re-application is
  not what keeps it in place, and every failure mode leaves data closed rather than open.
- A revoke recorded in `mcp_state` holds, for the default group too.
- Dataset grants survive rebuilds on Snowflake.
- A group can use a role the client already has.
- Read grants cover every relation kind a mart can be.
- Each part of the client's workaround becomes unnecessary, with a stated way off it.
- Postgres gets the same guarantees wherever its mechanisms allow, and the gaps are named.

**Non-Goals:**

- Hiding the *metadata* of datasets a person cannot read. `describe` and `search` still list
  every dataset in the model. Filtering them by the person's grants is a separate change
  (risk 3).
- Stopping the project's own dbt code from copying restricted data into an unrestricted
  schema. `LOADER` writes every schema, so code review stays the control for the dbt project.
- Write privileges, functions, stages and streams.
- A read-only identity for `vecten dbt --env` (Decision 11 sketches it; it is not part of
  this change).
- Row-level and column-level policies. They stay the warehouse's, as today.

## Decisions

Every decision was accepted by the maintainer on 2026-11-12, each as recommended. They are
ordered by consequence and by how hard they are to
reverse, the first being the hardest. Where a decision changes Postgres, it says so.

### Decision 1: Where restricted data lives (Accepted)

The issue offers three ways to restrict. A fourth falls out of Decisions 4 to 6 and is listed
because it is the fastest interim.

| Option | Consequences |
|---|---|
| A. The MCP owns the default grants after first setup, and nothing else changes | Necessary: Decision 4 does it. Not sufficient on its own. To hide one mart, the default group must lose `marts` as a whole and be granted every other mart one by one. Each new mart is then invisible to it until an admin grants it, and on Postgres each rebuild drops the per-mart grants until reconciliation. |
| B. A restricted flag on a dataset, with the mart staying in `marts` | While `analyst` holds a future grant in `marts`, a flagged mart is readable from its first build. A revoke after the build can be made to stick only with `COPY GRANTS`, which suppresses future grants on replacement. But any new object gets the future grant again: the first build, a change between view and table, a rename, or a drop and rebuild. That fails open. Without the future grant it reduces to option D. The flag would also be access policy written into the Ossie model, which "Grants and deny lists live in the state database" forbids. |
| **C. Restricted areas: declared schemas `core_<area>` and `marts_<area>` that no role reads until granted** | No role holds any grant in a new area. The first group granted it gets `USAGE`, `SELECT` on all and a schema-level future grant, so rebuilds are covered by how the grants are laid out. The default group keeps `marts` whole, and new unrestricted marts are visible at once as today. Every failure is closed: an undeclared or ungranted area is unreadable, and so is a schema dbt creates before anyone grants it. Works the same on Postgres, where default privileges are per schema. Costs a convention change (Decision 3), one-time moves of existing models, and longer schema names in agent SQL. |
| D. The default group reads marts through per-dataset grants, with `COPY GRANTS` (Decisions 4 to 6 without areas) | Fails closed and needs no convention change. But it is an allow-list the admin must extend for every new mart. Core stays readable, so it is not a full restriction (Decision 2). On Postgres per-object grants vanish at each rebuild until reconciliation. |

**Accepted: C, built on A.** Option D works as soon as Decisions 4 to 6 ship. The client
can use it as a first stage before areas land (migration plan).

### Decision 2: What an area covers (Accepted)

A restricted mart is built from core models. On `release/0.1.1` the default group reads all of
`core`, so `invoice` in `core` exposes what `finance_revenue` in a restricted schema
aggregates. `raw_<source>`, `staging` and `snapshots` are already granted to no group by
default.

| Option | Consequences |
|---|---|
| A. `marts_<area>` only | One schema per area. The core models behind it stay readable by the default group through `sql-exec`, so the restriction is cosmetic for the facts it was meant to hide. |
| **B. `core_<area>` and `marts_<area>`, granted together** | An area grant covers both schemas. A core model that only the area needs goes to `core_<area>`. Core models shared with unrestricted marts stay in `core`, which is the modeller's call, made in the `model` skill. Two schemas per area, and either may be empty. |
| C. One schema per area holding both layers | Breaks the layer rules: the description, primary-key and dataset tests key on the layer, which would then need a model-level marker. |

**Accepted: B.** The `model` skill states the rule: a core model whose rows the default
group must not read goes to `core_<area>`.

### Decision 3: How areas are declared, and what the convention says (Accepted)

| Option | Consequences |
|---|---|
| A. Any schema matching `(core\|marts)_<name>` is an area, with no declaration | Nothing to keep in step. A typo creates a new area, closed, so it fails safe, but `next` cannot ask who may read it. |
| **B. Declared in `vecten.toml` as `[areas.<name>]` with `access.groups`, with the pattern enforced everywhere** | Mirrors connector access ("Connector access is declared when the connector is added"). `next` asks for `access.groups` when it is absent, and seeds the area grant after deploy with `vecten grant --area <name>`. Once recorded in `mcp_state` the grant is owned there, so a later panel revoke stands. The naming test reads the declared names from a dbt var that `vecten-platform` passes. A model in `marts_x` with no `[areas.x]` fails the build, naming the area and the `vecten.toml` entry. |
| C. Declared in dbt only (folder `+schema`) | dbt knows, but the MCP and `next` read `vecten.toml`, not the dbt project. |

**Accepted: B.** An area name is `[a-z][a-z0-9]*`, with no underscore, so
`marts_<area>` parses one way. The administrator-installed bounds use the *pattern*, not the
list: `READ_GRANT` accepts `^(CORE|MARTS)_[A-Z][A-Z0-9]*$`, and the Postgres helper installer
covers `^(core|marts)_[a-z][a-z0-9]*$`. Declaring an area therefore needs no script rerun on
Snowflake. The MCP's own check uses the declared list.

The convention becomes **version 2**, a superset of version 1, so a version-1 project passes
unchanged. Areas follow their layer's rules: `marts_<area>` models are marts (domain-prefixed
names, descriptions, a primary key, one generated dataset each) and `core_<area>` models are
core (descriptions, primary key). Areas are created by dbt as `LOADER`, like the convention
schemas the platform does not pre-create. The Snowflake module does not manage them
(Decision 10). On Postgres the next ship's setup SQL creates each declared area's schemas as
`loader` before it installs helpers, so the helpers exist before the first grant.

### Decision 4: One owner for the default grants (Accepted)

| Option | Consequences |
|---|---|
| A. The deploy owns them, and the MCP refuses to revoke `core` or `marts` from the default group | Honest and small. No Terraform state change. The default group can never be narrowed. |
| **B. With the `mcp` component, the MCP owns them. Without it, the deploy does** | The Snowflake module grants `analyst` its reads only when `mcp` is not a component. The provisioner script grants `analyst` only database and warehouse `USAGE`. `warehouse_setup.sql` grants `analyst` only when `with_mcp` is false. `seed_defaults` keeps recording `core` and `marts` once, and the first reconciliation applies them through `READ_GRANT` or the helpers. A revoke then holds. |
| C. The deploy grants once and never re-asserts (provider `execute` resource or a marker in setup) | Two owners again, only further apart in time. The provider's `snowflake_execute` is a preview resource. |

**Accepted: B.** Terraform state of existing deployments holds `analyst_schema` and
`analyst_future`, and simply deleting them from the module would revoke the grants on the
next apply. The module therefore:

- declares `removed { from = snowflake_grant_privileges_to_account_role.analyst_schema
  lifecycle { destroy = false } }` and the same for `analyst_future`, so state forgets them and
  Snowflake keeps them;
- adds `analyst_default_schema` and `analyst_default_future` with
  `for_each = local.with_mcp ? {} : …`. On a platform-only deployment they re-create grants
  that already exist, which is an idempotent `GRANT`.

Whether a `removed` block may sit in the fetched child module, or must sit in the thin root
with a `module.` address, is settled by the `terraform` tier before implementation. If neither
works, `deploy` runs `terraform state rm` for the two addresses once and records it in the
environment's facts.

Adding `mcp` to a platform-only deployment destroys the platform-only grants, which revokes
them, and the MCP's first reconciliation grants them again. `analyst` reads nothing in between.
The runbook says so. No person uses `analyst` through the MCP before the MCP starts.

### Decision 5: Reconciliation converges in both directions (Accepted)

After Decision 4, grants restored by older deploys are still present on existing deployments.
The client also needs something to replace "check the grants after every deploy".

| Option | Consequences |
|---|---|
| A. Keep reconciling only what the MCP applied | Grants left by Terraform, by old scripts or by the client's post-hooks stay forever and are invisible. |
| **B. Reconciliation reads what each managed role actually holds inside the environment's database and revokes read grants the state does not hold** | Managed roles are `analyst`, every group role in the provisioner registry, and #4's per-person roles once they exist. Read grants mean `USAGE` on a schema, plus `SELECT` (current and future) on any relation kind (Decision 9). The scope is the convention schemas, `raw_*` and areas. A grant on any other schema, on another database, or to a role the MCP does not manage is never touched. On Snowflake the actual grants come from a new administrator-installed `VECTEN_ACCESS.READ_GRANTS(TARGET)` procedure, run with the owner's rights like `READ_GRANT`, which returns `SHOW GRANTS TO ROLE` and the schema-level future grants in the database. On Postgres they come from `pg_class.relacl`, `pg_namespace.nspacl` and `pg_default_acl`, which the provisioner can already read. Each grant added or revoked is logged and listed in access health. |
| C. B, but report only | Visibility without correction. The client's check becomes a read of access health instead of a script, but drift persists. |

**Accepted: B, with C available as a setting** (`reconcile_drift = revoke | report`,
default `revoke`). The first reconciliation after the upgrade is the migration for existing
deployments: on the client's account it revokes the `USAGE` and future `SELECT` that Terraform
restored in `MARTS`, if `mcp_state` holds the revoke. The changelog warns that a hand-made
read grant to `analyst` or a group role inside the convention schemas is revoked. The setting
lets an admin see the list before switching to `revoke`.

Revokes run before grants in each pass, as today, so a dataset grant inside a revoked schema
is re-applied in the same pass.

### Decision 6: How grants survive a rebuild (Accepted)

| Option | Consequences |
|---|---|
| **A. Snowflake: `+copy_grants: true` in the scaffolded `dbt_project.yml`, and a convention test refusing dbt `grants` config** | Per-object grants survive `CREATE OR REPLACE` in the same statement. Because a copied replacement skips the schema's future grants, a revoke on one object also persists across rebuilds. A new object (first build, a change between view and table, a rename) gets the future grants of its schema and no copied ones. In an area that means only the granted groups, and for a dataset grant reconciliation adds it. Both outcomes fail closed. |
| B. Reconcile after every dbt build | Shortens the gap but never closes it, and depends on an event arriving. |
| C. dbt `grants` config generated from `mcp_state` | Puts access in the build. dbt revokes what the config omits, which is why the spec forbids it today. |

**Accepted: A on Snowflake, B on Postgres.** Postgres has no equivalent of
`COPY GRANTS`. There, the MCP also reconciles when the dbt DAG's built record changes; the
DAG gains a finish time beside the built commit, so a rebuild of the same commit counts. The
restriction itself does not depend on this, because areas use schema-level default privileges.
Only dataset grants are briefly missing after a rebuild, which fails closed.

`vecten update` adds `+copy_grants: true` to existing projects through the scaffold's
three-way merge. The convention test fails a project that sets it to `false` on Snowflake.
Whether the DuckDB-backed emulation (fakesnow 0.11) accepts `COPY GRANTS` is checked first. If
it does not, the profile used by `dbt-build-local` turns it off.

### Decision 7: A group using an existing role (Accepted)

The issue's case is a client role such as `FINANCE_ANALYST` that already carries grants and
perhaps row access policies, and that the client wants an MCP group to read through.

| Option | Consequences |
|---|---|
| A. Ownership transfer: the admin runs `GRANT OWNERSHIP ON ROLE FINANCE_ANALYST TO ROLE PROVISIONER COPY CURRENT GRANTS` | Everything works as today, but the provisioner could then grant that role, with whatever it holds (writes, other databases), to any user in the account. `READ_GRANT` would accept it as a target, and deleting the group would drop it. In #34's shared layout, the unprefixed role fails `READ_GRANT`'s prefix rule. |
| **B. Binding by nesting: the group keeps its provisioner-owned role, and the admin runs `GRANT ROLE FINANCE_ANALYST TO ROLE <group role>`** | Members inherit the existing role through the hierarchy. The provisioner owns the group role but neither owns nor holds `FINANCE_ANALYST`, so it cannot grant it to anyone else. A role owner does not inherit the owned role's privileges, so `check_scope` sees no change. Deleting the group drops only the group role. The MCP prints the statement, records the binding after the admin confirms, and access health reports a binding that is missing. Policies keyed on `IS_ROLE_IN_SESSION('FINANCE_ANALYST')` still match; policies keyed on `CURRENT_ROLE()` do not, because the session's primary role is the group role (or #4's per-person role). |
| C. An owner's-rights procedure granting allow-listed existing roles to users | Gives `CURRENT_ROLE()` semantics only when the session's primary role is chosen, and widens the administrator-installed surface to role grants. |

**Accepted: B, on both warehouses.** On Postgres the statement is `GRANT finance_analyst
TO <group role> WITH INHERIT TRUE`. Before confirmation the panel and CLI show the bound role's
grants outside the environment's database, read with the admin's own statement since the
provisioner cannot. In #34's shared layout, binding a role that holds privileges on another
environment's database of the project is refused. The documentation states that a bound role's
reach is the client's to set, and that the warehouse enforces it.

### Decision 8: The group role's name (Accepted)

| Option | Consequences |
|---|---|
| **A. Derived from the group's name, overridable at creation (`vecten group create finance --role fin_readers`)** | A group can be created when its name matches an existing role. The override passes the same identifier checks and reserved prefixes (`admin_`, `svc_`, `pg_`, and `p_` for #4). The state keeps the unprefixed name, and #34's adapter adds the environment prefix. |
| B. Always derived | Creation fails when a role of that name exists, with no way round except renaming the group. |

**Accepted: A.** When the derived name exists, creation refuses and names both ways
out: `--role`, or create the group and bind the existing role (Decision 7).

### Decision 9: Relation kinds (Accepted)

| Option | Consequences |
|---|---|
| **A. Every relation kind that can hold a mart: tables, views, materialized views, dynamic tables, external tables, Iceberg tables** | `READ_GRANT` issues `ALL` and `FUTURE` grants per kind, and for one relation it finds the kind from `SHOW TERSE OBJECTS`. Revokes cover the same kinds. Postgres helpers already cover `r`, `p`, `v`, `m`, `f`. |
| B. A also with semantic views | Snowflake semantic views are queried differently, and their privileges were not verified. Left for a follow-up. |
| C. Tables and views only | Problem 5 stays. |

**Accepted: A.** A kind the account does not support makes the procedure fail with
Snowflake's message, which access health shows. Which `INFORMATION_SCHEMA` or `SHOW` column
names the kind is a `snowflake_live` check.

### Decision 10: Managed access for area schemas (Accepted)

| Option | Consequences |
|---|---|
| A. Area schemas `WITH MANAGED ACCESS`, owned by a role other than `LOADER` | `LOADER` can no longer grant on objects in an area, so a stray `grants:` config or post-hook fails instead of exposing data. But the schema must exist before dbt runs, so it is created by deploy, and ship runs without Terraform. The owner cannot be `LOADER`, because a schema owner can still grant. And `LOADER` can still `CREATE TABLE marts.x AS SELECT * FROM marts_finance.y`, so the trust boundary does not move. |
| **B. Ordinary schemas created by dbt, closed by default, with the convention test refusing `grants` config** | No deploy ordering and no new owner role. Grant-shaped mistakes in dbt are caught at build time. Data-shaped ones are caught by review, as under A. |

**Accepted: B now.** A can follow if the maintainers want the warehouse to refuse
grant statements from `LOADER`.

### Decision 11 (optional): An environment reader role for `vecten dbt --env` (Accepted)

#60 runs read-only dbt commands on `prod` as `LOADER`, because no role reads raw, staging,
snapshots, core and marts without write grants. The question is whether this change makes one
fall out.

Partly. A `READER` role (env-prefixed under #34) could hold database-level future `SELECT` and
`USAGE` on future schemas, plus `ALL` grants made at deploy. Schema-level future grants take
precedence, though, and they exist in every convention schema and in every area or raw schema
granted to a group. `READER` therefore also needs a schema-level future grant wherever one is
set. The Snowflake module would set it for the convention schemas, and `READ_GRANT` beside
every schema-level future grant it creates. That is one more invariant on the
administrator-installed procedure. `READER` also reads every area, so its key needs the same
care as `LOADER`'s.

**Accepted: not in this change.** Reserve the name: `READ_GRANT` refuses `READER` as a
target, and the identity contract lists it. Open a follow-up for #60 that builds on
Decision 5's report procedure, so a missing pairing shows up in access health.

## Interactions

- **#34, several environments in one account** (draft on `spec/multi-environment`). Areas
  live inside the environment's database, so they need no prefix. `READ_GRANT`'s area pattern
  is baked per database, as the database name already is. The default role's name comes from
  `snowflake_names` (`analyst` or `<PREFIX>_ANALYST`), and Decision 4 does not care which.
  Group role overrides (Decision 8) are unprefixed in `mcp_state` and prefixed by the adapter.
  #34 asked #51 to choose how adopted roles meet `READ_GRANT`'s prefix rule. Binding by nesting
  (Decision 7) means `READ_GRANT` never targets the bound role, so no allow-list is needed. In a
  shared account a bound role that reads another environment's database is refused. Merge
  order: both changes edit `terraform/snowflake/main.tf`, `snowflake_access.js` and
  `check_scope`, and whichever lands second rebases. Neither modifies the other's
  requirements. This change adds a requirement to `shared/deploy` rather than modifying
  "Snowflake module", which #34 modifies.
- **#4, one reader role per person.** `P_<LOGIN>` holds the person's group roles. Bound roles
  sit under group roles, so the person's single session role reads the union, which is what #4
  promises. Decision 5 treats `P_` roles as managed roles that must hold no direct read grant,
  and revokes any. Under #4 `CURRENT_ROLE()` is always `P_<LOGIN>`, so policies must use
  `IS_ROLE_IN_SESSION`. The documentation says so once, for both changes. Decision 8 reserves
  the `p_` prefix for role overrides.
- **#60, `vecten dbt --env`.** Decision 11. Separately, `copy_grants` changes nothing for
  `--env`, because read-only commands do not rebuild relations.

## Risks / Trade-offs

1. **[Two-way reconciliation revokes hand-made grants]** → Limited to read grants held by
   managed roles inside the environment's database's convention schemas and areas. The setting
   `reconcile_drift = report` previews the list. Access health and the log name every revoke.
2. **[The dbt project can still move restricted data]** → Stated in the docs and the `model`
   skill. Review is the control (Non-Goals).
3. **[Restricted datasets' names and descriptions stay visible]** → `describe` and `search`
   list every dataset. Follow-up issue: filter by the person's readable sources.
4. **[Database-level future grants made by the client]** → They reach every area no group has
   been granted, because schema-level grants take precedence only where they exist. Access
   health reports database-level future grants in the environment's database.
5. **[Old relations stay after a model moves to an area]** → dbt does not drop the relation
   it no longer builds, and the old copy in `marts` stays readable by the default group. The
   migration plan drops it before anything else is granted back, and `next` reports relations
   in convention schemas and areas that no model builds.
6. **[Rerunning the provisioner script is a human step]** → `READ_GRANT` and `READ_GRANTS`
   change. Until the admin reruns it, the MCP's startup check finds the old procedure (by a
   version string the procedure returns) and refuses area grants and convergence with a
   message naming `vecten mcp provisioner-script`, while every other grant keeps working.
7. **[Postgres helpers lag a new area]** → Ship's setup creates declared areas' schemas, so
   the helpers exist from the first ship after the declaration. On an existing Postgres
   warehouse the admin reruns the script, as for any new schema today.
8. **[A Snowflake behaviour change on `CREATE OR REPLACE`]** → dbt's documentation cites
   Snowflake bundle 2026_08 (BCR-2433, "grants on the replaced object are copied by default"),
   which that bundle's page now lists as removed. Setting `copy_grants` explicitly makes the
   project independent of the account default either way.

## Migration Plan

### Existing deployments

Snowflake with the MCP:

1. Release the version. `deploy` applies the module with the `removed` blocks, and the plan
   shows no destroy of an `analyst` grant (`terraform` tier plus a plan on a sandbox).
2. The admin reruns `vecten mcp provisioner-script --env <env>` as ACCOUNTADMIN and confirms
   with `--confirm-run`. This installs the new `READ_GRANT` and `READ_GRANTS` and no longer
   grants `analyst` data. `next` reports the step until the version check passes.
3. The MCP's first reconciliation converges. Where `mcp_state` holds the default group's
   `core` and `marts`, nothing changes. Where an admin revoked `marts`, the `USAGE` and future
   `SELECT` that Terraform restored are revoked and listed in access health.

Snowflake without the MCP: the module creates the platform-only grant resources over grants
that already exist, and nothing else changes.

Postgres: the next ship runs setup SQL that grants `analyst` nothing when `with_mcp` is true,
and installs helpers for declared areas. The MCP converges as above.

### The client: off the post-hook workaround

Stage 1 (Decisions 4 to 6, the first two pull requests, before areas):

1. Upgrade, deploy, rerun the provisioner script, and run `vecten update` so that
   `dbt_project.yml` carries `+copy_grants: true`.
2. Remove the revoke post-hooks from the dbt project in the same commit. From here, the
   default group's lack of `marts` is held by the MCP and no deploy restores it. Per-mart
   grants survive rebuilds.
3. Drop the grant check from the deploy pipeline. Access health (`vecten issues`) lists any
   drift, and reconciliation revokes it.

Stage 2 (areas):

1. Declare `[areas.finance] access.groups = ["finance"]` in `vecten.toml`.
2. Move the finance marts to `models/marts_finance/` and the finance-only core models to
   `models/core_finance/`, with `+schema` set to match, then run `dbt-build-local`. The
   regenerated `osi/datasets.json` names the new sources.
3. Ship. The dbt DAG builds the area schemas, and `next` reports the command that grants
   `finance` the area.
4. Drop the old copies in `MARTS` and `CORE`. `next` lists them, and the runbook gives the
   statements. This must come before step 5.
5. Grant `marts` back to the default group (`vecten grant --schema marts --group default`) and
   revoke the per-mart dataset grants from stage 1, which the schema grant now covers.

After stage 2 none of the three parts of the workaround remain: no post-hook, no per-dataset
grants for the restriction, no post-deploy check.

### Rollback

- Reverting to the previous release brings back the module's `analyst_schema` and
  `analyst_future`. They re-grant `USAGE` and future `SELECT` on `CORE` and `MARTS` to
  `analyst`, which is the original problem 1 and nothing worse. Areas are not affected: their
  grants live on group roles, and no old resource or script touches schemas outside `core` and
  `marts`. The old naming test fails area models, so dbt stops building them, which fails
  closed. The installed procedures stay until an admin reruns the old script. The old
  `READ_GRANT` refuses area schemas.
- `copy_grants` can be turned off in `dbt_project.yml` at any time, which brings back problem 2.
- `reconcile_drift = report` stops revocations without a release.
- A binding is undone with `REVOKE ROLE <existing> FROM ROLE <group role>` and an unbind in
  the MCP.

## Not verified

- Whether a `removed` block can live in the fetched child module (the `terraform` tier settles
  it; Decision 4 gives the fallback).
- Whether `GRANT … ON ALL TABLES` also covers dynamic, external or Iceberg tables. The design
  grants each kind explicitly, so it does not depend on the answer.
- Which `SHOW TERSE OBJECTS` or `INFORMATION_SCHEMA.TABLES` value names each relation kind
  (`snowflake_live`).
- Whether dbt-snowflake 1.12 adds `COPY GRANTS` on `--full-refresh` of an incremental model.
- Whether fakesnow 0.11 accepts `COPY GRANTS`.
- That `IS_ROLE_IN_SESSION` matches a role inherited through a granted role (from memory, not
  checked).
- The current state of Snowflake BCR-2433.
- `GRANT USAGE ON FUTURE SCHEMAS IN DATABASE`, which only Decision 11 needs.
- Semantic view privileges (Decision 9, B).
