# Installing and writing extensions

A Vecten extension is an installed Python package that contributes connectors, models,
scheduled work, [semantic documents](GLOSSARY.md), skills or MCP server hooks. Its code stays
in the package rather than being copied into each project. Only install packages you trust:
loading their entry points executes Python with the current process's permissions.

## Install and update

First create a project with `vecten init` using the [README](../README.md#try-it-on-your-laptop).
Run the command below from its directory containing `vecten.toml` (or a subdirectory), using
the Python environment that runs the Vecten CLI. Before adding an extension, start Docker
and pull the base images of the installed release (`vecten version` prints it), one per
enabled component:

```sh
docker pull ghcr.io/vecten/vecten-airflow:<version>
docker pull ghcr.io/vecten/vecten-mcp:<version>
export VECTEN_IMAGE_REGISTRY=ghcr.io/vecten
```

From a checkout of this repository, `vecten dev build-images` builds them instead. Then add the extension:

```sh
vecten extension add 'acme-resolution==1.2.0' --json
```

The command first stages the package in a temporary directory and checks its entry points.
It then resolves all locks before installing into the Python environment running the CLI.
A package with no known entry points, or a dependency-resolution failure, leaves that
Python environment and the project's records unchanged. Unknown Vecten groups are reported
while the known groups remain usable.

`vecten.toml` registers package names and extras; the dependency records the command writes
are described under [Dependencies and locks](#dependencies-and-locks). Repeat `extension add`
with a new version to upgrade: registration stays unchanged and the records change.

A successful command installs the CLI lock into the environment running Vecten. If several
projects share that environment, their dependencies are shared too: adding an extension in
one project can change versions used by another. Use a separate virtual environment per
project. Installation failures or failures while linking resources can leave that Python
environment changed; the command does not attempt to uninstall packages or restore earlier
versions. Project record writes are restored if writing those records fails.

On another laptop or in CI, install the committed CLI lock into the project's environment:

```sh
uv pip install --python /path/to/project/venv/bin/python -r requirements.lock
```

Machine-local skill links point into that environment's installed packages. Re-run the same
`extension add` after moving/recreating the environment to rebuild the links. Do not commit
absolute skill links. Extension Python code and resource files are never vendored into the
project. Skill links are exposed under both `skills/<entry-name>` and
`plugins/vecten/skills/<entry-name>`; the latter has a plugin manifest and can be loaded with
`claude --plugin-dir plugins/vecten`. Existing project-owned skill directories and symlinks are preserved: a collision refuses
the command. Generated links are refreshed or removed only while their targets still match
the automatically generated `.vecten/extension-skills.json` ownership record; you do not
need to maintain it manually.

## Extensions in the project

An extension can live in the project itself, under `extensions/<name>/`, as a normal Python
package that the project owns and changes in its own pull requests. Nothing about it is
published or fetched from an index.

```text
extensions/acme-portal/
├── pyproject.toml            # static name and version, [build-system], entry points, extras
├── README.md
├── src/acme_portal/          # the package; DAGs, dbt projects, Ossie documents and skills
│   ├── __init__.py           # live inside it as package data
│   └── skills/acme-portal/SKILL.md
└── tests/
```

**Create one.** `vecten extension new acme-portal` copies a minimal skeleton packaged with the
CLI and registers it. `--template <dir>` or
`--template 'git+https://<host>/<repo>@<tag>#subdirectory=<dir>'` copies a template instead,
read once with your own git setup; nothing records where it came from, so name the template and
its tag in your commit message. A template is an extension package whose names use four
placeholders, replaced in text files and in file and directory names: `__vecten_name__`
(`acme-portal`), `__vecten_package__` (`acme_portal`), `__vecten_title__` (`Acme Portal`) and
`__vecten_version__` (the running Vecten Open). Other files, such as images, are copied byte for
byte, and dbt's Jinja is untouched. An optional `vecten-template.toml` with `requires_vecten` is
checked and not copied. `--no-add` copies without registering.

**Register one.** `vecten extension add ./extensions/acme-portal` accepts only a directory
directly under `extensions/` whose name is the package's normalised name, with no `..` and no
symbolic link anywhere under it, and a `pyproject.toml` with a `[build-system]` and a static name, version, dependencies, extras and
entry points: those decide what is locked and wired, and only this file is hashed. `vecten.toml` lists `acme-portal`; `requirements.txt` holds
`./extensions/acme-portal`; `extensions.lock.json` records its path, version, the hash of its
`pyproject.toml` and its build requirements, pinned. `vecten lock`, `up` and `ship` check the
same rules again.

**Dependencies.** The locks are resolved with the in-repo extension as an input and hold only
third-party packages, never a line for the extension itself, and a lock that would take a
package of the same name from an index is refused. Changing `pyproject.toml` (dependencies,
extras, entry points) makes the locks stale until `vecten lock`; changing code does not.

**Where it runs.** Vecten's own environment installs it editable, so the CLI, its skills and
`vecten connector run` see every edit at once. The images install the lock with `--no-deps`, then
build the extension from `extensions/` on the machine that builds them, with the pinned build
requirements; the bundle carries `extensions/` without `dist`, `build` or `*.egg-info`. A
package that compiles code needs a compiler in the image, from `[dependencies.<component>]
system`. Because the images install the package rather than run its source, every resource it
serves must be package data inside `src/<package>/`.

**Reviewing a change** to an in-repo extension is reviewing project code: read the diff under
`extensions/<name>/`, and treat a `pyproject.toml` change like any dependency change, with the
lock diff beside it.

## Dependencies and locks

`vecten lock` needs Docker and the release's base images, as `extension add` does. It writes
these records together, or changes none of them:

| File | What it holds |
|---|---|
| `requirements.txt` | One exact pin per extension, such as `acme-resolution==1.2.0`. |
| `requirements.lock` | The CLI's environment, for any operating system on Python 3.14: each extension with its `cli` extra, the connectors' dependencies, `[dependencies.cli] python` and the release's `vecten`. |
| `requirements-airflow.lock` | The Airflow image, for Linux, against the packages its base image installs: each extension with its `platform` extra, the connectors' dependencies and `[dependencies.platform] python`. |
| `requirements-mcp.lock` | The MCP image, for Linux, against its base image: each extension with its `mcp` extra, the connectors' dependencies and `[dependencies.mcp] python`. |
| `extensions.lock.json` | Each extension's version and the extras its wheel provides. |

Only enabled components get an image lock. Before writing them, the command installs the new
`requirements.lock` into the environment running Vecten; it adds and upgrades packages there
but does not remove ones the lock no longer holds. `extension add` ends by running the same
steps, and `connector add` runs them when the project already has locks and the new connector
brings dependencies. Commit every one of these files.

**Project requirements.** A package the project's own code needs goes in `vecten.toml`, under
the component that imports it:

```toml
[dependencies.platform]   # DAGs and connectors in Airflow
python = ["polars>=1,<2"]

[dependencies.mcp]        # the MCP server
python = ["rapidfuzz>=3,<4"]

[dependencies.cli]        # vecten connector run, local dbt, anything a skill imports
python = ["polars>=1,<2"]
```

Entries are names with optional extras and a version range; URLs, paths, pip options and
markers are refused, and so is a section for a component the project does not enable. Edit the
section, then run `vecten lock`.

**Operating-system packages.** A Python package that needs a system library, such as
`libgomp1`, gets it from `system` in the image's section:

```toml
[dependencies.platform]
python = ["lightgbm>=4,<5"]
system = ["libgomp1"]
```

Each image's project layer installs these with `apt-get` as root, before the Python packages,
removes apt's package lists, and switches back to the image's own user (`airflow` or `app`),
on the laptop and on every VM alike. Entries are Debian package names, optionally pinned as
`name=version`; anything else, such as a shell character or an apt option, is refused when
`vecten.toml` loads. `[dependencies.cli]` takes no `system`: Vecten does not install packages
on your laptop. The Airflow and MCP base images are both Debian-based but may follow different
Debian releases, so a package can be named or versioned differently in each; check with
`docker run --rm --entrypoint cat <image> /etc/os-release`. An unpinned package installs the
release's current version at build time, so two builds of one commit can differ; pin with
`=version` when that matters. A package that a later base image's release drops makes the
layer build fail on the VM, and the ship keeps the previous release running. Extensions cannot
add system packages: one that needs a library says so in its documentation, and the project
lists it here, so no package install adds root build steps the project did not write.

**Keeping pins.** Each lock is resolved starting from the committed one, so an existing pin
stays while it still satisfies the inputs: adding one package moves nothing else unless the
resolution needs it. `vecten lock --upgrade httpx` lets one package move in every lock; repeat
`--upgrade` for several. An extension's own version changes only through `extension add`.

**Stale locks.** Each lock starts with a line such as
`# vecten-lock: component=platform inputs=<sha256>`, a hash of its inputs: the requirements
above, the base image, the Vecten release and the Python version. `vecten up` and the bundle that
`ship` builds recompute it for the image locks from `vecten.toml`, `requirements.txt` and
`extensions.lock.json` and refuse one that no longer matches, naming the file and
`vecten lock`. `vecten next` offers the command whenever any of the records, the CLI's lock
included, is out of date. A `vecten update` to a new release therefore asks for `vecten lock` too.

**Different versions per lock.** Every extension has the same version in every lock. Other
packages may differ, because each image keeps the versions its base image installs.
`vecten lock --json` lists them under `divergence`, for review.

**Projects locked by an earlier release.** Their locks have no inputs line and their
`requirements.txt` also holds connector dependencies. They keep building, checked name by name
as before, until the first `vecten lock`, which rewrites `requirements.txt` to the extension
pins and writes every lock with its inputs line and `extensions.lock.json`.

## Declare package entry points

Python entry points map a group and name to an importable object. For example:

```toml
[project.entry-points."vecten.dbt_packages"]
resolution = "acme_resolution.resources:dbt_directory"

[project.entry-points."vecten.mcp.auth"]
company_login = "acme_resolution.auth:identify"
```

`dbt_directory` is a `pathlib.Path` or string pointing to the installed package's dbt folder.
Include resources in the wheel; do not return checkout-relative paths. The extension must
also appear in `vecten.toml`: an unrelated installed package cannot register itself. Entry
names within a group must be unique, and installed versions must satisfy any version constraints in the configuration. Use the
corresponding environment lock to install the recorded package versions.

| Group | Exported object and consumer |
|---|---|
| `vecten.connectors` | A dictionary, or zero-argument factory returning one, with `kind`, optional `secrets`/`dependencies` lists, optional `groups` (below) and `write(target: Path)`. `connector add` uses the writer to create the project's connector adapter, and renders one sync DAG per group. |
| `vecten.dbt_packages` | Installed dbt directory. Added as a `local:` dependency before dbt runs. |
| `vecten.dags` | Installed directory of Python DAG files. The Airflow loader imports actual DAG objects, including DAGs created by calling `@dag` functions at module level; task objects are excluded and duplicate DAG IDs are refused. Use absolute package imports in DAG files. |
| `vecten.osi` | Installed directory of JSON semantic documents. Shared discovery and validation merge these with project `osi/` files and reject duplicate definitions. |
| `vecten.skills` | Installed directory containing `SKILL.md` and any supporting files. The entry name becomes the project/plugin skill link; built-in skill names (`connector`, `model`, `semantic`, `operate`, `guide`) are reserved. |
| `vecten.mcp.tools` | Dictionary with callable `handler` and `groups: list[str]`. The server's tool registry returns it only when the caller's groups intersect the declaration. An empty list exposes it to nobody. |
| `vecten.mcp.auth` | Callable identity backend. Selected by `auth = "company_login"` in the `[mcp]` section of `vecten.toml`; the registry refuses a missing backend at startup. Extensions cannot replace the reserved `password`/`oidc` names. |
| `vecten.mcp.provisioning` | Callable provisioning hook. The registry invokes the named hook with the owning service's arguments. |
| `vecten.mcp.audit_sinks` | Callable accepting one audit-row dictionary. The owning audit service calls the registry after persisting its local row. |
| `vecten.manifest` | A dictionary, or zero-argument factory returning one, with `secrets` and `settings`: what the extension needs, not a part it contributes (below). At most one per distribution; a package declaring only a manifest is refused. Its module imports only the standard library and `vecten_shared`. |

MCP means Model Context Protocol, the client/server interface for warehouse tools. OAuth login,
the authenticated transport, warehouse identity services, the [tool audit](mcp-audit.md) and the
[built-in tools](mcp-tools.md) are the server's; see the [identity guide](mcp-identity.md) for
the identity model and the [CLI reference](cli.md) for the commands. The transport accepts synchronous and asynchronous tool handlers;
other hooks use the synchronous contracts above. Audit rows identify the installed distribution,
and sink hooks run after local persistence on the audit writer thread. A sink should return
promptly because a slow sink delays subsequent deliveries. Use the shared query service to
attach warehouse SQL evidence, as described in the [audit extension contract](mcp-audit.md#append-only-storage-and-extensions).
Query evidence uses Python context variables to associate execution with the current tool
invocation. Direct calls and `await asyncio.to_thread(...)` preserve that association.
`loop.run_in_executor(...)` and custom thread pools do not automatically copy context;
without it, the invocation record can lack SQL, its hash and row count. Prefer
`asyncio.to_thread`, or capture `contextvars.copy_context()` inside the handler and submit
`context.run` with the query function and its arguments. Capture a separate context for each
concurrent submission, and await the query before returning from the handler so the evidence
is present when the invocation is recorded.

No CLI-command or HTTP-route extension hooks are offered yet.

### Secrets and settings

An extension says what it needs in its manifest:

```python
# acme_resolution/manifest.py
def manifest():
    return {
        "secrets": ["ACME_API_KEY", "MCP_ACME_TOKEN"],
        "settings": {
            "properties": {
                "daily_cap": {"type": "integer", "default": 1000, "minimum": 1},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": [],
        },
    }
```

```toml
[project.entry-points."vecten.manifest"]
acme-resolution = "acme_resolution.manifest:manifest"
```

The entry's name is free; a distribution declares at most one. `vecten lock` reads every listed
extension's manifest and records its secrets and settings
schema in `extensions.lock.json`, so CI and the hosts know them without installing the
extension. A malformed `secrets` or `settings` is refused, naming the extension and the key; a
key this release does not know is reported and ignored, so an extension written for a newer
release still installs.

**Where a secret is delivered.** The name's prefix decides, as it decides which host may read
it. An `MCP_` name goes to the MCP server; any other name goes to Airflow and to the CLI's own
process (`vecten connector run`). A DAG and a tool that call the same API therefore declare two
names, `ACME_TOKEN` and `MCP_ACME_TOKEN`, and the operator sets both. Refused: `DEPLOY_`,
`ADMIN_` and `SETUP_` names, which no host may read; names starting with `AWS_`, `SCW_`,
`VECTEN_` or `AIRFLOW_`, which the hosts and the stack set themselves; process names such as
`PATH`; and every credential the stack manages, such as `LOADER_PASSWORD`, `MCP_SIGNING_KEY` and
the warehouse administrator's login. A name for a component the project does not enable is reported as
not delivered. `vecten lock`, `extension add` and `vecten next` name each declared secret that is
not set yet, locally or per environment, with its `vecten secret set` command.

A project's own DAGs and MCP code declare theirs in `vecten.toml`, with the same rules:

```toml
secrets = ["SLACK_TOKEN", "MCP_CRM_TOKEN"]
```

**Settings.** Values live in `vecten.toml`, in a table named after the extension, with
per-environment overrides beside them; `local` is the laptop:

```toml
[settings.acme-resolution]
dry_run = false

[settings.acme-resolution.env.staging]
dry_run = true

[settings.acme-resolution.env.local]
dry_run = true
daily_cap = 10
```

The effective value in an environment is its override, else the table's value, else the
manifest's default. An override replaces the whole value, with no merge of nested tables.
`[settings.project]` holds the project's own values the same way, without a schema: they are
checked only for having a JSON type. TOML dates and times are refused; use strings.

**Checks.** Values are checked against the manifest's schema, which may use `properties` typed
`string`, `integer`, `number`, `boolean`, `array` or `object`, with `default`, `description`,
`enum`, `minimum`, `maximum` and `items`, plus `required`; unknown settings are always refused,
and no setting may be named `env`. `vecten lock` checks the laptop and every environment an
override names, `vecten up` the laptop, `vecten ship` and `ship --check` the environment
shipped, and the MCP server every extension's settings before it starts serving. An error
names `settings.<namespace>.<key>` and the environment, before anything starts. On a VM a change
takes effect at the next ship; on the laptop at the next read, because `vecten.toml` is mounted.

**Reading them.** DAG code, connector code and MCP tools read through one API in `vecten-shared`:

```python
from vecten_shared.context import extension, project

acme = extension("acme-resolution")
acme.settings["daily_cap"]  # the effective value for this environment, read-only
acme.secret("ACME_API_KEY")  # only names acme's manifest declares, for this component
acme.env  # "local", "prod", ...
project().settings["channel"]
project().secret("SLACK_TOKEN")
```

`secret()` refuses a name its extension, or the project, did not declare, and a name delivered
to the other component, on the laptop and on every VM alike, even where the host's cloud identity
could read it. Connector code may keep using `vecten_platform.sync.secret`.

### Dependencies per component

The CLI, the Airflow image and the MCP server all install the same version of an extension,
because each discovers its entry points. Its dependencies can still differ: declare
component-only packages in the standard extras named after the component.

```toml
[project]
dependencies = ["httpx>=0.27"]          # every component

[project.optional-dependencies]
platform = ["polars>=1,<2"]             # the Airflow image: DAGs and connectors
mcp = ["rapidfuzz>=3,<4"]               # the MCP server: tools and hooks
cli = ["polars>=1,<2"]                  # vecten connector run, local dbt, skills
```

`vecten lock` requests `acme-resolution[platform]` for the Airflow image, `[mcp]` for the MCP
image and `[cli]` for the CLI, and only the extras the wheel declares (`Provides-Extra`), so a
wheel without them locks as before. The three names are reserved: an extra called `mcp` for
another purpose would be installed in the MCP image. A connector's runtime dependencies belong
in both `platform` and `cli`, or in the base list. Prefer extras over a connector's
`dependencies` list, which goes into every lock.

### Connector groups and schedules

A connector declared without `groups` gets one group, `default`, whose sync DAG runs every
object `@daily` in UTC. `groups` splits the objects into groups, each with its own DAG
(`orchestration/dags/sync_<connector>_<group>.py`) and schedule:

```python
def write_connector(target):
    (target / "connector.py").write_text("from acme_attio.connector import source\n")


connector = {
    "kind": "custom",
    "secrets": ["ATTIO_API_KEY"],
    "write": write_connector,
    "groups": {
        "daily": {"objects": ["companies", "people"], "schedule": "0 6 * * *"},
        "hourly": {"objects": ["deals"], "schedule": "@hourly", "timezone": "Europe/Warsaw"},
        "weekly": ["notes"],
    },
}
```

Each group maps to its `objects` (an empty list syncs every object the source yields), a cron
`schedule` (default `@daily`) and an IANA `timezone` (default UTC), or to a list of objects
alone. A group name is lowercase letters, digits and underscores, starting with a letter, at
most 63 characters. `connector add` records the
groups and their objects in the project's `vecten.toml` and writes the schedules into the DAG
files, which the project then owns; rendering a declared connector again reads the schedules
from the installed extension. A group that cannot render a DAG (a bad name, objects that are not
strings, an unknown timezone or key) is refused before anything is written. Airflow checks the
cron syntax when it parses the DAG file. The DAG files' other options, such as ordered stages
and run parameters, are in [orchestration](orchestration.md#changing-a-sync-dag).

## Private package indexes

Set `[packages] private_index = true` in `vecten.toml`, then put the authenticated HTTPS index
URL in the local secrets store through the prompt or standard input:

```sh
vecten secret set PACKAGE_INDEX_URL
vecten extension add 'acme-private-extension==1.0.0'
```

The private index should mirror/proxy public dependencies as well. The index URL is supplied
to `uv` through its process environment and to Docker through an ephemeral
[BuildKit secret](https://docs.docker.com/build/building/secrets/). It is never a requirement,
build argument, Dockerfile value or bundle member. Private-index package-manager output is suppressed on
failure because it can contain authenticated URLs; public-index failures retain diagnostic
output. Extension import errors retain their exception type and chained cause for debugging. Investigate access and package/version
availability using your index's normal authentication tools.

For a deployed environment, provision `PACKAGE_INDEX_URL` in the platform secret
scope and `MCP_PACKAGE_INDEX_URL` in the MCP scope with `vecten secret set <NAME> --env <env>`.
Environment names are the keys under `[environments]` in `vecten.toml`; run `vecten bootstrap --env <env>` before setting cloud secrets, following the
[deploy pages](deploy-aws.md). The secret name selects the scope automatically:
`MCP_` names belong to the MCP, and `PACKAGE_INDEX_URL` belongs to the platform.
Each VM reads only its component's credential (a combined host reads both). These build
credentials are not passed into running application containers. CI installation likewise
needs its index URL supplied privately in `UV_INDEX_URL`, never in a committed workflow.

## Open and Enterprise boundary

Vecten Open has no licence-key checks, telemetry or disabled Enterprise implementations.
The open product includes the Apache Airflow platform and the MCP server, identity, audit,
analytics, the feedback loop that turns reported answer problems into tracked fixes,
administration and all built-in skills.

Exactly three Enterprise extensions sit outside this repository:

- Single sign-on with SAML, Okta/Entra ID directory sync, role mapping and SCIM.
- Locked-down provisioning with just-in-time credentials and script mode.
- Compliance evidence packaging with signed audit export, a compliance profile and checks.

They use the same public extension points as any third-party package. Setting `[mcp] auth = "saml"` in `vecten.toml` without
installing its extension fails startup with the backend's name. No SAML implementation is
bundled. A test in this repository's CI (`scripts/check_extension_wheels.py`) exercises a fixture declaring all ten groups and scans
shipped Python for prohibited licence-gating and telemetry symbols/imports. This automated
scan is a regression guard; code review remains necessary for behavior it cannot recognize.

Back to the [docs index](README.md).
