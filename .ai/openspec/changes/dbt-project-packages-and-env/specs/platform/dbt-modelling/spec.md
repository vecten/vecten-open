## MODIFIED Requirements

### Requirement: Adapter follows the warehouse choice

The rendered dbt project SHALL use dbt-core with the Postgres adapter when the warehouse is Postgres and the Snowflake adapter when the warehouse is Snowflake, with a profile that the CLI supplies so the user never writes one by hand.

#### Scenario: Postgres project

- **WHEN** a project is rendered with `warehouse=postgres`
- **THEN** `vecten dbt debug` connects through the Postgres adapter with the provider's connection string

#### Scenario: Snowflake project

- **WHEN** a project is rendered with `warehouse=snowflake` and deployed to `prod`
- **THEN** `vecten dbt --env prod debug` connects through the Snowflake adapter

### Requirement: dbt pass-through

`vecten dbt <args>` SHALL invoke dbt with the given arguments against the project's profile and target, forwarding dbt's output and exit code unchanged. It SHALL interpret one argument of its own, a leading `--env <env>` (or `--env=<env>`), and no other; an `--env` anywhere else SHALL be refused as a usage error, because dbt has no such option. Without `--env` it SHALL run against the laptop stack's warehouse, and on a Snowflake project, which the laptop stack cannot serve, it SHALL refuse and name both `vecten dbt --env <env>` and `vecten dbt-build-local`. It is the one command outside the CLI's `--json` and exit code contract.

#### Scenario: Forwarded command

- **WHEN** `vecten dbt run --select core` is run
- **THEN** dbt runs the `core` selection and `vecten` exits with dbt's exit code

#### Scenario: Snowflake project without an environment

- **WHEN** `vecten dbt debug` is run in a Snowflake project
- **THEN** it exits 3 without running dbt and names `vecten dbt --env <env>` and `vecten dbt-build-local`

#### Scenario: A misplaced environment flag

- **WHEN** `vecten dbt test --env prod` is run
- **THEN** it exits 2 without running dbt and says to put `--env` first

### Requirement: dbt dependencies are resolved by vecten-platform

`vecten-platform` SHALL resolve dbt dependencies before every build: the `vecten_convention` package, which carries the convention tests and macros, taken from the installed `vecten-platform` wheel and checked against the version in the versions table, a `local:` entry for every installed extension that declares `vecten.dbt_packages`, resolved from where the package is installed, and then the project's own packages from `dbt/project-packages.yml`. `vecten dbt deps`, `dbt-build-local` and the dbt DAG SHALL all go through it, so project, extension and convention models build identically on a laptop, in CI and on the VM. `dbt/packages.yml` SHALL be generated before every run with `local:` paths, which are machine-specific, so it and the `dbt/package-lock.yml` that `dbt deps` writes SHALL be git-ignored and left out of the bundle, while `dbt/project-packages.yml` SHALL be committed and shipped with the rest of `dbt/`.

#### Scenario: Extension dbt package

- **WHEN** an extension declaring `vecten.dbt_packages` is listed in `vecten.toml` and dependencies are resolved
- **THEN** `packages.yml` holds a `local:` entry pointing at the installed package's dbt directory
- **AND** the extension's models build with the project

#### Scenario: Convention package

- **WHEN** a fresh project resolves dependencies
- **THEN** `packages.yml` holds a `local:` entry for the `vecten_convention` package bundled in the installed wheel, whose version matches the versions table, and the convention tests run from it

#### Scenario: Project package on every machine

- **WHEN** `dbt/project-packages.yml` lists `dbt-labs/dbt_utils` at version `1.3.0` and dependencies are resolved by `vecten dbt deps`, `dbt-build-local` or the dbt DAG's `deps` task
- **THEN** `packages.yml` holds the generated `local:` entries followed by that entry, and `dbt deps` installs `dbt_utils` 1.3.0

## ADDED Requirements

### Requirement: Project dbt packages are pinned

`dbt/project-packages.yml` SHALL hold one key, `packages`, a list whose every entry is either a dbt hub package, `package: <owner>/<name>` with `version` set to one exact release `X.Y.Z`, or a public git repository, `git: https://...` with `revision` set to a full 40-character commit id and an optional `subdirectory`. Resolution SHALL fail with exit 6, naming the file and the entry, before dbt runs and without writing `packages.yml`, when an entry is unpinned (a range, a list of ranges, a partial version, a tag or a branch), names a `local`, `private` or `tarball` package, carries a key outside these, uses a URL that is not `https://` or that carries credentials or Jinja, lists a package twice, or installs under the name of a generated `local:` entry. Private git packages are out of scope: the hosts hold no git credential.

#### Scenario: Unpinned version

- **WHEN** an entry is `package: dbt-labs/dbt_utils` with `version: ">=1.0.0"`
- **THEN** `vecten dbt deps` exits 6 naming `dbt/project-packages.yml`, the package and the version, and dbt does not run

#### Scenario: Git package on a tag

- **WHEN** an entry is `git: https://github.com/acme/helpers.git` with `revision: v1.2.0`
- **THEN** resolution fails with exit 6 asking for a 40-character commit id

#### Scenario: Collision with a generated entry

- **WHEN** an entry would install a package named `vecten_convention`, or named like an installed extension's dbt project
- **THEN** resolution fails with exit 6 naming the package and the generated `local:` entry that already installs it

### Requirement: dbt against a deployed environment

`vecten dbt --env <env> <args>` SHALL run dbt against the environment's own Snowflake warehouse as its `LOADER` service user, signing in with `SNOWFLAKE_LOADER_PRIVATE_KEY` read from that environment's secrets store with the human's cloud login, in the database and warehouse `snowflake_names` gives for the environment, with the profile's `local` target, as the dbt DAG does. The credential SHALL reach dbt through the process environment only, also under a `DBT_ENV_SECRET_` variable so dbt scrubs it from what it logs and prints, and SHALL NOT be written to `profiles.yml`, any other file, or vecten's output. On a Postgres project it SHALL refuse with exit 4 before any cloud call, because a deployed Postgres warehouse sits on the deployment's private network. A project without deploy answers SHALL be refused with exit 4.

#### Scenario: Debug against prod

- **WHEN** `vecten dbt --env prod debug` runs in a deployed Snowflake project
- **THEN** dbt connects as `LOADER` to the `prod` account's database and `<PROJECT>_PROD_WH`, and no file in the project holds the private key

#### Scenario: Any command on another environment

- **WHEN** `vecten dbt --env staging build --select marts` runs
- **THEN** dbt builds the selection in the `staging` environment's warehouse and `vecten` exits with dbt's code

#### Scenario: Postgres project

- **WHEN** `vecten dbt --env prod compile` runs in a Postgres project
- **THEN** it exits 4 naming the private network, reads no secret and runs no dbt

### Requirement: Prod runs build nothing

On the environment named `prod`, `vecten dbt --env prod` SHALL run only the dbt commands `clean`, `compile`, `debug`, `deps`, `docs generate`, `docs serve`, `list` (`ls`), `parse`, `show`, `source freshness` and `test`, read with dbt's own argument parser, and SHALL refuse every other command, and arguments that name no command, with exit 4 before it reads any secret. It SHALL refuse `test` given `--store-failures` or `--exclude-resource-type`, SHALL run `test` with unit tests excluded through `DBT_EXCLUDE_RESOURCE_TYPES`, since each unit test creates a temporary table, and SHALL drop an inherited `DBT_STORE_FAILURES`. Before `test` or `source freshness`, which execute the project's on-run hooks, it SHALL parse the project with `dbt parse`, without the credential and with the run's `--vars`, and refuse with exit 4, naming them, when any package declares an `on-run-start` or `on-run-end` hook or, for `test`, when any test stores its failures through `store_failures` or `store_failures_as` set to `table` or `view`.

#### Scenario: Read-only command

- **WHEN** `vecten dbt --env prod test --select company` runs in a project without hooks or stored failures
- **THEN** dbt runs the selected data tests as `LOADER` with unit tests excluded, and `vecten` exits with dbt's code

#### Scenario: A command that builds

- **WHEN** `vecten dbt --env prod run`, `build`, `seed`, `snapshot`, `clone`, `retry`, `run-operation` or a command not on the list is run
- **THEN** it exits 4 naming the command and the commands prod allows, and reads no secret

#### Scenario: A hook that writes

- **WHEN** the project's `dbt_project.yml` declares an `on-run-end` grant and `vecten dbt --env prod test` runs
- **THEN** it exits 4 naming the hook, and dbt's test command does not run

#### Scenario: Stored failures

- **WHEN** a test is configured with `store_failures: true` and `vecten dbt --env prod test` runs
- **THEN** it exits 4 naming the test

#### Scenario: Commands that never execute hooks

- **WHEN** the project declares hooks and `vecten dbt --env prod compile` runs
- **THEN** dbt compiles the project, because `compile`, `show` and `docs generate` never execute on-run hooks
