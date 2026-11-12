# Design: extensions that live inside the project repository

## Context

**The model.** Vecten keeps generic templates of its paid-tier extensions (a Teams or Slack bot, a
web portal, Streamlit dashboards) in a private repository. At the start of an engagement, staff copy
a template into the client's Vecten project repository and customise it there. From then on the
client's repository holds the real version; nothing pulls from the template repository again, so no
client laptop, CI job or VM needs a credential for it. Teams that share extensions across several
projects from their own private repository are served by the follow-up change
`extensions-from-git` (#35).

**What the code does today.**

- `vecten extension add` (`vecten/extensions.py`) refuses URLs and paths: `requirement()` in
  `vecten_shared/extensions.py` refuses any requirement with a URL, and `resolve_lock` and
  `stack/images.lock_lines` refuse any lock line with one.
- The runtimes pick entry points from installed distributions whose names are listed under
  `extensions` in `vecten.toml` (`selected_entry_points`); resource groups (`vecten.dags`,
  `vecten.dbt_packages`, `vecten.osi`, `vecten.skills`) return directories inside the installed
  package (`extension_paths`).
- Skills are exposed through machine-local symbolic links from `skills/<name>` and
  `plugins/vecten/skills/<name>` to the installed directory, tracked in
  `.vecten/extension-skills.json` and never committed.
- The project layer (`write_project_layer`) writes `airflow.Dockerfile` and `mcp.Dockerfile` into
  `.vecten/stack/build/`, the only build context; each runs one `pip install -r` of its lock,
  constrained by the base image's installed set.
- The bundle (`deploy/bundle.py`) copies a fixed list, `CONTENT`: `vecten.toml`, the requirements
  and locks, `orchestration/dags`, the Compose override, `connectors`, `dbt`, `osi` and `evals`.
  It refuses symbolic links and skips `.git`, `.venv` and `__pycache__`. Bundles are built only in
  GitHub Actions; on a laptop `ship` and `deploy` dispatch the workflow.
- On the VM, the agent (`deploy/vm/apply.py`, Python 3.9 standard library, byte-identical
  Terraform copy) runs `docker compose up -d --build` from the release's `.vecten/stack/`.

**The #50 change** (`spec/extension-declarations`, decisions accepted) adds `vecten lock`, which writes the three
per-component locks, `requirements.txt` (extension pins only) and a committed
`extensions.lock.json` (each extension's version, extras, secrets and settings schema). Each lock
starts with a hash of its inputs, and `up` and `ship` refuse a stale record. This change plugs in
there.

## Goals

- An extension package can live in the project under `extensions/<name>/` and be registered with
  `vecten extension add ./extensions/<name>`.
- It is installed from the project everywhere — the CLI, `vecten up`'s images, the VMs — and the
  same commit means the same code everywhere.
- Its third-party dependencies are locked like any other, from PyPI or the private index.
- The VM agent and the base images do not change.
- `vecten extension new` creates one, from a template or from a minimal skeleton, with no stored
  credential and no later fetch.

## Non-goals

- Services, HTTP routes and analytics apps (#38, #39). The bot, portal and Streamlit templates
  need them; until they land, an in-repo extension can ship anything the nine existing groups and
  #50's manifest cover.
- Installing from a separate git repository (follow-up change `extensions-from-git`).
- A template engine, template versioning or updating a project from a newer template.

## Decisions

Ordered with the most consequential and least reversible first. All were accepted by the
maintainer on 2026-11-12, each as recommended; the landing order follows the order agreed for #50
and #35.

### Decision 1: How the records hold an in-repo extension (*Accepted*)

**Context.** Whatever is recorded is committed in every project, and the record checks, the image
lock check and #50's input hashes all read it. Two facts shape the choice. The code's version is
the project commit, so nothing needs to pin it separately. And an in-repo extension's name may also
exist on PyPI: any step that resolves it by name could install a stranger's package (dependency
confusion).

**Options.**

- **A. Path lines everywhere.** `requirements.txt` and every lock hold `./extensions/<name>`, as
  pip and uv both accept. The locks are then not portable without the tree beside them, the image
  lock check must accept local paths, and uv may write the line as an absolute path or a `file:`
  URL depending on where the input file sits.
- **B. Path in `requirements.txt`, third-party packages only in the locks.** `requirements.txt`
  holds `./extensions/<name>[extras]`, the human-readable list of what the project installs. Locks
  are resolved with the path as an input but hold only third-party pins: Vecten removes the
  path's own line from uv's output. `extensions.lock.json` records, per in-repo extension,
  `source = {"path": "extensions/<name>"}`, its version, its extras and `metadata_sha256`, the
  sha256 of its `pyproject.toml`. #50's per-lock input hash includes that hash, so changing the
  extension's dependencies, extras or entry points makes the locks stale, and changing its code
  does not. The existing "no URL or path in a lock" check stays as it is.
- **C. Convention only.** Record nothing but the name; derive the path from `extensions/<name>`.
  Shortest, but `requirements.txt` would no longer list everything the project installs, and a
  reader cannot tell an in-repo extension from an index one.

**Accepted: B.** It keeps the locks free of paths (no change to their checks), puts the
path where a reader expects it, and makes staleness follow what actually affects resolution.
`vecten.toml` keeps names and extras, so the runtimes' parsing does not change.

### Decision 2: Which paths `extension add` accepts (*Accepted*)

**Context.** Paths are refused today because a path can point anywhere: outside the project, at
another machine's checkout, or through a link at files the bundle would never carry.

**Options.** (a) Only `extensions/<name>/`, one level, under the project root. (b) Any directory
inside the project. (c) A folder configured in `vecten.toml`.

**Accepted: (a)**, with these rules, each checked by `extension add`, `vecten lock`, `up`
and `ship`:

- the argument is relative (`./extensions/acme-portal` or `extensions/acme-portal`), or absolute
  but inside the project; it is normalised before any check, and a `..` component is refused
  whatever it would resolve to;
- it names exactly `<project>/extensions/<dir>/`, and `<dir>` equals the normalised distribution
  name in the package's `pyproject.toml`;
- neither the path nor any file or directory under it is a symbolic link (the bundle already
  refuses links, so this fails early rather than at ship);
- `pyproject.toml` declares `[build-system]` and a static `[project]` name and version.

A fixed folder makes the bundle content one entry, makes the record checks simple, and tells a
reader where every in-repo extension is.

### Decision 3: How the images install it, and whether a wheel is built at ship time (*Accepted*)

**Context.** The same source must reach the laptop's images and each VM's images; VMs may be arm64
while CI runners are amd64. The VM agent should do nothing new.

**Options.**

- **A. Build a wheel at ship time in CI, carry it in the bundle.** Needs pure-Python packages (or
  a wheel per architecture), a second artefact to keep in step with the source, and its own
  verification.
- **B. Carry the source in the bundle; build and install it inside the image build, on the
  target.** The bundle gains `extensions/` (one `CONTENT` entry). Compose's `additional_contexts`
  hands `../../extensions` to the build next to the existing context, so no file is copied twice
  and the agent's `docker compose up --build` picks it up unchanged. The generated Dockerfile,
  when the project has in-repo extensions, runs one `RUN` that bind-mounts that context
  read-write (writes are discarded with the step), installs the lock with `--no-deps`, then
  installs each in-repo extension with `pip install --no-deps`, then runs `pip check`. The build
  happens on the target architecture, so there is no portability rule beyond what the base image
  can build: a package that compiles code needs a compiler, which #50's `system` packages can add.
- **C. Mount the source into the running containers** and put it on `PYTHONPATH`. No install step,
  but entry points would not exist (they come from installed metadata), and the Compose file would
  change.

**Accepted: B.** It is the smallest design that installs the same source everywhere, and it
needs no artefact, no pure-Python rule and no new verification. The `--no-deps` installs close the
dependency-confusion hole of Decision 1: the lock is the full third-party closure, and nothing is
resolved by name at build time. Today's Dockerfile installs the lock without `--no-deps`; this
change moves every project to `--no-deps` once #50's locks are the full closure, and keeps today's
form for projects without locks.

**Build backends.** Building from source needs the package's backend, which pip fetches from the
index into an isolated build environment. `vecten lock` resolves each in-repo extension's
`[build-system] requires` to exact versions and the Dockerfile applies them as build constraints,
so every build of a commit uses the same backend. pip's handling of constraints in isolated builds
is to be confirmed by the spike; if it does not apply them, the fallback is a two-stage build that
installs the pinned backend in a throwaway stage and builds with `--no-build-isolation`, copying
only the wheel out through a mount.

### Decision 4: Same code everywhere, and whether to record a content hash (*Accepted*)

**Context.** "Same version everywhere" is the existing guarantee. For an in-repo extension the
version is the project commit: the bundle records its commit and holds that commit's
`extensions/`, and CI builds bundles only from committed code.

**Options.** (a) Record a hash of the whole extension directory in `extensions.lock.json` and have
the VM build verify it. (b) Record only `metadata_sha256` (Decision 1) and rely on the bundle's
commit for the code.

**Accepted: (b).** A code hash would make every code edit need `vecten lock`, which is noise
for the developer and protects nothing the bundle's commit does not already fix: bundles are
written only by CI, from a commit. The metadata hash is what decides whether the locks still hold,
and `up` and `ship` already refuse stale locks through #50's input hash.

### Decision 5: The CLI's environment installs it editable (*Accepted*)

**Context.** The CLI imports extensions for `connector add`, skills, Ossie documents and
`vecten lock` (manifests, #50).

**Accepted.** `extension add`, `vecten lock` and `extension sync` (if the follow-up adds it)
install the CLI lock with `--no-deps`, then each in-repo extension with
`uv pip install --no-deps --editable`. Code and resource edits are seen at once; a change to entry
points or dependencies needs `vecten lock`, which the metadata hash makes `up` ask for. Because the
installed directory is the project's own `extensions/<name>/…`, skill links point into the
project tree. They stay machine-local and uncommitted, as today.

### Decision 6: `vecten extension new` and its template format (*Accepted*)

**Context.** Staff hand a template to a client project by copying it in and renaming it. The copy
then belongs to the project. The template repository is private; the person running the command
already has access to it with their own git setup.

**Options for the format.** (a) A plain copy with a few placeholders, substituted in file
contents and file and directory names. (b) copier or cookiecutter: questions, conditionals and
later updates, at the cost of a new dependency and a second template language. Both use `{{ }}`,
which collides with dbt's Jinja in every model a template carries.

**Accepted: (a)**, with exactly these placeholders, chosen not to collide with Jinja, Python
or YAML:

| Placeholder | Replaced by | Example |
|---|---|---|
| `__vecten_name__` | the distribution name | `acme-portal` |
| `__vecten_package__` | the import package | `acme_portal` |
| `__vecten_title__` | a title from the name | `Acme Portal` |
| `__vecten_version__` | the running Vecten Open version | `0.1.1` |

Substitution applies to UTF-8 text files and to path names; other files are copied byte for byte.
An optional `vecten-template.toml` at the template's root holds a description and the oldest
Vecten Open version it supports; it is read, checked and not copied. `.git`, `__pycache__`,
`.venv`, `dist` and `build` are not copied, and a symbolic link in the template is refused.

**Sources.** `--template` takes a local directory, or `git+https://<host>/<repo>@<ref>` with an
optional `#subdirectory=<dir>`. A git source is read once into a temporary directory with
`git clone --depth 1 --branch <ref>` (or a fetch of a full commit), using the person's own git
configuration and credential helpers. Unlike #35's fetch path, nothing is isolated, because
nothing is recorded or fetched again; prompts are allowed. Any ref is accepted; the guide asks
staff to use tags. Without `--template`, a minimal skeleton packaged with the CLI is used.

**After the copy** the command refuses an existing target, writes the files, and runs
`extension add ./extensions/<name>` so the new extension is registered and locked in one step;
`--no-add` skips that. No provenance is written anywhere; staff record the template and its tag in
the commit message, as the guide asks.

### Decision 7: Interactions (*Accepted*)

- **Entry points and extras.** Unchanged: the distribution is installed, so `selected_entry_points`
  finds it by name, and #50's per-component extras (`platform`, `mcp`, `cli`) apply to in-repo
  extensions the same way.
- **DAGs, dbt, Ossie and skills from installed packages.** The spec's rule stands: they come from
  the installed package. In the images the package is installed (not editable), so resources must
  be package data, as for any extension; the skeleton and the guide say so. In the CLI the
  editable install points into the project tree. Each of the four kinds is covered by a test.
- **Plugin rendering.** Projects do not render the plugin; `vecten dev render-plugin` is for this
  repository's built-in skills only. Project skill links are covered above.
- **`vecten update`.** It touches only scaffold files it recorded in the pristine baseline, so
  `extensions/` is left alone. A release bump changes the base images, which makes the locks
  stale; `up` and `ship` then name `vecten lock`, as #50 states.
- **`connector add`.** An in-repo extension that declares `vecten.connectors` is offered like any
  other.
- **The rule "nothing from an extension is vendored".** It stays for extensions that come from an
  index or a git source. An in-repo extension is project code, and `extension new`'s copy is
  deliberate: the client owns it.
- **#38 and #39.** The bot needs a long-running service and a webhook route, the portal a route,
  the Streamlit templates an analytics-app group. Those are new entry point groups delivered by
  #38 and #39; this change only makes sure that an in-repo extension carries whatever those groups
  will need (its source tree, static files inside the package) to the host, which Decision 3's
  bundle content already does.

### Decision 8: Where the guides live (*Accepted*)

- **Consumer side, in vecten-open:** a section of `docs/extensions.md`, "Extensions in the
  project", covering layout, `extension add ./extensions/<name>`, `extension new`, dependencies,
  resources as package data, and what to review. It mentions templates in general terms only and
  does not name any private repository.
- **Producer side:** the guide for building and handing over templates, with an example template,
  its tests and a template CI workflow, lives in Vecten's private template repository, outside this
  repository. The maintainer may keep a public example template in a separate repository later.

### Decision 9: Landing order (*Accepted*)

1. #50's locking pull request (`vecten lock`, `extensions.lock.json`, per-component locks).
2. This change: in-repo extensions, then `extension new`.
3. The follow-up `extensions-from-git` (#35), lower priority, built on both.

This change's delta of "Extensions are pip dependencies everywhere" is written over #50's text of
that requirement and assumes #50 is archived first; the follow-up's delta is written over this
one's.

## Risks and trade-offs

- **pip and build constraints** (spike): whether pip applies constraints inside isolated builds.
  Fallback: a two-stage build with `--no-build-isolation` (Decision 3).
- **Compose `additional_contexts`**: supported by the pinned Compose plugin (5.5.1) as far as known;
  the spike confirms it on both clouds' VMs. Fallback: copy `extensions/` into the build context
  at render time, which doubles it in the bundle.
- **Builds on the VM run backends from the index.** As today for any source distribution; the
  backend version is pinned by the lock.
- **A compiled in-repo extension** builds on the VM and needs a compiler in the image, added with
  #50's `system` packages. Documented, not prevented.
- **Dependency confusion** is closed only if every install uses `--no-deps` with full-closure
  locks; a test asserts no generated install step resolves by name.
- **Editable installs in a shared CLI environment** expose one project's working tree to another
  project using the same environment. The docs already advise one environment per project.
- **Templates diverge from their source** by design. A fix to a template does not reach projects
  already created from it; the guide says how staff carry a fix over by hand.

## Migration plan

- Additive. A project without `extensions/` gets byte-identical records, Dockerfiles, Compose files
  and bundles; a test asserts it. Projects that adopt #50's locks get `--no-deps` installs from
  their next `vecten lock`.
- The bundle gains one entry; an older agent builds a newer bundle unchanged, since Compose reads
  the extra context from the Compose file.
- A project that today publishes its own package to a private index only to install it in that same
  project can move it into `extensions/`, run `extension add ./extensions/<name>`, and drop the
  index if nothing else needs it.
