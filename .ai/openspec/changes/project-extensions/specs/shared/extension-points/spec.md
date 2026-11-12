## MODIFIED Requirements

### Requirement: Extensions are pip dependencies everywhere

An extension SHALL be resolved as an ordinary pip dependency, at the same version on a laptop, in CI, in the Airflow image and in the MCP image. It SHALL come from a package index, or from a directory under the project's `extensions/` folder, whose version is the project commit. Nothing from an extension that comes from an index SHALL be vendored into the project; an in-repo extension is the project's own code. The extension's own dependencies SHALL be scoped per component through its standard extras: the extra `platform` is installed in the Airflow image, `mcp` in the MCP image and `cli` in the CLI's environment, each only where the wheel provides it, while its base dependencies are installed in all three. Every third-party package, including an in-repo extension's dependencies, SHALL come from the package index: public PyPI, or the private index when `[packages] private_index` is set. The credential for a private package index SHALL come from the secrets store.

#### Scenario: Same version everywhere

- **WHEN** an extension is pinned in the project's requirements
- **THEN** the laptop, CI, the Airflow image and the MCP image install that same version

#### Scenario: Same commit everywhere

- **WHEN** a project with the in-repo extension `extensions/acme-portal` is shipped at commit `a3f9c2e`
- **THEN** the CLI, the local images and every host's images run `extensions/acme-portal` as it is at `a3f9c2e`

#### Scenario: Heavy dependency for the DAG only

- **WHEN** an extension declares `lightgbm` in its `platform` extra and nothing in `mcp` or `cli`, and `vecten lock` runs
- **THEN** `requirements-airflow.lock` pins `lightgbm`, and neither `requirements-mcp.lock` nor `requirements.lock` holds it

#### Scenario: Extension without extras

- **WHEN** an extension declares no `platform`, `mcp` or `cli` extra
- **THEN** its dependencies are pinned in every enabled component's lock, as before

#### Scenario: Update

- **WHEN** an index extension's version is bumped in the requirements, or an in-repo extension's dependencies change in its `pyproject.toml`
- **THEN** after `vecten lock` no file under the project changes other than the requirements, the lockfiles and `extensions.lock.json`

## ADDED Requirements

### Requirement: In-repo extensions are registered only from the project's extensions folder

`vecten extension add <path>` SHALL accept a path only when, after normalising it, it names a directory exactly one level under `<project>/extensions/`, the argument holds no `..` component, neither the path nor anything under it is a symbolic link, and the directory's name equals the normalised distribution name in its `pyproject.toml`. That `pyproject.toml` SHALL declare `[build-system]` and a static `[project]` name and version. `vecten lock`, `up` and `ship` SHALL apply the same checks to every recorded in-repo extension. Any other path SHALL be refused with a message naming the rule it breaks.

#### Scenario: Register an in-repo extension

- **WHEN** `vecten extension add ./extensions/acme-portal` runs and the package there is named `acme-portal`
- **THEN** `vecten.toml` lists `acme-portal`, `requirements.txt` holds `./extensions/acme-portal`, and its entry points are wired as for any extension

#### Scenario: Path outside the folder

- **WHEN** `vecten extension add ../shared/acme-portal` or `vecten extension add ./tools/acme-portal` runs
- **THEN** the command exits non-zero saying in-repo extensions live under `extensions/<name>/`, and no file under the project changes

#### Scenario: Symbolic link

- **WHEN** `extensions/acme-portal/src/acme_portal/static` is a symbolic link
- **THEN** `vecten extension add ./extensions/acme-portal` exits non-zero naming the link

#### Scenario: Name and directory differ

- **WHEN** `extensions/portal/pyproject.toml` names the distribution `acme-portal`
- **THEN** the command exits non-zero saying the directory must be `extensions/acme-portal`

### Requirement: An in-repo extension is recorded by its path, and the locks hold only third-party packages

For an in-repo extension, `vecten.toml` SHALL list its name and extras, `requirements.txt` SHALL hold its relative path with its extras (`./extensions/<name>[extras]`), and `extensions.lock.json` SHALL record its path, version, extras and the sha256 of its `pyproject.toml`. `vecten lock` SHALL resolve the locks with each in-repo extension as an input and SHALL write into them only third-party packages, so that no lock line carries a path or a URL. Each lock's recorded input hash SHALL include the `pyproject.toml` hash of every in-repo extension in its inputs, so that a change to an in-repo extension's metadata makes the locks stale and a change to its code does not. `vecten lock` SHALL refuse when a lock would pin a package from an index under an in-repo extension's name.

#### Scenario: Dependency added to an in-repo extension

- **WHEN** `httpx` is added to `extensions/acme-portal/pyproject.toml` and `vecten up` runs without `vecten lock`
- **THEN** `up` exits non-zero naming `vecten lock`, and after `vecten lock` the locks pin `httpx` and hold no line for `acme-portal`

#### Scenario: Code change needs no lock

- **WHEN** only a module under `extensions/acme-portal/src/` changes
- **THEN** `vecten up` and `vecten ship` accept the existing locks

#### Scenario: Name taken on the index

- **WHEN** another extension depends on `acme-portal`, which is also the name of a package on PyPI
- **THEN** the locks hold no `acme-portal` line, and every install takes `acme-portal` from `extensions/acme-portal`

### Requirement: An in-repo extension is installed from the project everywhere

The CLI's environment SHALL install the CLI lock with `--no-deps` and each in-repo extension editable with `--no-deps`. Each project-layer image SHALL install its lock with `--no-deps` and then build and install each in-repo extension from its source with `--no-deps`, on the architecture of the machine that builds the image, with the build requirements that `vecten lock` pinned. The extension's DAGs, dbt packages, Ossie documents and skills SHALL be found through its entry points in the installed package, as for any extension; skill links SHALL stay machine-local and uncommitted.

#### Scenario: Every kind of part from an in-repo extension

- **WHEN** an in-repo extension declares `vecten.dags`, `vecten.dbt_packages`, `vecten.osi`, `vecten.skills` and `vecten.mcp.tools`, and `vecten up` runs
- **THEN** its DAG runs in Airflow, its dbt models build, its Ossie document is merged, its skill appears under `skills/` and in the plugin, and its tool is offered

#### Scenario: Edit seen by the CLI at once

- **WHEN** a skill file under `extensions/acme-portal/` is edited
- **THEN** the linked skill shows the edit without any command being run

### Requirement: In-repo extensions are documented

`docs/extensions.md` SHALL describe in-repo extensions: the `extensions/<name>/` layout, `vecten extension add ./extensions/<name>`, `vecten extension new` with and without a template, how dependencies are locked, that resources must be package data, and what a reviewer checks in a change to an in-repo extension. It SHALL describe templates in general terms and SHALL name no private repository.

#### Scenario: Reader starts an extension

- **WHEN** a reader follows the page from an empty project
- **THEN** they create, register, run with `vecten up` and ship an in-repo extension without other sources
