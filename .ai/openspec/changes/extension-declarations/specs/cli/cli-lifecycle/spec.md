## ADDED Requirements

### Requirement: lock

`vecten lock` SHALL resolve the project's dependency records from `vecten.toml` and the installed extensions' metadata, and write them together or not at all. It SHALL run from the project with Docker and the release's base images available locally, as `extension add` does. The records are:

- `requirements.txt`, one exact pin per extension;
- `requirements.lock`, for the CLI's environment on any operating system, from each extension with its `cli` extra, the connectors' dependencies, `[dependencies.cli] python` and the release's `vecten`;
- `requirements-airflow.lock`, when `platform` is a component, for Linux against the Airflow base image's installed distributions, from each extension with its `platform` extra, the connectors' dependencies and `[dependencies.platform] python`;
- `requirements-mcp.lock`, when `mcp` is a component, for Linux against the MCP base image's installed distributions, from each extension with its `mcp` extra, the connectors' dependencies and `[dependencies.mcp] python`;
- `extensions.lock.json`, recording for each extension its version, the extras it provides, and its manifest's secrets and settings schema.

An extra SHALL be requested only when the extension's wheel provides it. Each lock SHALL start with a line recording a hash of its inputs: its roots, its base image, the release and the Python version. Each resolution SHALL keep the pins of the committed lock where they still satisfy the inputs. `--upgrade <package>`, which may be repeated, SHALL let that package move. The command SHALL then check settings and declared secrets as the `shared/extension-points` spec states. It SHALL print the secrets declared but not yet set locally, the secrets declared for a component the project lacks, and, with `--json`, the packages pinned at different versions in different locks.

#### Scenario: Project requirement added

- **WHEN** a person adds `polars>=1,<2` to `[dependencies.platform] python` and runs `vecten lock`
- **THEN** `requirements-airflow.lock` pins `polars`, `requirements-mcp.lock` and `requirements.lock` do not, and every pin already in the locks that still satisfies its inputs is unchanged

#### Scenario: Upgrade one package

- **WHEN** `vecten lock --upgrade httpx` runs
- **THEN** `httpx` may move in every lock and no other pin changes unless the move requires it

#### Scenario: Resolution fails

- **WHEN** a `[dependencies.mcp] python` entry conflicts with a package the MCP base image installs
- **THEN** the command exits non-zero naming the MCP lock, and no record changes

#### Scenario: Existing project's first lock

- **WHEN** `vecten lock` runs in a project whose locks were written by an earlier release
- **THEN** `requirements.txt` holds only the extension pins, every lock starts with its inputs line, and `extensions.lock.json` exists

### Requirement: Dependency sections of vecten.toml

`[dependencies.platform]` and `[dependencies.mcp]` SHALL each accept `python`, a list of requirements, and `system`, a list of operating-system package names. `[dependencies.cli]` SHALL accept `python` only. A Python requirement SHALL be a name with optional extras and version constraint, refusing URLs, paths, options and markers. A system package SHALL be a Debian package name, optionally followed by `=` and a version, and nothing else. A section for a component the project does not enable SHALL be refused.

#### Scenario: System package for the CLI

- **WHEN** `vecten.toml` sets `[dependencies.cli] system = ["libgomp1"]`
- **THEN** every command except `init` exits with the configuration error code, saying Vecten does not install operating-system packages on the laptop

#### Scenario: MCP section without the component

- **WHEN** a platform-only project sets `[dependencies.mcp] python = ["rapidfuzz"]`
- **THEN** every command except `init` exits with the configuration error code naming `dependencies.mcp`

### Requirement: Recorded facts keep vecten.toml's comments

A command that records a fact in `vecten.toml`, such as `bootstrap` or `user add`, SHALL change only the values it records, keeping every comment, blank line and the order of tables and keys elsewhere in the file.

#### Scenario: A comment on a setting survives a user add

- **WHEN** `vecten.toml` has a comment above `[settings.acme-resolution]` and `vecten user add` records a new user
- **THEN** the file still holds that comment and the settings table unchanged, and holds the new user

## MODIFIED Requirements

### Requirement: extension add

`vecten extension add <package>` SHALL record the package under `extensions` in `vecten.toml`, pin it in `requirements.txt`, write the locks and `extensions.lock.json` as `vecten lock` does, and wire every entry point group it declares among `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks`, `vecten.skills` and `vecten.manifest`. A package declaring none of these groups, or `vecten.manifest` alone, SHALL be refused; an unknown group is reported, not refused. The command's output SHALL name each secret the extension declares that is not yet set locally, with its `vecten secret set` command.

#### Scenario: Extension with several parts

- **WHEN** `vecten extension add vecten-ext-entity-resolution` runs and the package declares `vecten.dbt_packages`, `vecten.dags` and `vecten.osi`
- **THEN** `vecten.toml` lists the extension, the requirements include it, and `next` reports no unwired part

#### Scenario: Package without entry points

- **WHEN** `vecten extension add requests` runs
- **THEN** the command exits non-zero and lists the ten groups an extension may declare

#### Scenario: Secrets to set

- **WHEN** `vecten extension add acme-resolution` adds an extension whose manifest declares `ACME_API_KEY`, and `.env` lacks it
- **THEN** the output names `vecten secret set ACME_API_KEY`, and `next` reports it for every environment until it is set there
