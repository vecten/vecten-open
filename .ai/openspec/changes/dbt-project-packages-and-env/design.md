## Context

`vecten-platform` writes `dbt/packages.yml` before every dbt run (`write_packages_yml` in
`dbt/deps.py`): a `local:` entry for the bundled `vecten_convention` package and one per
installed extension that declares `vecten.dbt_packages`. Three callers go through it:
`vecten dbt` on the laptop, `resolve_dependencies` for `dbt-build-local`, and the same function
as the dbt DAG's `deps` task on a host. The file is git-ignored and left out of the bundle,
because the paths are machine-specific.

`vecten dbt <args>` builds its profile environment from the laptop provider and refuses a
Snowflake project, since the laptop stack's only warehouse is Postgres. The rendered
`dbt/profiles.yml` already reads every connection value from environment variables, under one
output, `local`, which the dbt DAG also uses on the hosts.

dbt 1.12 is the version every choice below was read from, and verified against where it says
so; the rendered `dbt_project.yml` pins `require-dbt-version` to `>=1.12.0,<1.13.0`.

## Goals / Non-Goals

**Goals:**

- A project adds hub and public git packages once, and the laptop, CI and every host install the
  same code.
- `vecten dbt --env <env>` runs dbt with the environment's own credentials, which never land in
  a file or in output.
- On `prod`, an allowed command builds nothing, and anything in the project that would make it
  write is refused before dbt connects.
- Nothing changes without `--env`, apart from a better Snowflake refusal.

**Non-Goals:**

- Private git packages: the hosts hold no git credential, and adding one is a deploy change.
- A read-only Snowflake identity for dbt: it means new warehouse objects in the Snowflake
  module, which this change does not create.
- `--env` on Postgres, and any tunnel to a deployed database.
- New machinery for a scratch or CI database.

## Decisions

### The project's list lives in `dbt/project-packages.yml`

A YAML file beside `dbt_project.yml`, in dbt's own `packages:` shape, committed and shipped with
the rest of `dbt/`. `write_packages_yml` appends its entries after the generated `local:`
entries, so all three callers merge it without a change to `runner.py` or `airflow_dag.py`.

- *A `[dbt] packages` section in `vecten.toml`*: rejected. `save_config` rewrites `vecten.toml`
  through `tomli_w` whenever a command records something (`connector add`, `answer`,
  `init --deploy`), which drops comments a human leaves beside a pin; a pull request that changes
  `vecten.toml` runs the `terraform-plan` workflow, which a package bump has no reason to; the
  schema lives in `vecten-shared`, the contract every runtime loads; and hub entries would have
  to be translated from dbt's syntax, which the hub pages and dbt's docs show.
- *dbt's own `dependencies.yml`*: dbt refuses a `packages` key in both `packages.yml` and
  `dependencies.yml` (`dbt/config/project.py`), and vecten writes `packages.yml`.
- *Swap the roles, with the project owning `packages.yml` and vecten generating
  `dependencies.yml`*: every existing project ignores `dbt/packages.yml` in git and documents it
  as generated, so the swap breaks them on upgrade.

### Pins name one release or one commit

A hub entry needs `version` as one `X.Y.Z` string; a git entry needs `revision` as a full
40-character lowercase commit id, with an optional `subdirectory`. Ranges, lists of ranges,
partial versions, tags and branches are refused, as are `local`, `private`, `tarball` and any
other key, including `install-prerelease` and `warn-unpinned`. The laptop and the hosts each
run `dbt deps` on their own, so only a pin that cannot move makes them install the same code.

- *Ship `package-lock.yml`*: its `sha1_hash` covers `packages.yml`, whose `local:` paths differ
  per machine, so a shipped lock never matches the host's `packages.yml` and dbt re-resolves.
- *Allow tags*: a tag can be moved or deleted after review.

A git URL must be `https://` with no user information and no Jinja, which keeps out the
`env_var` token pattern dbt documents for private repositories. A hub package's own
dependencies are resolved by dbt hub from the ranges that package declares, so a package that
depends on another can resolve differently over time; this remains (see Risks).

### Collisions are by installed name

Each project entry is named by what it installs: the part of a hub package after the slash, and
for git the last part of `subdirectory` or of the repository path without `.git`. Each generated
entry is named by its `dbt_project.yml` (`vecten_convention`, or the extension's project name).
A project entry with a generated name, or two project entries with one name, exit 6 before
`packages.yml` is written. dbt's own duplicate-project check stays behind this for a git
repository whose project name differs from its path.

### The host reaches dbt hub and GitHub

The image runs no `dbt deps` at build time; the DAG's `deps` task installs into
`/tmp/vecten-dbt/dbt_packages` on every run, and `ensure_deps` reinstalls after a ship empties
that directory. Read from the modules: on AWS the hosts sit in public subnets with an Elastic IP
and a security group whose egress is `0.0.0.0/0`; on Scaleway each host has a flexible IP and a
security group with `outbound_default_policy = "accept"`. The Airflow image installs `git`. So
a deployed host already reaches `hub.getdbt.com`, `codeload.github.com` and `github.com`; the
change records that as a deploy requirement so a later egress rule keeps them. When the hub or
GitHub is unreachable, `deps` fails, `build` does not run, and `vecten issues` lists the task.

### `--env` is vecten's one argument, and comes first

`vecten dbt --env <env> <args>` or `--env=<env>`. dbt has no `--env` option, so one later in the
arguments is a misplaced vecten flag and exits 2 rather than reaching dbt, or, on a Snowflake
project, the laptop refusal. Everything after it is dbt's, unchanged.

### The identity is LOADER

What the Snowflake module creates: `ANALYST`, with `SELECT` on future tables and views in `CORE`
and `MARTS` only, no grant on raw, `STAGING` or `SNAPSHOTS`, and no user or key in the secrets
store; `LOADER`, which owns every schema and is the dbt DAG's identity, with its key as
`SNOWFLAKE_LOADER_PRIVATE_KEY`; and, with the MCP, `PROVISIONER`, which manages people's roles.
The MCP's people roles read marts for one person each and are not the CLI's to use. No identity
reads raw through marts without write grants, so `--env` runs as `LOADER`, exactly as the dbt DAG
does, on every environment. Read-only on `prod` is then enforced by vecten, below, and the gap
that leaves is stated under Risks.

### Credentials come from the environment's store and stay in the process

`cloud_store(root, env)` reads `SNOWFLAKE_LOADER_PRIVATE_KEY` with the human's cloud login;
`snowflake_names(config, env)` gives the database and `<PROJECT>_<ENV>_WH`; the account is
`[warehouse] account`. `profile_env` turns them into the variables the rendered profile reads,
the same function the dbt DAG uses. The key is also set as `DBT_ENV_SECRET_VECTEN_WAREHOUSE_KEY`,
because dbt replaces the value of every `DBT_ENV_SECRET_*` variable in what it logs and prints
(`dbt_common.events.functions.env_scrubber`). Nothing is written: not `profiles.yml`, not a
temporary file. A missing key names `vecten deploy --env <env>`, which generates it. A refused
command reads no secret.

### The prod allow-list, read and verified

Allowed: `clean`, `compile`, `debug`, `deps`, `docs generate`, `docs serve`, `list`/`ls`,
`parse`, `show`, `source freshness`, `test`. Everything else is refused, including `run`,
`build`, `seed`, `snapshot`, `clone`, `retry`, `run-operation`, `init`, a bare group (`docs`,
`source`) and arguments that resolve to no command. The command is read with dbt's own click
tree (`dbt.cli.main.cli`), so global flags before it (`--debug test`) and the `ls` alias resolve
as dbt resolves them.

Which commands execute the project's `on-run-start` and `on-run-end` hooks was checked in code
(`RunTask.before_run` and `after_run`; `CompileTask.before_run` only fills the relation cache;
`FreshnessTask.get_hooks_by_type` honours `source_freshness_run_project_hooks`, default true)
and then run on the Snowflake emulation with a hook that creates a table: `run`, `test` and
`source freshness` created it; `parse`, `ls`, `compile`, `show --select`, `show --inline`,
`docs generate` and `debug` did not.

### What makes an allowed command write is refused, not neutralised

- `test --store-failures` is refused; an inherited `DBT_STORE_FAILURES` is dropped from dbt's
  environment.
- Before `test` and `source freshness`, vecten runs `dbt parse` into a temporary target
  directory, with the run's `--vars` and without the credential, and reads the manifest. Any
  `operation` node (a hook of the project or of any installed package) refuses both commands;
  any test whose `store_failures` is true or whose `store_failures_as` is `table` or `view`
  refuses `test`. dbt's `TestConfig` folds `store_failures_as` into `store_failures`, and both
  are checked.
- Unit tests are left out of prod `test` with `DBT_EXCLUDE_RESOURCE_TYPES=unit_test`: dbt's unit
  materialisation creates a temporary table in the model's schema and drops it. They need no prod
  data; `dbt-build-local` runs them. `--exclude-resource-type` on the command line would override
  the variable, so prod `test` refuses it.

Alternatives considered:

- *Neutralise hooks*: dbt 1.12 has no command-line switch that skips on-run hooks for `test`;
  `source_freshness_run_project_hooks` is a project flag in `dbt_project.yml`, and writing the
  user's project to flip it is a write vecten should not make.
- *Refuse hooks for every command*: stricter than the code and the run show is needed; compile,
  show and docs generate keep working beside hooks.
- *Check only the tests the run selects*: needs `dbt ls` with the run's selection flags, which
  are mixed with `test`'s other flags; refusing on the whole project is simpler and errs safe.
- *Run the user's command in-process with the checked manifest (`dbtRunner(manifest=...)`)*:
  the same manifest would be checked and run, but `vecten dbt` forwards dbt's own process exit
  code and output, which `dbtRunner` reduces to success or failure.

### Postgres is refused

The deploy spec's "Databases are private and set up from the VM" puts every managed Postgres
instance on the private network unless `database_public_cidrs` opts in: on AWS in private
subnets with no route out, on Scaleway with no public endpoint. `--env` on a Postgres project
exits 4 before any cloud call, naming the private network, the dbt DAG,
`vecten issues --env <env>` and `vecten dbt` without `--env`.

- *Open the way `vecten user add-admin` does (`admin_connection`)*: an SSM tunnel through the
  setup host on AWS, and on Scaleway the public endpoint that exists only when
  `database_public_cidrs` opts in. dbt would hold that tunnel for a whole run, as `loader`,
  whose password the laptop would then read; that is a larger change than the maintainer scoped.
- *Use `database_public_cidrs` when set*: an administrator's opt-in, and the same `loader`
  password question.

### Scratch and CI databases

No new target. A scratch Snowflake database is another environment, in its own Snowflake account
since one account serves one environment: deploy it, then `vecten dbt --env <name> build`, with
every command allowed. CI keeps `vecten dbt-build-local --check` on the emulation. The docs say
both.

## Risks / Trade-offs

- **LOADER can write.** On prod the guarantee is vecten's checks, not the warehouse's grants.
  They stop accidents, not a determined user: anyone who can run `--env prod` can read the
  LOADER key from the secrets store with the same cloud login. The strong guarantee is a
  read-only role over raw, staging, snapshots, core and marts with its own key; that needs a
  Snowflake module change and is left for a follow-up.
- **Jinja that runs SQL at compile time.** A model or macro that calls `run_query`,
  `statement` or an adapter method, and Jinja in `show --inline` or `compile --inline`, executes
  as LOADER during `compile`, `show`, `docs generate` and `test`. vecten cannot see it without
  rendering the project against the warehouse.
- **Singular tests are SQL a person wrote.** dbt wraps them in a select, but nothing stops a
  test from calling a stored procedure.
- **Stored failures refuse the whole project's `test`**, even when the selection holds no such
  test; the message names the tests so they can be fixed or the run moved to another
  environment.
- **Transitive hub dependencies** resolve from the ranges a package declares.
- **dbt 1.13.** The allow-list and the hook findings are read from 1.12. The rendered project
  pins 1.12; the next dbt bump rereads them.
