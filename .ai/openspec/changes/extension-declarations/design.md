## Context

This change answers two client issues together: #49 (secrets and settings for extensions that are
not connectors) and #50 (per-component dependencies, project dependencies and OS packages). They
share one piece of machinery: a committed record of what each locked extension declares, written
by the command that writes the locks. They are therefore one change, implemented in three pull
requests (see "Migration Plan").

What the code does today, which every decision below starts from:

- **Connector secrets.** A connector declaration in `vecten.toml` carries `secrets`
  (`ConnectorDecl.secrets`, `vecten_shared/config.py`). `stack/compose.py` adds each one to the
  Airflow services' environment as `${NAME:-}`, so on the laptop Compose fills it from `.env`.
  Inside Compose the local provider reads secrets from the process environment
  (`LocalProvider.get_secret`, `inside_compose=True`), so on the laptop only names passed this way
  can be read. On a VM, `bundle.py` writes the same names into `host.json` as
  `connector_secrets`. The VM agent (`deploy/vm/apply.py`, Python 3.9 standard library only, with
  a byte-identical copy in the Scaleway host module) reads them from the platform scope and passes
  them to Compose. The cloud runtime providers (`AwsRuntimeProvider`, `ScalewayRuntimeProvider`)
  can also read any name in their VM's scope directly. As a result, code that reads an undeclared
  secret works on a VM and fails on the laptop.
- **Secret scopes.** A name's prefix decides its scope (`provider/__init__.py`, `scope_for`):
  `MCP_*` is `mcp`, `DEPLOY_*` is `deploy`, `ADMIN_*` is `admin`, `SETUP_*` is `setup`, and
  anything else is `platform`. The platform VM reads `platform` only and the MCP VM reads `mcp`
  only. Under `minimal`, the one VM reads both (`shared/deploy`, "Secrets and bucket prefixes are
  scoped per VM").
- **`vecten.toml`.** Every model is `extra="forbid"`. `[environments.<env>]` (`EnvironmentFacts`)
  holds facts the CLI records, and `save_config` rewrites the whole file with `tomli_w`, dropping
  comments. `extension add` alone edits it with `tomlkit`. The file is shipped in every bundle and
  mounted read-only into Airflow (`/opt/airflow/vecten.toml`) and the MCP server
  (`/app/vecten.toml`). `VECTEN_ENV` is `local` on the laptop and the environment's name on a VM.
- **Locks.** `extension add` (`vecten/extensions.py`) builds one root list: the extensions, pinned
  to exact versions, plus every connector's `dependencies`. It writes that list to
  `requirements.txt` and resolves it three times with `uv pip compile`. `requirements.lock` is a
  universal lock for the CLI and adds `vecten==<release>`. `requirements-airflow.lock` and
  `requirements-mcp.lock` are Linux locks, each constrained by the distributions installed in its
  base image, which `docker run` reads. Each compile writes into a temporary directory, so every
  run re-resolves from scratch. `stack/images.py` refuses records whose names differ from
  `vecten.toml`, with "rerun vecten extension add". A project with no records at all installs its
  connector dependencies unlocked. `connector add` does not relock. The `shared/deploy`
  requirement "Base images are published, the project layer is built on the VM" still says "a
  project carries no lockfile at v0.1", which the code no longer matches. This change's delta
  rewrites that sentence.
- **Images.** The base images are `apache/airflow` plus the platform wheels (Debian, running as
  `airflow`) and `python:3.14-slim` plus the MCP wheels (Debian, running as `app`). `ship` and `up`
  build a thin project layer: `FROM <base>` and one `pip install` against the base image's own
  distributions as constraints. There is no hook for an OS package.
- **CI ships without the project's extensions.** The rendered `ship.yml` and
  `deploy-platform.yml` run `uv tool install "vecten==<release>"` and nothing else, so anything
  `ship` renders must come from committed files. It cannot come from an imported extension. The
  same holds for the rendered `ci.yml`: its `vecten mcp validate` runs without the project's
  extensions, so their Ossie documents are not validated in CI. That gap is outside this change.
- **What each component imports.** The CLI imports `vecten.connectors`, `vecten.skills`,
  `vecten.osi` and `vecten.dbt_packages`, and `connector run` runs connector code in the CLI's
  environment. Airflow imports `vecten.dags` (and `osi`, `dbt_packages` through dbt). The MCP
  server imports `vecten.mcp.*` and `vecten.osi`.

Related work: #59 added `groups` to an extension connector's declaration; #58 put
per-environment settings in `deploy/envs/<env>.tfvars`; #35 (install an extension from a git
tag) changes the same lock code; #40 (extensions bring cloud and warehouse resources) and #42
(declare Airflow pools and variables) will want to declare more things.

## Goals / Non-Goals

**Goals:**

- Any extension can name its secrets. The laptop and every VM deliver them the same way, and code
  reads them through one call.
- Namespaced, typed settings per extension, with per-environment values, readable from DAG code,
  connectors and MCP tools, and checked before a stack starts.
- An extension's heavy dependencies install only in the component that uses them.
- A project can add Python packages per component and OS packages per image, without publishing
  an extension.
- The three locks stay reviewable: one command writes them, and re-locking does not churn
  unrelated pins.
- Every existing extension and project keeps working without an edit.

**Non-Goals:**

- A secret shared by both VMs. Two names, one per scope, remain the way to do this (Decision 3).
- Settings that change at run time without a ship. Airflow variables and pools are #42.
- Extensions installing OS packages, or bringing cloud, IAM or warehouse resources (#40). This
  change only leaves room for them in the manifest (Decision 12).
- Installing an extension from git (#35). This change defines the lock shape #35 plugs into.
- dbt variables or packages (#44).
- Changing the VM agent's contract (`HOST_KEYS`) or its Python 3.9 code.

## Decisions

All decisions were accepted by the maintainer on 2026-11-12, each as recommended. They are
ordered with the most consequential and least reversible first: the first three are the contract every extension author codes against.

### Decision 1: Extension-wide declarations live in a `vecten.manifest` entry point — *Accepted*

**Context.** An extension needs a place to say "I read these secrets and these settings". Today
the only per-extension object with room for that is a connector's dictionary. A DAG is a
directory and a skill is a directory, so they have no object to carry it. This is a public
contract: extensions will be published against it, so changing it later breaks them.

**Options.**

1. *A new entry point group, `vecten.manifest`.* At most one per distribution. It holds a
   dictionary, or a zero-argument factory returning one, in the style of `vecten.connectors`, with
   `secrets` and `settings`. The CLI imports it at `vecten lock` time, so its module must import
   only the standard library and Vecten's shared package. One place covers every kind of
   extension, and #40 and #42 have an obvious home later. The cost is a tenth group: the stub
   extension, `scripts/check_extension_wheels.py` and the docs' group table change.
2. *Keys on each layer's object.* `secrets` on a `vecten.mcp.tools` dictionary, and so on. This
   cannot work for `vecten.dags`, `vecten.osi`, `vecten.dbt_packages` or `vecten.skills`, which
   are directories. The declarations would be spread out, and settings belong to the extension,
   not to one tool.
3. *A static data file in the wheel* (for example `vecten-manifest.toml`), found through package
   metadata. It can be read without importing code, even from the staged `--no-deps` install in
   `inspect_package`. But an entry point is still needed to find the file, and resource entry
   points already import their module (`extension_paths` calls `load()`). Nothing in the product
   reads non-entry-point data from a wheel today.

**Accepted: option 1.** It matches the `vecten.connectors` style that extension authors
already know, works for every layer, and gives #40 and #42 a place to land. Import cost is not a
concern, because the CLI imports manifests only in `vecten lock`, inside the environment the CLI
lock installed. A package whose only entry point is `vecten.manifest` contributes no layer, and
is refused like a package with none.

The manifest sits alongside a connector's own dictionary and does not replace it. `kind`,
`secrets`, `dependencies` and `groups` stay on the `vecten.connectors` entry,
because `connector add` copies them into the project's connector declaration, which the project
then owns. A secret that only a connector reads stays there. The set delivered to Airflow is the
union of connector secrets and manifest secrets, so a name declared in both places is harmless.

### Decision 2: Per-component dependencies are standard extras named `platform`, `mcp` and `cli` — *Accepted*

**Context.** An extension's dependencies have to differ per component, but its version may not:
the CLI, Airflow and the MCP server all discover its entry points, for example the Ossie
documents both the CLI and the MCP server read. The question is how an extension says "this
package only for Airflow".

**Options.**

1. *Extras.* The wheel declares `[project.optional-dependencies] platform = [...]`, `mcp = [...]`
   and `cli = [...]`. Its base `dependencies` install everywhere. Vecten locks
   `acme[platform]==1.2.0` for Airflow, `acme[mcp]==1.2.0` for the MCP server and
   `acme[cli]==1.2.0` for the CLI, and requests an extra only when the wheel's metadata provides
   it (`Provides-Extra`, readable without importing). This is standard packaging, so resolvers,
   `pip` and `uv` already handle it, and no extension code runs to read it. A wheel that declares
   no extras is locked exactly as today. The cost is that the names `platform`, `mcp` and `cli`
   become reserved in extension packages. An extension that already uses an extra called `mcp`
   for something else would have it installed in the MCP image.
2. *A `dependencies` mapping in the manifest* (`{"platform": [...], "mcp": [...]}`). This
   duplicates what packaging metadata already expresses. Locking would also have to import the
   manifest before resolving, and a staged `--no-deps` install cannot import a module whose
   dependencies are missing.
3. *Prefixed extras* (`vecten-platform`, `vecten-mcp`, `vecten-cli`). These avoid collisions, but
   `vecten-mcp` reads like the distribution of that name, and the issue asked for the plain
   names.
4. *Install the extension only in the components it targets.* This breaks entry point discovery
   wherever it is missing: the MCP server would not see an extension's Ossie documents, and the
   CLI would not see its skills. It also breaks "same version everywhere".

**Accepted: option 1, with the plain names.** `cli` covers what runs in the CLI's
environment: `connector run`, local dbt and anything a skill imports. A connector's runtime
dependencies therefore belong in both `platform` and `cli`, or in the base list. Verified with
uv 0.8.15: a requirement naming an extra that a wheel lacks resolves with a warning and exit 0.
Vecten still requests only the extras the wheel provides, so no warning appears.

### Decision 3: A secret's name prefix decides where it is delivered — *Accepted*

**Context.** A declared secret has to reach the code that reads it, in a scope that code's VM may
read. The scope rule (prefix to scope) is enforced by IAM on both clouds, by the Terraform modules
and by the provider contract tests. Whatever the manifest says has to fit that rule or change it.

**Options.**

1. *Keep the prefix rule.* `secrets` is a flat list. An `MCP_*` name is delivered to the MCP
   server, and any other name is delivered to Airflow and to the CLI process (`connector run`).
   An extension whose DAG and MCP tool both need one credential declares two names
   (`ACME_TOKEN`, `MCP_ACME_TOKEN`), and the operator sets both. Nothing changes in IAM,
   Terraform or the providers.
2. *The manifest says the component* (`{"platform": [...], "mcp": [...]}`), and the prefix must
   agree. This is more explicit, but it says the same thing twice, so the two can disagree.
3. *A new `shared` scope* that both VMs read, for a secret declared for both components. This
   changes the IAM policy on both clouds, the Scaleway per-VM key policy, the boundary
   `bootstrap` writes on AWS, the contract test and the `shared/deploy` requirement. It also
   weakens "each VM reads only its own scope".

**Accepted: option 1.** Declaring `DEPLOY_*`, `ADMIN_*` or `SETUP_*` is refused, because no
VM may read those scopes. So are names the stack manages itself: `PACKAGE_INDEX_URL`,
`MCP_PACKAGE_INDEX_URL`, `LOADER_PASSWORD`, `MCP_STATE_DB_PASSWORD`, `MCP_PROVISIONER_PASSWORD`,
`MCP_IMPERSONATOR_PASSWORD`, `MCP_SIGNING_KEY`, `MCP_ADMIN_SETUP_TOKEN`,
`MCP_OIDC_CLIENT_SECRET`, `ALERT_WEBHOOK_URL`, every name in `stack/env.py`'s `STACK_MANAGED`,
and any `AIRFLOW_*` name. An `MCP_*` name in a project without `mcp`, or another name in a project
without `platform`, is reported by `vecten lock` as not delivered. It is not refused, so an
extension stays usable in a single-component project.

Delivery under this option:

- **Laptop.** `up` adds platform names to the Airflow services' environment, as it does for
  connector secrets now, and `MCP_*` names to `mcp-server`'s, both as `${NAME:-}`.
- **Platform VM.** `bundle.py` adds the platform names to the list it already writes as
  `host.json`'s `connector_secrets`. The agent already reads that list from the platform scope and
  passes it to Compose, so `apply.py` and its Scaleway copy are unchanged. The key now carries
  more than connector secrets; a comment in `bundle.py` says so.
- **MCP VM.** Nothing is injected. The MCP server's cloud runtime provider reads the name from the
  `mcp` scope it already may read. Adding injection here would need a new `HOST_KEYS` entry,
  which means an agent change and a new byte-identical copy, for no behavioural gain.

### Decision 4: Settings live in `vecten.toml`, per-environment values beside them — *Accepted*

**Context.** A setting such as a daily cap has a default (in the manifest), a project value and
sometimes a per-environment value, including one for the laptop. It must reach the code running in
Airflow and in the MCP server on every environment. #58 put per-environment *deploy* choices,
such as `dags_paused_at_creation`, in `deploy/envs/<env>.tfvars`. Whichever file holds settings
moves with the commit, which matters once #37 ships one commit to staging before prod.

**Options.**

1. *`vecten.toml`.* Values go in `[settings.<extension>]` and overrides in
   `[settings.<extension>.env.<env>]`, with `env.local` for the laptop. The file is already
   shipped and mounted into both containers, and `VECTEN_ENV` already names the environment, so
   delivery needs no new plumbing. A project without a deploy layer can use it. Defaults and
   overrides sit next to each other, so a reviewer sees what differs. The costs: the setting name
   `env` is reserved, and `save_config` rewrites the file without comments (true of every section
   today; see Risks).
2. *`deploy/envs/<env>.tfvars`*, beside #58's settings. There is no tfvars for the laptop, so
   laptop values would need a second home. The file is not shipped, so `ship` would have to render
   the values into the bundle. Its parser takes one `key = JSON` per line (`deploy/tfvars.py`), so
   nested settings become one long JSON line. Terraform warns about every value no root declares,
   so the roots would need an `any`-typed variable that exists only to be ignored. #58's
   setting fits tfvars because `ship` consumes it to shape the stack. Extension settings are read
   by application code at run time.
3. *Per-environment files*, such as `deploy/envs/<env>.settings.toml`, with defaults in
   `vecten.toml`. This follows the "copy prod to staging" habit and keeps hand-written values out
   of the file `save_config` rewrites. But values are then split across two files, `deploy/`
   exists only with the deploy layer, and the bundle and both containers need a new file and
   mount.
4. *`[environments.<env>]`.* That table holds facts the CLI records (#58's design rejects it
   for the same reason), and it has no `local`.

**Accepted: option 1.** An override replaces a whole top-level value, with no deep merge,
so a reader can tell the effective value from two places. `env` must name `local` or a valid
environment name. TOML dates and times are refused, so every value has a JSON type. The namespace
is the extension's normalised distribution name, which must be listed in `extensions`, or
`project` (Decision 13). On a VM a change takes effect with the next ship. On the laptop it takes
effect at the next read, because the file is mounted live.

### Decision 5: What extensions declare is recorded at lock time in `extensions.lock.json` — *Accepted*

**Context.** `ship` renders the Compose file and `host.json` and needs the declared secret names.
`up`, `ship` and `vecten lock` validate settings against each manifest's schema. Locking needs
each extension's extras. But CI ships with only `vecten` installed, so `ship` there cannot import
an extension's manifest.

**Options.**

1. *A committed, generated record, `extensions.lock.json`*, written by `vecten lock` next to the
   requirement locks. For each extension it holds the version, the extras it provides, its
   secrets and its settings schema. `ship` and `up` read it without importing anything. It goes
   into the bundle and is mounted beside `vecten.toml`, so the runtime read API validates against
   the same record. The record is stale when its versions differ from `requirements.txt`, and
   then `up` and `ship` refuse with "run `vecten lock`".
2. *Import manifests when rendering*, and change the rendered CI workflows to install
   `requirements.lock`. This is one source of truth, but CI then needs the private index (or, after
   #35, git) credential on every run, it gets slower, and existing projects must run
   `vecten update` before shipping works.
3. *Record the declarations in `vecten.toml`*, as connector declarations are. JSON Schema in TOML
   is unreadable, and the file `save_config` rewrites would then hold generated content.

**Accepted: option 1.** A reviewer sees a new secret or setting arrive in the diff of an
upgrade. The record also gives #35 a place for a resolved commit.

### Decision 6: Three per-component locks, one command, inputs hashed, pins kept — *Accepted*

**Context.** Per-component roots mean the three locks no longer share one input list. The issue
asks that three diverging locks not become unmanageable. Two facts constrain the options. Each
image lock must not move a package its base image installed, so it is resolved against that
image's installed set. And the two base images pin different versions of shared libraries.

**Options.**

1. *Keep three locks, each resolved from its own roots.* The roots are each extension at its exact
   pin with the component's extra, the connector dependencies (Decision 11) and the component's
   `[dependencies.<component>].python`. One command, `vecten lock`, writes all three locks, the
   record and `requirements.txt` together or not at all. To keep the locks manageable:
   - Each compile starts from the committed lock as its output file. uv then keeps existing pins
     as preferences, so adding one package does not re-resolve the rest (verified with uv
     0.8.15). `vecten lock --upgrade <package>` passes `--upgrade-package`.
   - Each lock's first line is `# vecten-lock: component=<c> inputs=<sha256>`, a hash of its
     sorted roots, the base image reference, the release and the Python version. `up` and `ship`
     recompute it from `vecten.toml`, `requirements.txt` and the record, and refuse a mismatch,
     naming `vecten lock`. This replaces today's name-by-name check.
   - Extension versions are identical in every lock by construction. `vecten lock --json`
     reports any other package pinned differently across locks, as information.
2. *One universal `uv.lock`* from a generated `pyproject.toml` with a dependency group per
   component. Constraints in uv apply to the whole resolution, so it cannot honour two base
   images that pin different versions of the same library. It would either fail or need the
   groups declared as conflicting, which brings back separate resolutions anyway.
3. *Resolve the union once and filter per component.* This has the same base-image conflict, and
   the CLI's universal resolution would also have to agree with both Linux images.

**Accepted: option 1.** `requirements.txt` becomes the record of each extension's exact pin
and nothing else, which is also where #35 adds a git source. Connector and project requirements
live in `vecten.toml` and their pins in the locks. `connector add` runs `vecten lock` when the
project already has locks, which fixes today's "rerun vecten extension add" after adding a
connector with dependencies.

### Decision 7: Project requirements in `[dependencies.<component>]` — *Accepted*

**Context.** A project needs to add a Python package to one component without wrapping it in an
extension, and an OS package to one image.

**Options.**

1. *Per-component tables.*
   ```toml
   [dependencies.platform]
   python = ["polars>=1,<2"]
   system = ["libgomp1"]
   [dependencies.mcp]
   python = ["rapidfuzz>=3,<4"]
   [dependencies.cli]
   python = ["polars>=1,<2"]
   ```
   Everything that goes into one image is together. `system` is refused under `cli`, because
   Vecten does not manage the laptop's OS.
2. *Flat lists per kind*, such as `[dependencies] platform = [...]` and
   `[system_packages] platform = [...]`. This is shorter, but what an image gets is split across
   two tables.
3. *One list for every component*, as `requirements.txt` was. This is exactly what #50 asks to
   stop.

**Accepted: option 1.** Python entries follow the rules `extension add` applies today:
name, extras and version constraint only, with no URL, path, option or marker. #35 relaxes this
for git sources. The section is hand-edited and followed by `vecten lock`. No `deps add` command
is added. The `guide` skill names the two steps.

### Decision 8: OS packages are apt packages in the generated layer; extensions cannot add them — *Accepted*

**Context.** Both base images are Debian-based: both Dockerfiles already run `apt-get`. Their
Debian releases can differ, and this draft has not checked which release each pinned image uses.
The project layer is generated, so the hook has to be something the generator renders safely
into a `RUN` line.

**Options.**

1. *Declared names.* `[dependencies.<platform|mcp>].system` lists Debian package names, optionally
   with `=<version>`, validated against Debian's naming rules before they reach a shell line. The
   generated Dockerfile runs, before `pip install`:
   `USER root`, `apt-get update && apt-get install -y --no-install-recommends <names> && rm -rf
   /var/lib/apt/lists/*`, then back to the image's user (`airflow` or `app`). It is the same on
   the laptop and on the VM.
2. *A project Dockerfile fragment* (for example `orchestration/images/platform.Dockerfile`),
   inserted verbatim. This is fully flexible, but any root command can run at build, and nothing
   can be validated or kept consistent with the layer's own steps.
3. *Documentation only*: build your own base image from Vecten's and point
   `VECTEN_IMAGE_REGISTRY` at it. This needs a per-project registry, which the `shared/deploy`
   spec rules out ("No per-project registry SHALL be required").
4. *Also let an extension's manifest declare system packages.* A pip install would then add root
   build steps to the images without the project naming them.

**Accepted: option 1, without option 4.** An extension that needs `libgomp1` says so in its
docs, and the project adds it. Versions are unpinned unless the project pins them (see Risks). A
manifest key `system_packages` would be reported as unknown (Decision 12), and can be added later
as a check that the project declared them.

### Decision 9: The read API is `vecten_shared.context` — *Accepted*

**Context.** DAG code runs in Airflow, connector code in Airflow and in the CLI, and tool code in
the MCP server. Runtime packages import independently of each other and of the CLI, so the API has
to live where all three already reach. That is `vecten-shared`, which both `vecten-platform` and
`vecten-mcp` depend on. Today connector code reads secrets through `vecten_platform.sync.secret`,
which MCP code cannot import.

**Options.**

1. *A handle per extension.*
   ```python
   from vecten_shared.context import extension, project
   acme = extension("acme-resolution")
   acme.settings["daily_cap"]      # read-only; default <- [settings.acme-resolution] <- env.<VECTEN_ENV>
   acme.secret("ACME_API_KEY")     # only names acme's manifest declares, for this component
   acme.env                        # "local", "prod", ...
   project().settings["alert_channel"]
   project().secret("SLACK_TOKEN")  # only names vecten.toml's top-level `secrets` lists
   ```
   It reads `vecten.toml` and `extensions.lock.json` from `VECTEN_PROJECT_ROOT`, the environment
   from `VECTEN_ENV` and the provider from `VECTEN_PROVIDER`, as the sync runtime does today.
   Compose sets `VECTEN_COMPONENT` (`platform` or `mcp`) and, for `mcp-server`,
   `VECTEN_PROJECT_ROOT=/app`. With no component set (the CLI process), it acts as `platform`.
   `secret()` refuses an undeclared name, and a name outside the reading component's scope, on
   every provider. A read therefore fails the same way on the laptop, on a `standard` VM and on a
   `minimal` VM, where the cloud alone would allow it.
2. *Module functions* such as `setting("acme-resolution", "daily_cap")` and
   `secret("ACME_API_KEY")`. These are simpler, but a secret read cannot be tied to the extension
   that declared it, so the declared-only rule cannot be enforced.
3. *Put the API in each runtime package.* That means two implementations, and connector code
   (platform) and tool code (MCP) from one extension would import different modules.

**Accepted: option 1.** `vecten_platform.sync.secret` keeps working unchanged for
connectors. A connector's secrets are declared on its connector declaration, not in a manifest,
so they stay outside the handle.

### Decision 10: Settings are a JSON Schema, checked at lock, up, ship and first read — *Accepted*

**Context.** A typo in a key (`dialy_cap`) or a string where a number belongs should fail before a
DAG runs, not halfway through one. `vecten-shared` already depends on `jsonschema`, and both
images install it.

**Options.**

1. *The manifest's `settings` is a JSON Schema for an object.* Vecten accepts top-level
   `properties` (each with a `type` among `string`, `integer`, `number`, `boolean`, `array` and
   `object`, plus optional `default`, `description`, `enum`, `minimum`, `maximum` and `items`)
   and `required`, and it forces `additionalProperties: false`. `vecten lock` refuses a schema
   outside that subset, and a property named `env`. The effective values for each environment
   (defaults, then project values, then the override) are validated:
   - by `vecten lock`, for `local` and every environment named under `env`;
   - by `up`, for `local`;
   - by `ship` and `ship --check`, for the environment being shipped;
   - by the MCP server at startup, before it reports healthy, because it loads every extension
     then anyway;
   - by the read API, on first access in any other process (a DAG, a connector run).
   An unknown key, a wrong type or a missing required value exits with the configuration code,
   naming `settings.<namespace>.<key>` and the environment.
2. *A small type vocabulary of Vecten's own* (`{"daily_cap": "int"}`). This is shorter, but it
   is one more format to document, and anything beyond a type (an enum, a range) would need more
   of it.
3. *No schema, only read-time errors in the extension's code.* Typos would then go unnoticed.

**Accepted: option 1.** `[settings.project]` has no schema and is checked for JSON types
only (Decision 13).

### Decision 11: A connector's `dependencies` still go into every lock — *Accepted*

**Context.** A connector declaration's `dependencies` (from a dlt verified source, or from an
extension connector's dictionary) are in all three locks today. Connectors run only in Airflow and
in the CLI, so the MCP image does not need them. But an extension may have put a package its MCP
tool also imports into its connector's `dependencies`. Dropping them from the MCP lock would break
that extension at its next lock.

**Options.**

1. *Unchanged: every enabled component's lock.*
2. *Platform and CLI only.* This is the cleaner end state and makes the MCP image smaller. It is a
   behaviour change for existing projects, and it can break an extension that declared nothing,
   which this change must not do.

**Accepted: option 1 for this change.** Extension authors are told to use extras (Decision
2) instead of a connector's `dependencies`. Option 2 is worth doing in the next minor version,
with a `vecten lock` report of the packages that would leave the MCP lock.

### Decision 12: Unknown manifest keys are reported, not refused — *Accepted*

**Context.** #40 (cloud and warehouse resources) and #42 (pools, variables, API users) will want
to add keys to the manifest. An extension built for a newer release will meet an older one.

**Options.**

1. *Report and ignore unknown top-level keys*, as `extension add` already does for unknown groups.
   An extension that declares `permissions` for a future release still installs, and the report
   says that key is not supported here.
2. *Refuse unknown keys*, as `vecten.toml` does. This is stricter, but it ties each extension
   release to the oldest Vecten release it supports.

**Accepted: option 1 for top-level keys only.** Inside `secrets` and `settings` everything is
strict: a malformed known key is refused.

### Decision 13: `[settings.project]` for the project's own code — *Accepted*

**Context.** The issue's workaround ("keep settings in a Python module in the project, keyed on
`VECTEN_ENV`") applies to project DAGs as well as extensions.

**Options.**

1. *Reserve the namespace `project`*, read with `project().settings`, with the same `env`
   overrides. It has no schema: values are checked for JSON types only. An extension whose
   normalised name is `project` is refused.
2. *Extensions only.* Project code keeps its own module.

**Accepted: option 1.** It costs one reserved name and removes the workaround entirely.

### Decision 14: Project code declares its own secrets in a top-level `secrets` list — *Accepted*

**Context.** Decision 13 covers project settings, but project DAGs and MCP code have the same
secrets gap as #49. On a VM they can read any name in their scope through the provider. On the
laptop only declared names reach the containers, so `secret("SLACK_TOKEN")` in a project DAG works
on a VM and fails on the laptop.

**Options.**

1. *A top-level `secrets = ["SLACK_TOKEN", "MCP_CRM_TOKEN"]` in `vecten.toml`*, with the same
   rules and delivery as a manifest's (Decision 3). It is read with `project().secret(NAME)`.
2. *A `secrets` key inside `[settings.project]`.* This mixes secret names into a table of values,
   and `secrets` would become a reserved setting name.
3. *Keep the Compose override as the answer for project code.* This is today's workaround, and it
   leaves the laptop and the VM behaving differently.

**Accepted: option 1.** Its `project().secret()` is bound by the same declared-only rule as
an extension's.

## Risks / Trade-offs

- [`save_config` drops comments] → Settings are hand-written values that deserve comments, but
  `bootstrap`, `user add` and other commands rewrite `vecten.toml` with `tomli_w`. Mitigation, a task
  in this change: `save_config` updates the parsed `tomlkit` document in place,
  as `extension add` already does. Every section benefits, not only `settings`.
- [Reserved extras `platform`, `mcp`, `cli`] → An extension that already uses one of these names
  for something else gets it installed in that component. This is unlikely, and documented. The
  fallback would be prefixed names (Decision 2, option 3).
- [Unpinned apt packages] → Two builds of the same commit can install different Debian versions.
  A project pins with `name=version` when it matters. The base images' Debian release changes
  with Vecten releases, so a package name can disappear. The layer build then fails on the VM, and
  `ship` keeps the previous bundle.
- [Locks stale after every Vecten upgrade] → The input hash includes the release, so `vecten
  update` is followed by `vecten lock`. Today the same upgrade fails later, at image build, with a
  less clear message. `next` reports the step.
- [The MCP VM reads declared secrets through the provider, the platform VM through the
  environment] → This asymmetry exists today between connector secrets and everything else. The
  read API hides it, and changing it would mean an agent change (see Decision 3).
- [Two declarations of one credential] → A credential needed by Airflow and by the MCP server is
  set twice under two names. The docs show the pattern. A shared scope stays possible later.
- [`extensions.lock.json` edited by hand] → It is generated. `up` and `ship` compare its versions
  with `requirements.txt` and the lock hashes, so a hand edit is detected as stale.

## Migration Plan

- **Existing extensions.** No manifest means no extension secrets or settings, and no extras means
  the base dependencies go into every lock. Both are today's behaviour.
- **Existing projects.** Locks without the `# vecten-lock:` header, and no
  `extensions.lock.json`, are accepted under today's name-by-name check until the first
  `vecten lock` or `extension add`, which rewrites `requirements.txt` to extension pins only and
  writes the record. A project with no records at all keeps installing its connector dependencies
  unlocked, as now. Declaring a `[dependencies]` Python requirement makes a lock mandatory, and
  `up` and `ship` then name `vecten lock`. `next` suggests `vecten lock` once.
- **Bundles.** `extensions.lock.json` joins the bundle content. An older bundle without it still
  starts. The read API then reports that settings are unavailable until the next ship.
- **Pull requests**, in order:
  1. Locking (#50, Decisions 2, 5 partly, 6, 7 without `system`, 11): `vecten lock`, extras,
     headers, preferences, the record with versions and extras, `[dependencies.*].python`, the
     project layer per component, legacy acceptance. About 3 to 4 days.
  2. System packages (Decisions 7 `system`, 8): validation, the generated apt step and a
     Docker-tier test. About 1 to 1.5 days.
  3. Manifest, secrets and settings (#49, Decisions 1, 3, 4, 5, 9, 10, 12, 13, 14): the group, the
     record's secrets and schema, `[settings]`, validation, Compose delivery, the `host.json` list,
     `vecten_shared.context`, `next`, the stub extension and the docs. About 4 to 5 days.
- **Rollback.** Each pull request is additive for projects that do not use its features. Reverting
  one leaves locks that an older CLI reads as stale. The fix is to re-run that CLI's
  `extension add`.

## Resolved Questions

- **Command name.** `vecten lock`, at the CLI's top level, because it also locks project-only
  requirements.
- **Order with #35.** Pull request 1 of this change lands before #35's implementation, and #35's
  delta to "Extensions are pip dependencies everywhere" is rebased onto this one's.
