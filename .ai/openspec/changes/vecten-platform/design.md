## Context

[proposal.md](proposal.md) states the problem: one in-process, shared-environment mechanism
carries five kinds of contribution. This design describes the target and how to get there.

What the code does today, which every decision starts from:

- **Discovery.** `vecten_shared.extensions.selected_entry_points` returns an entry point only
  if its distribution is listed in `vecten.toml`, its version satisfies the listed range, and
  its name is unique within the group. `load_group` imports it. The CLI, Airflow
  (`vecten_platform.extensions.load_dags`) and the MCP server (`McpExtensions.from_config`)
  each do this on their own, which is why one extension version must be installed in all
  three.
- **Records.** `vecten lock` writes `requirements.txt`, `requirements.lock`,
  `requirements-airflow.lock`, `requirements-mcp.lock` and `extensions.lock.json` together.
  The image locks resolve against the base images' installed packages, so locking needs
  Docker. `extensions.lock.json` already records manifests, services and apps, so `ship`
  renders from committed files without importing extensions.
- **Services and apps** already run as separate containers on a network only NGINX joins,
  with no cloud identity and only their declared secrets (`docs/services.md`). They run from
  the project image of a component, so their dependencies are Airflow's or the MCP server's.
- **MCP hooks.** `runtime_setup.py` calls the selected identity backend, passes sign-in hooks
  to the access service, and hands `extensions.emit_audit` to `AuditService` as its sink, which
  runs synchronously on the audit writer thread. Tools are callables in the tool registry,
  filtered per session by group (`mcp/mcp-tools`).
- **Warehouse identity.** Every query runs as the person's own warehouse user, via a
  session-scoped impersonator membership on Postgres or a server-issued token on Snowflake
  (`mcp/warehouse-identity`). The MCP server holds the provisioning credential permanently.
- **Hosts.** The `minimal` profile puts both components on one VM. The VM agent runs on the
  host's Python 3.9 with the standard library only.

## Goals / Non-Goals

**Goals:**

- No third-party code in the MCP server's process unless the project explicitly opts in.
- No extension resolves its dependencies against another extension, against Airflow or
  against the MCP server.
- Everything an extension can do is known from committed data, without running its code, and
  an upgrade shows what it adds or removes.
- An extension's warehouse access is the calling person's, through the gateway, and every
  query it makes is in the audit.
- The open product stays complete and the Enterprise extensions still install through the
  public extension kinds (`shared/edition-boundary`).
- The laptop loop stays fast: an edit to an in-repo extension is live without a rebuild for
  Python code.

**Non-Goals:**

- A sandbox for in-process Python. Framework plugins (DAG definitions) and the opt-in
  in-process mode stay fully trusted, and the docs say so.
- Kubernetes or another orchestrator. Docker Compose on VMs stays the runtime.
- A marketplace or registry service. Sources stay package indexes, git and `extensions/`.
- Changing what the open and Enterprise editions contain.

## Decisions

### 1. Three extension kinds, declared in one static manifest

An extension declares any mix of three kinds in one manifest, generated at build time from
typed declarations and shipped in the wheel as data:

- **Content:** dbt packages, Ossie documents, skills, consumer skills, settings schema,
  secret names, Airflow pools, variables and API accounts. Pure data, read without importing
  anything.
- **Framework plugins:** Airflow DAG definition files and dbt packages, loaded by the
  framework in its own process because the framework requires it.
- **Workloads:** tool servers, services, apps, identity brokers, provisioners, audit
  exporters, and the sync code connectors call. Each runs in its own environment and is
  reached through a protocol.

Connector scaffolding becomes a one-time template: `connector add` writes the project's code
and schedules, and nothing reads the installed extension again to re-render them. That
removes the second source of truth that "rendering a declared connector again reads the
schedules from the installed extension" creates today.

*Alternative considered:* keep entry points and add typed validation. Rejected as the end
state because it still executes code to discover what an extension does, and does nothing for
problems 1 to 3 in the proposal. Entry points remain as the transport for the deprecation
window (Decision 8).

### 2. The MCP server is a gateway; tools are MCP servers

An extension tool is an MCP server run as a workload on an internal network that only the
gateway joins. The gateway:

- builds each session's tool list from the tool servers' declared tools and the project's
  group binding, as it does for in-process tools today;
- forwards a permitted call with a **caller assertion**: a JWT signed with a dedicated
  gateway key (never the OAuth signing key), audience-bound to that tool server, with the
  principal, groups, invocation id, and a lifetime of seconds. Tool servers verify it against
  a key set published on the internal network;
- exposes a **query API** on the internal network. A tool runs warehouse SQL by calling it
  with the caller assertion. The gateway runs the query as the person through the existing
  warehouse identity path and attaches the SQL, hash and row count to the invocation by its
  id. This replaces the `contextvars` association in `docs/extensions.md`, which breaks with
  custom thread pools, with evidence that cannot be lost;
- writes one audit row per call, as `mcp/audit` requires, including calls a tool server
  refuses or fails.

Tool servers never receive warehouse credentials, the signing key or other extensions'
secrets.

*Alternatives considered:* give tool servers warehouse credentials by token exchange
(rejected: credentials leave the server and queries escape the audit); put a commercial MCP
gateway such as Runlayer in front (rejected as a dependency of an open, self-hosted product
because it would duplicate identity and split the audit; supported as an upstream that a
client may put in front of Vecten).

### 3. Identity: OIDC only in-process; everything else is a broker

The MCP server keeps its `password` and `oidc` backends and drops the in-process
`vecten.mcp.auth` and `vecten.mcp.sign_in` groups. SAML and directory sync become an identity
broker workload that speaks SAML to the client's identity provider and OIDC to the MCP
server. Group membership from directory sync arrives through SCIM on the admin API, called by
the broker with its own service principal. The claim-to-group mapping that sign-in hooks
compute today becomes declarative configuration in `vecten.toml`, evaluated by the MCP server.

*Alternative considered:* keep identity backends in-process because sign-in is
latency-sensitive. Rejected: one redirect hop at sign-in is negligible, and identity is the
last place third-party code should share a process with the signing key.

### 4. Provisioning: a provisioner workload holds the elevated credential

The provisioning credential moves out of the MCP server into a provisioner workload with a
small, fixed command interface: create or adopt a person's warehouse user, grant and revoke
session membership, disable, delete, rotate. The MCP server calls it on an internal network
with its own service identity. A just-in-time or cloud IAM provisioner (Enterprise) replaces
that workload instead of replacing a Python hook. The startup grant check
(`mcp/warehouse-identity`) moves with the credential.

This also narrows the built-in path: a compromised MCP process can ask the provisioner only
for the fixed commands, and every command is logged by the provisioner.

### 5. Audit export: a cursor and a read-only role, not a sink

`vecten.mcp.audit_sinks` is removed. An audit exporter is a workload with a read-only role on
the audit table that reads rows after a persisted cursor and delivers them wherever it
needs to (signed export, SIEM). The append-only table stays the source of truth. A slow or
failing exporter no longer delays the writer thread, and it can be restarted and caught up.

### 6. Environments: images for long-running workloads, per-extension venvs for called code

- **Long-running workloads** (tool servers, services, apps, brokers, provisioners, exporters)
  get their own image, built from their own lock over a slim Vecten workload base image.
  Services and apps keep today's isolation and binding.
- **Code a host calls** (connector sync code in an Airflow task, `vecten connector run`) runs
  in a per-extension virtual environment, built with `uv` into the Airflow image and on the
  laptop, and invoked out of process. The DAG file names the extension's sync entry; the task
  runs it in that environment.
- **Framework plugins** stay in the host environment and may import only Airflow and the
  Vecten SDK, which the manifest check enforces.

*Alternatives considered:* `DockerOperator` for task isolation (rejected: it needs the Docker
socket inside Airflow, which is root on the host); one container per task (rejected for the
single-VM profile on memory); Dagger as the build engine (deferred: plain BuildKit with one
lock per image gives the isolation; reconsider if building and testing many workload images
becomes the bottleneck).

### 7. A typed, versioned extension SDK

A small `vecten-extension-api` distribution holds frozen pydantic models for every
declaration, the manifest builder, the caller-assertion verifier, the query API client and an
in-memory test kit. The manifest carries an `api` major version, and a Vecten release states
the range it supports: the current major and the one before it. An extension outside the range
is refused at `extension add` and `vecten lock`, naming both versions, instead of having
unknown keys reported and ignored.

### 8. Records and the deprecation window

- `vecten.toml` stays the desired state: which extensions, their trust mode, settings,
  bindings and group grants.
- `vecten lock` writes one lock per workload image or environment, the host image locks
  (now holding only Vecten, framework plugins' declared needs and project requirements), and
  `extensions.lock.json` with each extension's manifest. It prints the capability change
  against the committed record: new tools and their groups, routes and their access, secrets,
  network needs, resource limits.
- For one deprecation window, an extension may be listed with `trust = "in-process"`, which
  loads its entry points as today. The MCP server logs at startup every extension running in
  its process, and `ship --check` reports them. After the window, in-process loading in the
  MCP server is removed. The fixture in `scripts/check_extension_wheels.py` covers both modes
  during the window.

## Risks / Trade-offs

- **Memory on the `minimal` profile.** Each workload is a container with its own Python. A
  project with a few tools and a service could add hundreds of MB. Mitigation: tool servers
  from one extension share a container; `memory_mb` defaults stay small; a measured budget on
  the smallest supported VM type gates stage 3.
- **Latency.** One internal hop per tool call and per query API call. Small next to
  warehouse queries, but measured before stage 2 ships.
- **The gateway and the provisioner become more critical.** Both are Vecten's own code with
  existing health checks; neither runs third-party code, which is the point.
- **More for extension authors to learn.** A manifest, an SDK and MCP servers instead of a
  callable. Mitigated by `vecten extension new` templates for each workload kind and the test
  kit.
- **Enterprise extensions must be rebuilt** as a broker, a provisioner and an exporter. This is
  the largest migration cost and sits on the critical path of stage 1.
- **Laptop loop.** Workload images must not slow editing. Mitigation: bind mounts and
  `docker compose watch` for in-repo workloads under `vecten up`, editable per-extension venvs
  for called code.

## Migration

Each stage is its own issue and OpenSpec change, shippable alone, with no stage breaking
existing projects.

0. **SDK and static manifest.** Generated alongside today's entry points; `vecten lock`
   records it and prints capability changes. No behaviour change.
1. **Security hooks out of the process.** Audit exporter by cursor, provisioner workload, OIDC
   with an identity broker for SAML. The Enterprise extensions move here. In-process hooks
   remain only under `trust = "in-process"`.
2. **Tool gateway.** Caller assertion, query API, tool servers. In-process tools remain under
   `trust = "in-process"`.
3. **Workload images for services and apps.** Their own locks; the host image locks shrink.
4. **Per-extension environments for connector sync code** in Airflow and the CLI.
5. **End of the window.** In-process loading removed from the MCP server; "Extensions are
   pip dependencies everywhere" removed from `shared/extension-points`.

## Open Questions

- Which smallest VM type must the `minimal` profile support, and what is its memory budget for
  workloads?
- Does the built-in provisioner also move into its own workload in stage 1, or only
  replacement provisioners? Moving it gives the security gain to every project.
- How do per-extension venvs fit in the Airflow image: built at image build time per
  extension, or one venv per extension shared across its tasks? This needs a spike against
  Airflow 3's task execution.
- How long is the deprecation window: one minor release or two?
- Should consumer skills and skills move to a content-only package format (no wheel), or stay
  as wheel data?
