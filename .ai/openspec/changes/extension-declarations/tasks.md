The three groups below are three pull requests, in this order (design, "Migration Plan").

## 1. Locking per component (#50, pull request 1)

- [ ] 1.1 Test then implement: `vecten_shared.config` gains `[dependencies.platform|mcp|cli]` (`python`, and `system` except under `cli`), refused for a component the project lacks; Python entries pass `vecten_shared.extensions.requirement`
- [ ] 1.2 Test: a fixture extension wheel with `platform`, `mcp` and `cli` extras, each naming a different package, and one with no extras
- [ ] 1.3 One lock routine in `vecten/extensions.py` that builds each component's roots (extension pin with the component's extra when `Provides-Extra` lists it, connector dependencies, the component's `python` list, and `vecten==<release>` for the CLI)
- [ ] 1.4 Each compile starts from the committed lock as its output file; `--upgrade <package>` passes `--upgrade-package`; test that adding one root leaves other pins unchanged
- [ ] 1.5 The `# vecten-lock: component=<c> inputs=<sha256>` first line, and a function that recomputes it from `vecten.toml`, `requirements.txt` and `extensions.lock.json`
- [ ] 1.6 `extensions.lock.json` with each extension's version and provided extras (secrets and settings follow in 3.x); written atomically with the locks and `requirements.txt`, restored together on failure
- [ ] 1.7 `requirements.txt` holds only extension pins
- [ ] 1.8 `vecten lock` command (`--upgrade`, `--json` with the cross-lock divergence report); `extension add` ends by calling the same routine
- [ ] 1.9 `connector add` runs the lock routine when the project already has locks
- [ ] 1.10 `stack/images.py`: the project layer installs each component's own lock; a stamped lock is checked by its hash, an unstamped one by today's name rule; a `[dependencies]` Python requirement without locks is refused; every refusal names `vecten lock`
- [ ] 1.11 `deploy/bundle.py`: `extensions.lock.json` joins `CONTENT`
- [ ] 1.12 Tests: legacy project (unstamped locks, connector dependencies in `requirements.txt`) still builds; first `vecten lock` migrates it; stale lock refused by `up` and by bundle build; failure leaves every record unchanged
- [ ] 1.13 Docker-tier test: a project with different `python` lists per component builds both layers, and each image holds only its own packages
- [ ] 1.14 `next` reports `vecten lock` when a record is stale or missing
- [ ] 1.15 Docs: `docs/extensions.md` (extras, `[dependencies]`, `vecten lock`, the lock table), `docs/cli.md`; the `guide` skill where it mentions `extension add` for dependencies; render the plugin and `docs/agent-guide.md`
- [ ] 1.16 Changelog

## 2. Operating-system packages (#50, pull request 2)

- [ ] 2.1 Test then implement: `system` entries validated as Debian package names with an optional `=version` before any Dockerfile is written; a shell metacharacter is refused
- [ ] 2.2 The generated `airflow.Dockerfile` and `mcp.Dockerfile` run `USER root`, `apt-get update && apt-get install -y --no-install-recommends … && rm -rf /var/lib/apt/lists/*`, then return to `airflow` or `app`, before the Python install
- [ ] 2.3 Unit test of both generated Dockerfiles with and without `system`; the file without it is byte-identical to today's
- [ ] 2.4 Docker-tier test: a layer with one small package holds it, has no apt lists, and runs as the image's user
- [ ] 2.5 Docs: `docs/extensions.md` (or the dependencies page) on OS packages, the two Debian releases, pinning, and why an extension cannot add them
- [ ] 2.6 Changelog

## 3. Manifest, secrets and settings (#49, pull request 3)

- [ ] 3.1 `vecten.manifest` in `vecten_shared.extensions.GROUPS` (not a layer group for the empty-declaration rule); one per distribution
- [ ] 3.2 Test then implement manifest loading in the lock routine: `secrets` (names, scope rule, reserved scopes, stack-managed names, `AIRFLOW_*`), `settings` (the accepted JSON Schema subset, no `env` property), unknown top-level keys reported
- [ ] 3.3 `extensions.lock.json` records each manifest's secrets and settings schema
- [ ] 3.4 `vecten_shared.config`: top-level `secrets`, `[settings.<namespace>]` with `env.<env>` overrides; namespace is a listed extension or `project`; `local` or a valid environment name; TOML dates and times refused; an extension named `project` refused
- [ ] 3.5 One validator in `vecten-shared` for the effective settings of one environment against the record; called by `vecten lock` (local and every named environment), `up` (local), bundle build and `ship --check` (the shipped environment)
- [ ] 3.6 `stack/compose.py`: declared platform names to the Airflow services and `MCP_*` names to `mcp-server` as `${NAME:-}`, collision checks for both services; mount `extensions.lock.json` beside `vecten.toml`; set `VECTEN_COMPONENT`, and `VECTEN_PROJECT_ROOT=/app` for `mcp-server`
- [ ] 3.7 `deploy/bundle.py`: `host.json`'s `connector_secrets` carries every declared platform name; comment the key's wider meaning; test that `apply.py` and its Scaleway copy are unchanged
- [ ] 3.8 `vecten_shared.context`: `extension(name)` and `project()` with `settings`, `env` and `secret()`; declared-only and component-scoped `secret()`; tests on the local provider inside and outside Compose and on mocked AWS and Scaleway runtime providers, including a `minimal` host refusing an `MCP_` read from platform code
- [ ] 3.9 The MCP server validates every extension's settings at startup and does not report healthy on failure
- [ ] 3.10 `vecten_platform.sync.secret` keeps its behaviour; it may delegate to the shared provider construction
- [ ] 3.11 `extension add` output and `next` name declared secrets missing locally and per environment (`next/deploy.py`)
- [ ] 3.12 The stub extension declares a manifest with a platform secret, an `MCP_` secret and a setting with a default; the layer test asserts the record, the laptop Compose environment and a read through the API in a DAG and in a tool
- [ ] 3.13 `scripts/check_extension_wheels.py` and the docs' group table count ten groups
- [ ] 3.14 Docs: `docs/extensions.md` (manifest, secret delivery and the two-names pattern, settings and overrides, the read API, validation), `docs/GLOSSARY.md` (manifest, setting), `docs/mcp-tools.md` for tool authors; the `connector` skill where it says only connectors declare secrets; render the plugin and `docs/agent-guide.md`
- [ ] 3.15 Changelog

## 4. Comments in `vecten.toml` survive, in pull request 3

- [ ] 4.1 `save_config` updates the parsed `tomlkit` document in place, so the comments in `vecten.toml` survive `bootstrap`, `user add` and the other commands that record facts (design, Risks)
