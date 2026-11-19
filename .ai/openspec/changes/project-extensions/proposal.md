# Extensions that live inside the project repository

Related issues: [#35](https://github.com/vecten/vecten-open/issues/35) (private extensions),
[#50](https://github.com/vecten/vecten-open/issues/50) (locking, which this change builds on),
[#38](https://github.com/vecten/vecten-open/issues/38) and
[#39](https://github.com/vecten/vecten-open/issues/39) (services, routes and analytics apps, which
later templates need).

## Why

An extension can only come from a package index today. `vecten extension add` refuses paths, and
the shipped bundle carries a fixed list of project paths (`CONTENT` in `deploy/bundle.py`), so a
package that sits in the project repository can reach neither the images nor the hosts.

That rules out the simplest way to own custom code: keep it in the project. A client engagement
often starts from a generic extension — a chat bot, a portal, a dashboard — that is then
customised for that client. The customised copy belongs to the client's project repository,
reviewed and versioned with everything else, and nothing should pull from anywhere after it is
copied in. That needs two things: an extension package can live in the project and be installed
everywhere from there, and a command can create one from a template.

## What changes

- A project may hold extension packages under `extensions/<distribution-name>/`, each a normal
  Python package with its own `pyproject.toml` and `vecten.*` entry points.
- `vecten extension add ./extensions/<name>` registers one. Only a directory exactly one level
  under the project's `extensions/`, named after its distribution, reached without `..` and
  without a symbolic link anywhere on the way or inside it, is accepted.
- Its version identity is the project commit. `vecten.toml` lists its name and extras;
  `requirements.txt` holds its relative path (`./extensions/<name>`); the locks hold only
  third-party packages, including its dependencies; `extensions.lock.json` (#50) records its path,
  version and a hash of its `pyproject.toml`, which makes the locks stale when its metadata
  changes and not when its code changes.
- It is installed from the path everywhere: editable in the CLI's environment, so edits are seen at
  once, and built from source inside the image build on the laptop and on each VM. The bundle
  carries `extensions/`, and Compose hands it to the build as an additional build context, so the
  VM agent does nothing new.
- Every lock is installed with `--no-deps` and every in-repo extension with `--no-deps`, so no
  package is ever looked up by an in-repo extension's name on an index.
- `vecten extension new <name> [--template <source>]` creates `extensions/<name>/` from a
  template — a local directory, or a git URL and ref read once with the person's own git setup —
  or from a minimal built-in skeleton. It is a plain copy with four documented placeholders,
  records nothing about where the template came from, and never fetches again.
- Consumer documentation in `docs/extensions.md` covers in-repo extensions and `extension new`.
  A producer guide for Vecten's private template repository is drafted in this change folder.
- Installing from a tag of a separate private git repository (#35) follows as the change
  `extensions-from-git`, built on this one.

## Capabilities

- **Modified** `shared/extension-points`: "Extensions are pip dependencies everywhere" names the
  in-repo source; new requirements for in-repo extensions and their accepted paths.
- **Modified** `shared/deploy`: "Content ships on merge without Terraform" adds `extensions/` to
  the bundle; a new requirement for building in-repo extensions on the host.
- **Added to** `cli/cli-lifecycle`: `extension new`.

## Impact

- Code: `vecten.extensions` (path parsing and validation, records), #50's `vecten lock` (path
  extensions as inputs, third-party-only locks, `extensions.lock.json` entries),
  `vecten.stack.images` (install step, `--no-deps`), `vecten.stack.compose` (additional build
  context), `vecten.deploy.bundle` (`CONTENT`), `vecten.extension_skills` (links into the project
  tree), a new `cli/commands/extension.py` subcommand and a built-in minimal skeleton.
- Not changed: the VM agent and its Terraform copy, `host.json`, the base images, the secrets
  stores.
- Docs: `docs/extensions.md`, `docs/cli.md`, `docs/GLOSSARY.md` ("In-repo extension", "Bundle"),
  the changelog.
