# Contributing to Vecten Open

Thank you for looking. This page says what we merge, how to set up, how the tests are tiered,
and how a change to the requirements happens. Questions go to
[Discussions](https://github.com/vecten/vecten-open/discussions); bugs to
[Issues](https://github.com/vecten/vecten-open/issues) using the form there.

## What is welcome

- Bug fixes, with a test that fails before the fix.
- Documentation: a wrong sentence, a missing step, an unclear page.
- Tests, for behaviour the specs describe and the suite does not cover.
- Connectors, as [extensions](docs/extensions.md): a package the project installs, not code in
  this repository. Vecten Open ships no connectors of its own.

A new cloud, warehouse, CI host or orchestrator is outside the v0.1 scope and needs an issue
first, so we can agree the design before anyone writes it. The same goes for any change that
alters a requirement (see "Specs" below).

## Terms

By submitting a pull request you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), the licence of this repository, as its section 5 provides. We do
not ask for a contributor licence agreement or a sign-off line.

## Running from a checkout

The published package and images are the easy path (the README has it). From a checkout, build
the images locally and run the CLI through `uv`, from the checkout's root:

    uv sync --all-packages
    uv run vecten dev build-images
    uv run vecten init ../my-project --name my-project --users 5 --source hubspot
    checkout=$PWD && cd ../my-project
    uv run --project "$checkout" vecten up
    uv run --project "$checkout" vecten down

The plugin under `plugins/vecten`, the marketplace file and `docs/agent-guide.md` are rendered
from the skills in the `vecten` package. After editing a skill, run
`uv run vecten dev render-plugin --repo .` and commit the result; the suite fails while they
are out of sync.

## Tests

Lint with `uv run ruff check .` and `uv run ruff format --check .`. The unit suite runs with no
services:

    uv run pytest -m "not docker and not network and not terraform and not browser and not snowflake_live" -n auto --dist loadgroup

The other tiers are selected by marker and each needs something:

| Marker | Needs | Runs in CI |
|---|---|---|
| `docker` | a reachable Docker daemon | yes |
| `network` | GitHub and PyPI | yes |
| `terraform` | the `terraform` binary (1.16) and providers from the registry; set `TF_PLUGIN_CACHE_DIR=$HOME/.terraform.d/plugin-cache` | yes |
| `browser` | Playwright's Chromium and WebKit (`uv run --group browser playwright install --with-deps chromium webkit`) | yes |
| `snowflake_live` | a real Snowflake account and the maintainers' secrets | maintainers only |

No pull request may require a real cloud account to review or merge. Terraform changes are
checked with `terraform fmt`, `terraform validate` and the `terraform test` suites under each
module's `tests/`; real deploys happen on the maintainers' sandboxes before a release.

Comments in code state the constraint that makes the code correct, never the change history;
a hook lints them.

## Specs

The requirements the software is built to are in `.ai/openspec/specs/`, one folder per
capability, written as requirements with scenarios. They are the source of truth and the
tests follow them.

- A fix, a doc change or a test needs no spec change.
- A change in behaviour starts as an issue. Once agreed, it becomes an OpenSpec change under
  `.ai/openspec/changes/<name>/` with a delta spec, a design and a task list, in the same pull
  request as the code; the maintainer archives it into `.ai/openspec/specs/` on merge. The
  [OpenSpec](https://github.com/Fission-AI/OpenSpec) CLI generates the scaffolding, but a
  hand-written delta in the same shape is fine.

## Agents

Pull requests written with an AI agent are welcome; this is how the project itself is built.
The human who opens the pull request is responsible for it: they have read the diff, run the
tests, and can answer questions about it. `AGENTS.md` at the root holds the commands and rules
an agent needs in this repository.

## Commits and pull requests

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(`fix(mcp): …`, `docs: …`). Pull requests are squash-merged by a maintainer; the template asks
what changed, why, which tests ran and whether a spec or a doc moved.

## What to expect

We read every new issue and pull request within five working days. Releases are cut by the
maintainers; the [changelog](CHANGELOG.md) says what shipped.
