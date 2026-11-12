# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions
follow [Semantic Versioning](https://semver.org/); in the 0.x series a minor release may break.

## [Unreleased]

### Added

- In-repo extensions: an extension package kept in the project under `extensions/<name>/`,
  registered with `vecten extension add ./extensions/<name>`, locked with its third-party
  dependencies only, installed editable in the CLI's environment and built from the bundle on
  every host. `vecten extension new <name> [--template <dir or git+https://…@<ref>>]` creates
  one from a template or the built-in skeleton, once.
- `vecten lock` writes `requirements.txt` (one pin per extension), one lock per component and
  `extensions.lock.json` together. An extension's `platform`, `mcp` and `cli` extras install
  only in that component, and `[dependencies.platform|mcp|cli] python` in `vecten.toml` adds a
  project's own packages to one component. Each lock records a hash of its inputs, which `up`
  and the bundle build check, naming `vecten lock` when stale; re-locking keeps existing pins,
  and `--upgrade <package>` moves one. `extension add` locks the same way, and `connector add`
  re-locks a project that already has locks ([#50](https://github.com/vecten/vecten-open/issues/50)).
- `[dependencies.platform] system` and `[dependencies.mcp] system` in `vecten.toml` install
  Debian packages into that image's project layer, as root before the Python packages, leaving
  no apt lists and returning to the image's user. Entries are validated as package names
  ([#50](https://github.com/vecten/vecten-open/issues/50)).
- Any extension can declare the secrets it reads and a schema for its settings in a
  `vecten.manifest` entry point; a project declares its own in `vecten.toml` (`secrets`,
  `[settings.project]`). A secret's prefix decides where it is delivered (`MCP_` names to the
  MCP server, others to Airflow), the same way on the laptop and on every VM. Settings live in
  `[settings.<extension>]` with `env.<env>` overrides, are checked against the schema by
  `vecten lock`, `up`, `ship` and the MCP server's startup, and are read, with declared secrets,
  through `vecten_shared.context` ([#49](https://github.com/vecten/vecten-open/issues/49)).
- Deployed hosts: `dags_paused_at_creation = true` in `deploy/envs/<env>.tfvars` makes that
  environment's new DAGs start paused, `[airflow] allow` in `vecten.toml` limits which networks
  reach `airflow.<domain>` (Airflow's health path stays open for `vecten ship`), and
  `root_volume_size_gb` in the tfvars sizes every VM's root volume on AWS and Scaleway. Every
  default is the current behaviour.
- `build_sync_dag` takes `stages` (objects that run in a declared order, each stage once the
  one before it synced), `params` (run parameters passed to the connector's `source()`),
  `pool`, `guard` (a function whose falsy return skips the run without failing it),
  `timezone` (the zone a cron schedule is read in) and `paused_at_creation`. A DAG file that
  passes none of them behaves as before.
- An extension connector's declaration takes `groups`: each group's objects, cron `schedule`
  and `timezone`. `connector add` renders one sync DAG per group on its own schedule; an
  extension that declares no groups still gets the one `default` group on `@daily`.
- `dbt_build` has run parameters `select`, `exclude` and `full_refresh` for a manual run, and
  `build_dbt_dag` takes their defaults, `trigger="any"` (build when any awaited group updated
  rather than all of them) and `paused_at_creation`; `dbt_assets(config, groups)` narrows the
  awaited groups. A green build emits the `dbt_build` asset (`vecten://dbt/build`). A
  narrowed build skips `mark_built`, so the built commit still means the whole bundle built.
- A project lists its own dbt packages in `dbt/project-packages.yml`: dbt hub packages pinned
  to one release and public git repositories pinned to a full commit id. `vecten dbt`,
  `vecten dbt-build-local` and the dbt DAG's `deps` task append them to the generated
  `dbt/packages.yml`, and the file ships in the bundle. An unpinned, private, local or tarball
  entry, a duplicate, or one named like a generated entry exits 6 before dbt runs. A deployed
  host needs outbound HTTPS to `hub.getdbt.com` and GitHub, which both clouds' hosts already
  have.
- `vecten dbt --env <env> <args>` runs dbt against a deployed environment's Snowflake warehouse
  as its `LOADER` user, with the key from that environment's secrets store passed to dbt
  through its process environment only. On `prod` only commands that build nothing run
  (`compile`, `test`, `ls`, `show`, `debug`, `parse`, `source freshness`, `docs generate`,
  `deps`, `clean`, `docs serve`). `test` leaves out unit tests and refuses `--store-failures`,
  and `test` and `source freshness` are refused while the project declares on-run hooks or a
  test stores its failures. Every command runs on other environments. A Postgres project is
  refused, because its deployed warehouse is on a private network.
- Restricted areas, in the schema convention's version 2, a superset of version 1:
  `[areas.<area>]` in `vecten.toml`, with an optional `access.groups` list, declares the
  schemas `core_<area>` and `marts_<area>`, which no warehouse role reads until an admin grants
  the area to a group. Their models follow the core and marts rules, each mart in
  `marts_<area>` becomes a dataset whose source names that schema, and
  `convention_model_naming` fails a model in a `core_<name>` or `marts_<name>` schema whose
  area is not declared. `vecten dbt-build-local`, the dbt DAG and `vecten dbt` (with or
  without `--env`) pass the declared areas to dbt as the var `vecten_areas`, merged into a
  `--vars` given by hand. `vecten update` moves an existing project's
  `docs/schema-convention.md` and `convention_version` to version 2
  ([#51](https://github.com/vecten/vecten-open/issues/51)).
- Area grants: `vecten grant --area <area> --group <group>`, the admin panel and the admin API's
  `grant` with kind `area` grant a group both of an area's schemas, applied and revoked
  together, each with its own future or default grant, so a mart dbt rebuilds there is readable
  by the granted groups and no one else before any reconciliation. A grant of an area with no
  `[areas.<area>]` entry is refused naming it. Schema grants accept a declared area's schemas,
  and reconciliation's drift pass covers them. `next` asks for an area's `access.groups`
  (answered with `vecten answer access.<area>`) and, after deploy, reports the command that
  grants each declared group the area. Snowflake's `READ_GRANT` and the Postgres helpers accept
  schemas matching `(core|marts)_<area>` by pattern; on Postgres the setup `ship` runs creates
  every declared area's schemas as `loader` before it installs the helpers. An area removed
  from `vecten.toml` has its grants revoked and forgotten. The access procedures are version
  4: until an ACCOUNTADMIN reruns `vecten mcp provisioner-script`, grants that reach an area
  are refused and drift is neither listed nor revoked, while every other grant keeps
  reconciling ([#51](https://github.com/vecten/vecten-open/issues/51)).
- Read grants on Snowflake cover every relation kind a mart can be: a schema or area grant
  grants and revokes `SELECT` on all and future tables, views, materialized views, dynamic
  tables, external tables and Iceberg tables, and a dataset grant names its relation's kind,
  read back through `SHOW GRANTS`. On a grant, a kind the account refuses is a failure in
  access health carrying Snowflake's message, the other kinds still apply, and
  `vecten grant` exits with code 5 naming the refusals; a refusal on tables or views fails the
  grant. A revoke the warehouse refuses while the role still reads in the schema fails and
  stays recorded, so every reconciliation retries it
  ([#51](https://github.com/vecten/vecten-open/issues/51)).

### Changed

- `requirements.txt` holds only extension pins after the first `vecten lock`; connector
  dependencies live in the locks. Locks written by an earlier release keep building until then.
- The `connector` skill names the `build_sync_dag` option for each scheduling request, and says
  that a new sync DAG syncs the latest whole day as soon as it is unpaused rather than at the
  next midnight.
- `vecten dbt` on a Snowflake project without `--env` now names `--env` alongside
  `vecten dbt-build-local` in its refusal.
- With the `mcp` component, the MCP's state is the only owner of the default group's reads
  on `core` and `marts`: the Snowflake module, the provisioner script and the Postgres setup
  SQL no longer grant `analyst` anything on a schema, and the MCP's first reconciliation
  applies the grants it seeded. A revoke of `marts` from the default group now survives
  `deploy`, `ship` and a rerun of the provisioner script
  ([#51](https://github.com/vecten/vecten-open/issues/51)). Without the `mcp` component the
  deploy grants them as before.
- Reconciliation converges in both directions: it reads the read grants (`USAGE` on a schema,
  current and future `SELECT`) that `analyst` and every group role hold in the convention and
  `raw_` schemas, and revokes those the state does not hold. Access health lists every grant
  added, revoked or found drifting, and the database-level future grants of those roles;
  `vecten issues` reports drift that was kept and an outdated install of the access
  procedures. The setting `reconcile_drift` (`revoke` or `report`) is in the admin panel's
  settings: a new deployment starts with `revoke`, and a deployment whose MCP state an earlier
  release created starts with `report`.

  **Upgrading.** Snowflake deployments with the MCP: `vecten deploy` forgets the old
  `analyst_schema` and `analyst_future` resources without revoking them (the plan destroys
  nothing); then an ACCOUNTADMIN reruns `vecten mcp provisioner-script --env <env>` and the
  run is confirmed with `--confirm-run`, which installs `VECTEN_ACCESS.READ_GRANTS` and the new
  `READ_GRANT`. Until then the MCP keeps applying grants but reads no drift, and
  `vecten issues` names the step. Postgres deployments need nothing beyond the next ship, and
  an existing Postgres warehouse needs no rerun; there the drift report starts at once.

  **Before upgrading**, remove every dbt `grants:` config and granting post-hook from the
  dbt project, and override any a dbt package sets with `+grants: {}` under the package's name
  in `dbt/dbt_project.yml`: from this release every dbt run, local or on a host, stops before
  it builds anything while one remains.

  After the upgrade nothing is revoked: the deployment is in `report` mode. Read the drift in
  access health or `vecten issues`, record every grant you want to keep with `vecten grant`
  (a schema grant, or a dataset grant for a relation the semantic model has as a dataset),
  then set `reconcile_drift` to `revoke`. A grant that cannot be recorded is revoked from then
  on: `SELECT` on a relation that is not a dataset, a grant on a single relation of `staging`,
  `snapshots` or a `raw_` schema that is not a dataset. A post-hook that grants is revoked on
  every pass and granted again by every build, so it must be gone before the switch.
  Grants in other schemas or databases, and grants to roles the MCP does not manage, are never
  touched.

  **Rolling back.** After the provisioner script was rerun, an earlier release's MCP refuses to
  start beside `USAGE` on the new procedure. Before starting it, an ACCOUNTADMIN runs
  `REVOKE USAGE ON PROCEDURE <DATABASE>.VECTEN_ACCESS.READ_GRANTS(VARCHAR) FROM ROLE
  PROVISIONER;` (or drops the procedure). The earlier release's grants come back with it:
  its Terraform resources grant `ANALYST` `USAGE` and future `SELECT` on `CORE` and `MARTS`
  again.
- Rebuilt relations keep their grants. A Snowflake project's `dbt/dbt_project.yml` sets
  `+copy_grants: true` for every model, so a table or view dbt replaces keeps the grants of
  the one it replaces, dataset grants included, in the same statement; `vecten update` adds the
  setting to an existing project and leaves the rest of the file as it is. A new convention
  test, `convention_no_grants`, fails the build when a model, snapshot or seed sets dbt's
  `grants` config, or a Snowflake model sets `copy_grants` to `false`; it runs at the start of
  every dbt run (an `on-run-start` hook of the convention package), so the run stops before
  dbt builds or grants anything, and it covers dbt packages' models too, naming the package
  and the `+grants: {}` override for `dbt/dbt_project.yml`. On Postgres, which
  cannot copy grants, the dbt DAG records when each build finished
  (`dbt/build_finished_at`, beside the built commit) and the MCP reconciles as soon as that
  time changes, so a dataset grant a rebuild dropped returns within a minute.

### Fixed

- `--env <env>` commands that reach or name the MCP (`vecten login`, `user`, `group`, `grant`,
  `issues`, `mcp admin-link`, `mcp connection`, `mcp provisioner-script` and the sign-in items
  of `next`) use the environment's own domain from `deploy/envs/<env>.tfvars`, not prod's
  `[deploy] domain` ([#36](https://github.com/vecten/vecten-open/issues/36)).
- A sync DAG no longer publishes its asset when an object failed, so `dbt_build` no longer
  builds on a partial sync. The asset now follows the `check` task, and existing DAG files
  pick this up unchanged.

## [0.1.0] - 2026-11-09

The first public release.

- The `vecten` CLI over two components: `platform` (dlt ingestion, Apache Airflow
  orchestration, dbt modelling to schema convention 1, an Apache Ossie semantic model) and
  `mcp` (the Model Context Protocol server over the warehouse, with one warehouse identity per
  person, an audit with receipts, usage analytics and the feedback loop).
- Deploys to AWS and Scaleway with Postgres or Snowflake, through GitHub Actions and
  Terraform; runs on a laptop with Docker.
- Five agent skills (`guide`, `connector`, `model`, `semantic`, `operate`), shipped in the
  package, vendored into every project and published as a Claude Code plugin, with their
  evaluations.
- Published as `vecten`, `vecten-platform`, `vecten-mcp` and `vecten-shared` on PyPI and as
  `ghcr.io/vecten/vecten-airflow` and `ghcr.io/vecten/vecten-mcp`.

[Unreleased]: https://github.com/vecten/vecten-open/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vecten/vecten-open/releases/tag/v0.1.0
