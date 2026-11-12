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

`vecten.toml` registers package names and extras. `requirements.txt` pins extension versions
and includes connector dependencies. Three dependency locks serve different environments:

| File | Environment |
|---|---|
| `requirements.lock` | The CLI, including its own dependencies; supports Python 3.14 and newer. |
| `requirements-airflow.lock` | The platform image, resolved against packages already installed in its base image. |
| `requirements-mcp.lock` | The MCP image, resolved against its own base image. |

Only enabled components get an image lock. The command does not pull images itself.
Resolution targets Python 3.14 from the repository's
[version table](../packages/vecten-shared/src/vecten_shared/versions.toml); image locks target Linux. The same
extension version is used everywhere, while transitive dependencies can differ to preserve
each runtime's existing packages. Builds refuse incompatible or missing image locks and
refuse to upgrade or downgrade packages supplied by the base image.

Commit `vecten.toml`, `requirements.txt` and all generated lock files. Repeat `extension add`
with a new version to upgrade; registration stays unchanged and the dependency records
change. Re-run it after enabling another component or changing the base images. The command
owns these requirements and lock files; do not keep unrelated dependencies in them.
Names, extras and version constraints are accepted. Direct URLs, paths, pip options and
conditional top-level requirements are refused.

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
bundled. A test in this repository's CI (`scripts/check_extension_wheels.py`) exercises a fixture declaring all nine groups and scans
shipped Python for prohibited licence-gating and telemetry symbols/imports. This automated
scan is a regression guard; code review remains necessary for behavior it cannot recognize.

Back to the [docs index](README.md).
