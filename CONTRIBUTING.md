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

## Branches

Work for the next version lands on its release branch, `release/<version>`; with 0.1.0
released, that is `release/0.1.1`. Branch from it and open your pull request against it.
`main` holds released code only: every commit on it is a release or a security hotfix, and CI
fails a pull request to `main` from any other branch. The maintainers are
[@m-puchala](https://github.com/m-puchala) and [@haxoza](https://github.com/haxoza).

| Pull request into | From | Needs | Merged as |
|---|---|---|---|
| `release/<version>` | any branch or fork | a maintainer's approval; a maintainer's own pull request needs none | a squash |
| `main` | `release/<version>` | the other maintainer's approval | a merge commit |
| `main` | `hotfix/<name>` | the other maintainer's approval | a squash |

Every merge also needs CI green, and no one bypasses that, the maintainers included. One
release branch is open at a time. It opens as the next
patch version; when a breaking change is accepted, a maintainer renames it to the next minor
(`release/0.2.0`), which GitHub carries over to the open pull requests.

A hotfix is for a vulnerability handled under [SECURITY.md](SECURITY.md) and nothing else: an
urgent bug that is not a vulnerability goes to the release branch. A maintainer opens the
`hotfix/<name>` branch from `main`.

## Commits and pull requests

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(`fix(mcp): …`, `docs: …`). The template asks what changed, why, which tests ran and whether a
spec or a doc moved. A change a user would notice adds a line under `[Unreleased]` in the
[changelog](CHANGELOG.md), in the same pull request.

## Releasing

For maintainers. Versions follow [Semantic Versioning](https://semver.org/), the four
distributions share one, and the tag is `v<version>`.

1. As the last commit on the release branch: move the `[Unreleased]` entries of the changelog
   under `## [<version>] - <date>`, update the compare links at its foot, run
   `uv run vecten dev bump-version <version>` and confirm the lock with
   `uv sync --locked --all-packages`. Until this commit the branch reports the released
   version.
2. Open the pull request from the release branch to `main`; the other maintainer approves and
   merges it with a merge commit.
3. Tag the merge commit `v<version>` with an annotated tag and push the tag. The Terraform
   modules are fetched by this tag, so nothing deploys from the version before it exists.
4. Publish the packages and the images, then run `uv run vecten dev check-images`.
5. Rename the merged branch to `release/<next patch>`. Its open pull requests follow the
   rename; deleting the branch would move them onto `main`.

A hotfix carries its own version: the `hotfix/<name>` branch takes the next patch version, with
the same bump and changelog entry as step 1, and is tagged and published as steps 3 and 4.
The open release branch is then renamed to the patch version after it, and a pull request
brings `main` into it as a merge commit. A squash would leave the hotfix out of the branch's
history, and the next release would conflict in every file the bump edits.

## What to expect

We read every new issue and pull request within five working days. Releases are cut by the
maintainers; the [changelog](CHANGELOG.md) says what shipped.
