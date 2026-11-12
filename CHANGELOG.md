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
  `raw_<source>` schemas, and revokes those the state does not hold. Access health lists every
  grant added, revoked or found drifting, and `vecten issues` reports drift kept under
  `reconcile_drift = report` and an outdated install of the access procedures. The setting
  `reconcile_drift` (`revoke`, the default, or `report`) is in the admin panel's settings.

  **Upgrading.** Snowflake deployments with the MCP: `vecten deploy` forgets the old
  `analyst_schema` and `analyst_future` resources without revoking them (the plan destroys
  nothing); then an ACCOUNTADMIN reruns `vecten mcp provisioner-script --env <env>` and the
  run is confirmed with `--confirm-run`, which installs `VECTEN_ACCESS.READ_GRANTS` and the new
  `READ_GRANT`. Until then the MCP keeps applying grants but neither lists nor revokes drift,
  and `vecten issues` names the step. Postgres deployments need nothing beyond the next ship;
  an existing Postgres warehouse needs no rerun.

  **Warning.** A read grant made by hand to `analyst` or a group role inside `core`, `marts`,
  `staging`, `snapshots` or a `raw_<source>` schema is revoked by the first reconciliation
  after the upgrade unless the state holds a matching grant. Set `reconcile_drift` to `report`
  before upgrading to see the list first, record the grants you want with `vecten grant`, then
  set it back to `revoke`. Grants in other schemas or databases, and grants to roles the MCP
  does not manage, are never touched.

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
