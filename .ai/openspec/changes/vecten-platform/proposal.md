## Status

Draft for discussion. This is a direction, not a change ready to build: it carries no delta
specs and no task list. Once the direction is agreed, each stage under
[Migration](design.md#migration) becomes its own issue and OpenSpec change with delta specs,
as CONTRIBUTING.md describes.

## Why

An extension today is one Python package that contributes through entry points, and every
part of it is imported into a Vecten process and installed into that process's dependency
tree: the CLI, the Airflow image and the MCP image (`shared/extension-points`, "Extensions are
pip dependencies everywhere"). That one mechanism carries five different kinds of
contribution:

| Kind | Groups today | What it really is |
|---|---|---|
| Content | `vecten.dbt_packages`, `vecten.osi`, `vecten.skills`, `vecten.consumer_skills`, `vecten.manifest` | Data |
| Framework plugins | `vecten.dags` | Code that Airflow requires inside its own processes |
| Scaffolding | `vecten.connectors` (`write(target)`) | A template whose output the project owns |
| Workloads | `vecten.services`, `vecten.apps`, `vecten.mcp.tools`, connector sync code | Programs with their own dependencies |
| Security hooks | `vecten.mcp.auth`, `vecten.mcp.sign_in`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks` | Third-party code in the identity, provisioning and audit path |

Only framework plugins need what the mechanism provides. The others pay its costs without
needing it, and three of those costs are structural rather than incidental:

1. **Every extension shares one dependency tree with Airflow, the MCP server and the CLI.**
   Services and apps run from the project image of a component (`docs/services.md`,
   `docs/analytics-apps.md`), so a chat bot's dependencies must resolve against Airflow's
   constraint set, together with every other extension's. The lock machinery exists to manage
   that coupling: five record files, locking that needs Docker and the release's base images,
   input-hash staleness checks, a `divergence` report, one extension version forced across
   three runtimes, and a shared-environment warning in `docs/extensions.md`. Its cost grows
   with every extension, and an upgrade of Airflow or of Vecten can break any extension.

2. **Third-party code runs inside the MCP server's trust boundary.** The MCP server holds the
   warehouse provisioning credential and the token signing key, signs people in and writes
   the audit. Identity backends, sign-in hooks, provisioning hooks and audit sinks are loaded
   into that process (`vecten_mcp/runtime_setup.py`) with its full permissions, and so is every
   `vecten.mcp.tools` handler. The Enterprise SAML, provisioning and compliance extensions
   install the same way (`shared/edition-boundary`). Validation of declarations does not help:
   any code in the process has the process's power, and so does any compromised dependency of
   that code. The audit's insert-only role (`mcp/audit`, "Compromised process") limits what a
   compromised process can do to stored rows, not what it can read or forge before writing.

3. **MCP tools are a private plugin system next to a protocol built for the job.** A tool
   must be a Python callable in Vecten's process, in Vecten's environment, released with
   Vecten. MCP itself is the standard way to expose tools across a process boundary.

The parts of the current design that work should stay: the project's explicit allowlist in
`vecten.toml`, recorded declarations in `extensions.lock.json` so `ship` needs no extension
code, the project binding of services, the sign-in gate, service network isolation, and
secrets scoped by name.

## What Changes

Vecten moves from a host that loads plugins to a platform that publishes content and runs
workloads, and keeps in-process code only where a framework requires it.

- **Three extension kinds, each with the mechanism it needs.** An extension declares, in one
  static manifest, any mix of: content bundles (data only, nothing executes to discover
  them), framework plugins (in-process, Airflow DAG definitions and dbt packages only), and
  workloads (their own environment, reached through a protocol).
- **The MCP server becomes a gateway.** Extension tools are MCP servers run as workloads. The
  gateway keeps what it already owns: sign-in, groups, the per-session tool list
  (`mcp/mcp-tools`), warehouse identity and the audit. It forwards each permitted call with a
  short-lived signed caller assertion. Warehouse access stays in the gateway: a tool queries
  through a gateway query API as the calling person, so warehouse credentials never leave the
  MCP server and query evidence is captured centrally.
- **Security hooks become protocols.** Identity: OIDC only at the MCP server; SAML and
  directory sync become an identity broker workload that speaks OIDC to it and SCIM to the
  admin API. Provisioning: a command interface to a provisioner workload that holds the
  elevated credential, so the MCP server no longer does. Audit export: sinks read the
  append-only audit through a cursor with a read-only role, as workloads, instead of running
  on the audit writer thread.
- **Every workload gets its own environment.** Long-running workloads (services, apps, tool
  servers, brokers) get their own image built from their own lock. Code a host must call
  (connector sync code in Airflow tasks, `vecten connector run`) runs in a per-extension
  virtual environment inside the host image or on the laptop, invoked out of process. No
  extension resolves against another extension or against Airflow.
- **Declarations are static data built into the package**, typed and versioned in a small
  extension SDK, so `vecten lock`, CI and review see an extension's full capability set, and
  the capability change in an upgrade, without running its code.
- **The in-process model stays for one deprecation window** as `trust = "in-process"`, allowed
  per extension in `vecten.toml`, so existing extensions keep working while they move.

Nothing changes for a project without extensions.

## Capabilities

### New Capabilities

- `shared/extension-manifest`: the static manifest, its kinds, versioning and the SDK.
- `mcp/tool-gateway`: forwarding to tool servers, the caller assertion, the gateway query API.
- `shared/workload-runtime`: per-extension images and environments, isolation, resources.

### Modified Capabilities

- `shared/extension-points`: replaced by the three kinds; "pip dependencies everywhere"
  removed after the deprecation window.
- `shared/edition-boundary`: the Enterprise extensions install as workloads and content.
- `mcp/mcp-auth`, `shared/identity-contract`: OIDC as the only in-process identity path;
  brokers outside the process.
- `mcp/warehouse-identity`: the provisioner as a separate workload holding its credential.
- `mcp/audit`: export by cursor and read-only role instead of in-process sinks.
- `mcp/mcp-tools`: "Tools are discovered as plugins" becomes "tools are served by tool
  servers behind the gateway".
- `platform/platform-stack`, `shared/deploy`, `cli/cli-lifecycle`: images per workload, the
  lock records, `up`, `ship` and `extension add`.

## Impact

- **Security:** no third-party code in the MCP process by default. Each workload holds only
  its declared secrets and network access, as services already do.
- **Dependencies:** one lock per workload image or environment instead of co-resolution
  across all extensions. Extension versions become independent of each other and of the
  Airflow and MCP base images.
- **Operations:** more containers per host. The `minimal` profile, one VM for both
  components, needs a measured memory budget before stage 3 (see design, Risks).
- **Extension authors:** a manifest and an SDK in place of bare entry points. Tools become MCP
  servers, which also makes them usable outside Vecten.
- **Code:** `vecten_shared.extensions`, `vecten.extensions`, `vecten_mcp.extensions` and
  `runtime_setup`, `vecten_platform.extensions`, `stack/images.py`, `stack/compose.py`, the
  bundle and the VM agent (Python 3.9, standard library only).
- **Docs:** `docs/extensions.md`, `docs/services.md`, `docs/analytics-apps.md`,
  `docs/mcp-identity.md`, `docs/mcp-audit.md`, `docs/GLOSSARY.md`.
