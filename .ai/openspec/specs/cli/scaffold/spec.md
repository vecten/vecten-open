# cli/scaffold Specification

## Purpose
The scaffold that `init` renders: its parameters, all from the intake, the layout of the rendered project for each set of components, what is deliberately absent from it, and the `CLAUDE.md` and `vecten.toml` every project carries.

## Requirements

### Requirement: Scaffold parameters are the intake answers

The scaffold SHALL take these parameters and no others: `cloud` (`aws` or `scaleway`), `region` (defaulting per cloud), `warehouse` (`postgres` by default or `snowflake`), `project_name`, `domain`, `dns_zone`, `github_repository`, `users`, `sources` (a list of names, possibly empty), `size` (`small`, `medium` or `large`, derived from `users` unless given), `components`, `profile`, `has_warehouse` and `warehouse_url`. Any other setting SHALL take a default at render time.

#### Scenario: Defaults

- **WHEN** `init` is given no warehouse, no region, no size, no components and no profile, with a small number of users
- **THEN** the project renders for Postgres in the chosen cloud's default region at the `small` tier with both components under the `standard` profile

#### Scenario: Unsupported cloud

- **WHEN** `cloud` is given as `digitalocean`
- **THEN** the render is refused with a message listing the two supported clouds

#### Scenario: Unsupported warehouse

- **WHEN** `warehouse` is given as `bigquery`
- **THEN** the render is refused with a message listing `postgres` and `snowflake`

#### Scenario: Unknown component

- **WHEN** `components` names anything other than `platform` and `mcp`
- **THEN** the render is refused with a message listing the two components

### Requirement: Rendered project layout

A rendered project SHALL contain `orchestration/` with `docker-compose.override.yml`, `osi/` with `datasets.json` and `metrics.json`, `evals/`, `skills/`, `docs/` with `schema-convention.md` and `runbook.md`, `.github/workflows/` with the ci workflow, `CLAUDE.md` and `vecten.toml`. With the `platform` component it SHALL also contain `connectors/`, `orchestration/dags/`, `dbt/` and `.mcp.json`, which declares the Airflow and dbt MCP servers. The deploy layer is rendered separately.

#### Scenario: Layout after init with both components

- **WHEN** `init` completes for any cloud and warehouse with both components and the deploy answers present
- **THEN** each path listed in the requirement exists in the project root
- **AND** `.github/workflows/` contains the ci, terraform-plan, ship, terraform-apply and deploy-platform workflows

#### Scenario: MCP-only layout

- **WHEN** `init` completes with `components = ["mcp"]`
- **THEN** `connectors/`, `orchestration/dags/`, `dbt/` and `.mcp.json` are absent
- **AND** `orchestration/docker-compose.yml` carries the `mcp` and `local` profiles only
- **AND** `osi/datasets.json` and `osi/metrics.json` exist as empty documents

#### Scenario: Environment file filled

- **WHEN** `deploy/envs/prod.tfvars` is opened after `init`
- **THEN** it holds the domain, the DNS zone, the region, the GitHub repository, the components and the profile
- **AND** a Snowflake project's file also holds the Snowflake account

### Requirement: The deploy layer needs the deploy answers

The deploy layer, `deploy/` with `terraform/` and `envs/` plus the terraform-plan, ship, terraform-apply and deploy-platform workflows, SHALL be rendered only once the deploy answers are present, by `init` from a complete intake or by `init --deploy` later, with `deploy/envs/prod.tfvars` fully filled and no placeholder left.

#### Scenario: Layout before the deploy answers

- **WHEN** `init` completes from the local answers alone
- **THEN** `deploy/` is absent and `.github/workflows/` holds only the ci workflow
- **AND** every other path listed in the requirement exists

### Requirement: Only the chosen cloud is rendered

The rendered project SHALL contain `deploy/terraform/<cloud>/` for the chosen cloud only, and `deploy/terraform/snowflake/` only when `warehouse` is `snowflake` and the `platform` component is present. Terraform and workflow files for other clouds MUST NOT be present.

#### Scenario: AWS with Postgres

- **WHEN** `init` is run with `cloud=aws` and `warehouse=postgres`
- **THEN** `deploy/terraform/aws/` exists
- **AND** no `deploy/terraform/scaleway/` or `snowflake/` exists

#### Scenario: Scaleway with Snowflake

- **WHEN** `init` is run with `cloud=scaleway` and `warehouse=snowflake` and both components
- **THEN** `deploy/terraform/scaleway/` and `deploy/terraform/snowflake/` both exist

#### Scenario: MCP-only over an existing Snowflake

- **WHEN** `init` is run with `has_warehouse=yes` against a Snowflake account
- **THEN** no `deploy/terraform/snowflake/` is rendered, because the warehouse already exists

### Requirement: The project holds only what its owner edits

The scaffold SHALL render only files a project owner edits or an agent reads: `vecten.toml`, `deploy/envs/*.tfvars`, the connectors and their DAGs, the dbt project, `osi/`, `evals/`, `CLAUDE.md`, `skills/`, `.mcp.json` in a project with the `platform` component, `docs/` and `orchestration/docker-compose.override.yml`. Everything else SHALL be owned by the installed packages and referenced by version.

#### Scenario: Package-owned parts

- **WHEN** a rendered project is inspected
- **THEN** `deploy/terraform/<cloud>/` is a thin root module sourcing `vecten-open//terraform/<cloud>?ref=v<version>`, the `vecten_convention` dbt package is pinned as the installed `vecten-platform` version, checked against the versions table, with `dbt/packages.yml` generated before each dbt run and git-ignored, no Compose file, NGINX configuration or sync helper is present, and `up` generates the stack from `vecten.toml`

#### Scenario: Security fix in a module

- **WHEN** a new `vecten` release fixes a Terraform module and a project runs `update`
- **THEN** only the `ref` in the thin root module changes and no project file conflicts

#### Scenario: Edited project survives update

- **WHEN** a project owner has edited a DAG and a mart and runs `update`
- **THEN** those files are untouched and the version pins move

### Requirement: The rendered project holds only the product's files

The rendered project SHALL contain only the files the product defines for its cloud, warehouse, components and profile: the project's configuration and documentation, the platform's connector, orchestration and dbt skeleton when the `platform` component is chosen, the semantic model and its evaluation cases, the deploy layer when it is rendered, and the vendored skills. The project carries the `mcp` Compose profile and its configuration, never the MCP server's source code.

#### Scenario: Only the product's files

- **WHEN** any combination of cloud, warehouse, components and profile is rendered
- **THEN** every file outside `skills/` belongs to the set the product defines for that combination
- **AND** `connectors/` holds only its package marker, and `orchestration/dags/` only the dbt build DAG

#### Scenario: MCP server code absent

- **WHEN** a rendered project with the `mcp` component is searched for server source files
- **THEN** only the Compose profile, its environment and the `osi/` documents reference the `mcp` component

### Requirement: Rendered project is valid without edits

Every combination of cloud, warehouse, components and profile SHALL render a project whose dbt project parses when present, whose Python and YAML lint cleanly, whose `osi/` documents pass `vecten mcp validate`, and whose Terraform passes `terraform validate`, with no edits to generated files.

#### Scenario: Render matrix

- **WHEN** every cloud, warehouse, components and profile combination is rendered
- **THEN** each one lints and passes `terraform validate`
- **AND** each one with the `platform` component has a dbt project that parses
- **AND** each one passes `vecten mcp validate`

### Requirement: Run records live under .vecten, deploy facts in vecten.toml

The CLI SHALL keep its run records, the outcomes of `up`, `connector run`, `dbt-build-local`, `secret set --env` (names only) and `mcp validate` that `next` reads, under `.vecten/` in the project root, and the scaffold's `.gitignore` SHALL exclude that directory. Facts a second clone must see, whether an environment is bootstrapped, its deployed commit and whether its first user and connector access were applied, SHALL live under `[environments.<env>]` in `vecten.toml` and be committed.

#### Scenario: Records ignored by git

- **WHEN** `connector run` has completed in a rendered project
- **THEN** a run record exists under `.vecten/`
- **AND** `git status` does not list it

#### Scenario: User fact committed

- **WHEN** `vecten user add` has completed for `prod`
- **THEN** `vecten.toml` records `first_user_added = true` under `[environments.prod]` and holds no credential

### Requirement: CLAUDE.md describes the convention and skills

The rendered `CLAUDE.md` SHALL describe the schema convention and the five skills `connector`, `model`, `semantic`, `operate` and `guide` so that a user's own agent follows them without the published plugin installed. It SHALL state the naming rules the convention test cannot check: full words and singular nouns for core entities.

#### Scenario: Agent without the plugin

- **WHEN** an agent opens the project with only the rendered `CLAUDE.md` and `skills/`
- **THEN** it can find the convention version, the naming rules and the entry point skill from `CLAUDE.md` alone

### Requirement: vecten.toml holds editable project configuration

`vecten.toml` SHALL hold the warehouse, users and size, `components`, `profile` defaulting to `standard`, the chosen cloud once the deploy answers are given, `extensions`, the `wanted` source names from the intake that have not yet been added, the declared connectors (each with its name, `source` and `access.groups` as the connectors spec defines), and every defaulted setting, and the CLI SHALL read changes to it on the next invocation without re-rendering.

#### Scenario: Edit after init

- **WHEN** a user changes a defaulted setting in `vecten.toml`
- **THEN** the next CLI command uses the changed value
- **AND** no re-render is required

#### Scenario: Profile changed by hand

- **WHEN** a user changes `profile` from `standard` to `minimal` and runs `deploy --env prod --plan-only`
- **THEN** the plan shows one VM where there were two

#### Scenario: Malformed configuration

- **WHEN** `vecten.toml` fails to parse
- **THEN** every command except `init` exits non-zero and names the file and the parse error

### Requirement: Pristine upgrade metadata

`init` SHALL commit no user data into upgrade metadata. It SHALL generate
`.copier-baseline.json` containing the pristine rendered template files and release version,
beside `.copier-answers.yml`. These metadata files belong in the project repository.
`init --deploy` SHALL advance only the baseline entries for the files it changes.
`update` SHALL merge in a temporary directory with Copier, validate the result, and report
conflicts without changing any project file or version pin. Symlinked participating paths
and unsafe metadata paths SHALL be refused before writes.

#### Scenario: Conflicting upgrade

- **WHEN** both the owner and a newer template change the same part of a generated file
- **THEN** `update` lists that file, exits 4 and leaves every project file unchanged

#### Scenario: Internal project predating metadata

- **WHEN** upgrade metadata is absent and the installed CLI matches the saved scaffold release
- **THEN** `update` reconstructs a pristine baseline from the saved answers and reports adoption
- **AND** a newer CLI refuses to guess the missing older baseline
