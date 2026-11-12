## Why

An extension can tell Vecten very little about what it needs, and a project can tell it even less.

- **Secrets (#49).** Only a connector declaration lists secrets, and only those are passed into
  the Airflow containers, which is where the laptop's provider reads them. A DAG or an MCP tool
  from an extension has no way to name its credentials. On the cloud the code can still read
  any secret in its VM's scope through the provider. On the laptop the same call fails, so
  projects add the names to their Compose override by hand.
- **Settings (#49).** There is no place for a non-secret value such as a cap, an allow-list or a
  dry-run flag. `vecten.toml` refuses unknown keys, `[environments.<env>]` holds facts the CLI
  records, and the container environment is a fixed set. Projects keep a Python module keyed on
  `VECTEN_ENV` instead.
- **Dependencies (#50).** One requirement list is locked three times: for the CLI environment,
  the Airflow image and the MCP image. An extension that ships only a DAG with heavy
  dependencies must still resolve against the MCP base image and install on every laptop.
- **Project dependencies (#50).** Project code can add a Python package only by attaching it to
  a connector's `dependencies`, or by publishing a private extension to carry it. Nothing can
  add an operating-system package to an image.

## What Changes

- An extension MAY declare one `vecten.manifest` entry point: a dictionary with `secrets` (names)
  and `settings` (a JSON Schema for its settings). Unknown keys are reported, not refused.
- A secret's name decides where it is delivered, as it already decides its scope: `MCP_*` names
  reach the MCP server and every other name reaches Airflow. Declared secrets reach the laptop
  stack's containers the same way connector secrets do today.
- `vecten.toml` gains `[settings.<extension>]`, with per-environment overrides under
  `[settings.<extension>.env.<env>]` (`local` is the laptop), and `[settings.project]` for the
  project's own code. Values are checked against the manifest's schema by `vecten lock`, `up`
  and `ship`.
- A top-level `secrets` list in `vecten.toml` declares the project's own secrets, delivered the
  same way.
- `vecten_shared.context` gives DAG code, connectors and MCP tools one read API:
  `extension(name).settings`, `extension(name).secret(NAME)`, `project().settings` and
  `project().secret(NAME)`. It reads only declared secrets, and only those of the reading
  component.
- An extension scopes its dependencies with the standard extras `platform`, `mcp` and `cli`.
  The extension itself is still installed everywhere at the same version.
- `vecten.toml` gains `[dependencies.platform]`, `[dependencies.mcp]` and `[dependencies.cli]`,
  each with `python` requirements, and `system` packages for the two images.
- A new command, `vecten lock`, resolves the three locks from their own inputs, keeps the
  committed pins as preferences, stamps each lock with a hash of its inputs, and writes
  `extensions.lock.json`. This is the committed record of each extension's version, extras,
  secrets and settings schema. `extension add` ends by running it.
- The generated project layer installs the declared `system` packages with `apt-get` before the
  Python packages.

Existing projects and extensions keep working. An extension without a manifest or extras is
locked and installed as today. A project whose locks predate this change is accepted until its
next `vecten lock`.

## Capabilities

### Modified Capabilities

- `shared/extension-points`: the manifest group, secrets and settings, per-component extras, and
  the read API.
- `platform/platform-stack`: how declared secrets and settings reach the containers.
- `shared/deploy`: what the project layer installs.
- `cli/cli-lifecycle`: `extension add` and the new `lock` command.
- `cli/scaffold`: the `settings` and `dependencies` sections of `vecten.toml`.
- `mcp/mcp-tools`: tool handlers read settings and secrets through the shared API.

## Impact

- `packages/vecten-shared/src/vecten_shared/config.py` (two sections), `extensions.py` (the
  manifest group), a new `context.py` (the read API) and a reader for `extensions.lock.json`.
- `packages/vecten/src/vecten/extensions.py` (locking), `stack/images.py` (project layer),
  `stack/compose.py` (secret delivery and mounts), `deploy/bundle.py` (content and the
  host's secret list), `next/` (secrets to set), a new `cli/commands/lock.py`.
- `packages/vecten-platform/src/vecten_platform/sync/context.py`: `secret()` keeps working; it may
  delegate to the shared context.
- `packages/vecten-mcp`: nothing structural; tool handlers import the shared API.
- `packages/vecten/src/vecten/deploy/vm/apply.py` and its Scaleway copy are unchanged.
- Docs: `docs/extensions.md`, `docs/GLOSSARY.md` (manifest, setting), the `connector` and
  `guide` skills where they mention dependencies or secrets.
- New committed project file: `extensions.lock.json`. It is added to the shipped bundle.
