# Design: install extensions from a git tag in a separate private repository

## Context

This change follows `project-extensions` (in-repo extensions under `extensions/<name>/`) and #50's
`vecten lock` and `extensions.lock.json`, and is written against both. It serves a team that shares
extensions across several projects from one private repository of its own. Vecten's own paid-tier
templates do not use it: they are copied into each client project with `vecten extension new` and
become in-repo extensions.

What it starts from:

- Bundles are built only in GitHub Actions: on a laptop, `vecten ship` and `vecten deploy` dispatch the
  workflow and follow it (`deploy/run.py`, `in_actions()`). The reusable `deploy-platform.yml` job
  installs `vecten` with `uv tool install` and runs `vecten ship`, which writes one bundle per host,
  including the image build context `.vecten/stack/build/`.
- The VM agent (`deploy/vm/apply.py`, Python 3.9 standard library, byte-identical Terraform copy) runs
  `docker compose up -d --build`; it must not change.
- Both base images carry `vecten-shared` at the release version; only the Airflow image carries git.
- After `project-extensions`, the bundle carries `extensions/`, Compose hands it to the image build as
  an additional context, and the generated Dockerfile installs each lock with `--no-deps`, then each
  in-repo extension from its source with `--no-deps` and pinned build constraints, then runs
  `pip check`.

## Goals and non-goals

Goals: a tag or full commit of an HTTPS git repository as a third source; the same commit everywhere,
recorded in every record and checked by a hash of its source; no branches; subdirectories and prefixed
tags for repositories holding several extensions; the credential only on laptops and in CI; one install
path for in-repo and git-sourced extensions; public dependencies from PyPI; the private index unchanged.

Non-goals: SSH URLs and deploy keys; a credential on any host; git sources for connector or project
dependencies; submodules and LFS; local editable overrides of a git-sourced extension (an author can
test an unreleased change by adding its full commit on a scratch branch of a project).

## Decisions

All decisions were accepted by the maintainer on 2026-11-12, as recommended; the open variant of
Decision 3 was resolved to shipping source.

### Decision 1: Records (*Accepted*)

`requirements.txt` is the source of truth and holds
`<name>[extras] @ git+https://<host>/<path>@<full commit>[#subdirectory=<dir>]  # tag <tag>`; the
comment is ignored by pip and uv. Every lock holds the same line. `extensions.lock.json` mirrors it:
`source` (URL without credentials, `commit`, `subdirectory`, `tag`, and `public`, true when
`extension add` resolved the ref with no credential), `source_sha256` (Decision 4) and
`build_requires` pinned exactly. `vecten lock` writes them together; `up`, `ship` and the image build
refuse any mismatch. Only `extension add` with that extension's source changes its commit; no re-lock
resolves a tag again. `vecten.toml` keeps names and extras.

### Decision 2: Fetch, then build with no credential in reach (*Accepted*)

Vecten resolves refs with `git ls-remote` (at `extension add` only) and fetches the recorded commit with
`git fetch --depth 1 <url> <commit>` into a throwaway bare repository, then checks the extension's
directory out into a fetched copy. The credential reaches git only through a `get`-only credential
helper reading a mode-0600 or tmpfs file that exists for the fetch alone, with `credential.helper`
reset first, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, the
askpass variables cleared and every `GIT_TRACE*` and `GIT_CURL_VERBOSE` removed. The file is deleted
before anything else runs.

The fetch runs on a laptop (`extension add`, `vecten lock`, `up`, `extension sync`) and in CI
(`ship`). uv resolves the locks through a `url.file://<copy>.insteadOf=https://<host>/<path>` rewrite,
with no credential in its environment. The spike checks that uv honours the rewrite; if it does not,
the laptop falls back to handing uv the credential helper and hiding its output. Builds read the
fetched copy directly and never need the rewrite.

### Decision 3: CI ships the fetched source in the bundle; the host builds it like an in-repo extension (*Accepted*)

`vecten ship` in CI fetches each git-sourced extension's recorded commit through Decision 2, checks its
`source_sha256` against `extensions.lock.json`, and writes the source into every host bundle at
`extensions/<name>/`, beside the in-repo extensions. A distribution name has one source, so the two
never collide. The image build then installs it exactly as `project-extensions` Decision 3 installs an
in-repo extension: Compose's additional context, the lock with `--no-deps`, each extension with
`--no-deps` and its pinned build constraints, then `pip check`. Before installing, the build verifies
each git-sourced directory's `source_sha256` against the bundled `extensions.lock.json`.

`vecten up` does the same on the laptop, from a fetched copy kept under `.vecten/` and never written
into the project repository. The VM needs no git and no credential. Nothing about this touches the VM
agent, `host.json`, Compose secrets or the base images.

Rejected: build a wheel in CI and ship it. It needs a content digest that survives ZIP noise and stays
equal between macOS and Linux builds, which even pinned backends do not guarantee; a pure-Python rule,
because an amd64 runner cannot build for an arm64 VM; a wheel cache and a verify module in the images;
and a second install path beside the in-repo one. This change first followed that direction. Shipping
source supersedes it, because in-repo extensions now provide a source build path on every host.

### Decision 4: The source's identity is recorded once and verified in CI and on the host (*Accepted*)

`source_sha256` is the sha256 over the sorted list of, for every file in the extension's directory at
the commit (the subdirectory when one is given), its path relative to that directory, the sha256 of its
bytes and its executable bit. Only `.git` is excluded. A symbolic link anywhere in the directory is
refused, as for in-repo extensions.

`vecten lock` computes it on the laptop from the fetched copy. CI recomputes it after each fetch and
refuses to ship on a mismatch, before any host changes. The image build verifies it on the host before
installing. The hash depends only on the files git stores, so it is the same on macOS and Linux and
needs no reproducibility spike.

### Decision 5: Compiled extensions are allowed (*Accepted*)

A git-sourced extension builds on its target host, like an in-repo extension, so its architecture is
the host's. An extension that compiles code needs a compiler in the image, which #50's `system`
packages add. There is no pure-Python rule.

### Decision 6: Credentials (*Accepted*)

- **Laptop:** `GIT_CREDENTIALS` in `.env`, whitespace-separated entries in git's credential-store
  format, `https://<username>:<token>@<host>[/<path>]`; an entry with a path covers one organisation on a
  shared host. `vecten secret set GIT_CREDENTIALS --env …` (and `MCP_GIT_CREDENTIALS`) is refused.
- **CI:** the rendered `deploy-platform.yml` declares optional secrets `VECTEN_GIT_CREDENTIALS` and
  `VECTEN_GIT_APP_PRIVATE_KEY`, which `ship.yml` and `terraform-apply.yml` pass by name. When
  `vars.VECTEN_GIT_APP_ID` is set, a step mints an installation token with
  `actions/create-github-app-token` (pinned by commit in the versions table) for
  `vars.VECTEN_GIT_APP_OWNER` and `vars.VECTEN_GIT_APP_REPOSITORIES`. Only the `vecten ship` step receives
  `GIT_CREDENTIALS`: the token as `https://x-access-token:<token>@github.com`, else the secret. GitHub
  masks secrets and the minted token; under `GITHUB_ACTIONS=true`, `vecten` also emits `::add-mask::` for
  each token inside a multi-entry value. An environment secret of the same name overrides the repository
  secret, since the job declares the environment.
- **Deploy keys:** deferred; they can be added later by rewriting the HTTPS records to SSH at fetch time,
  without changing records.

### Decision 7: Tags or commits only, including prefixed tags (*Accepted*)

`git ls-remote` resolves the ref: a full 40- or 64-hex hash is a commit; an exact `refs/tags/<ref>` is a
tag (annotated tags peeled); a branch, a name that is both, a missing ref or an abbreviated hash is
refused. A ref may contain `/`, so `<extension>/v<version>` resolves as a tag. When the tag matches
`[<prefix>/]v<version>`, `extension add` refuses a version that differs from the static version at that
commit.

### Decision 8: Upgrades (*Accepted*)

A new tag fetches the new commit, checks it, records its `source_sha256`, re-locks and reports the old and
new commit. Re-adding a moved tag records the new commit and says the tag moved; an unchanged tag is a
no-op. Switching between index, in-repo and git sources is allowed and reported. A bare name for a
git-sourced extension is refused. Other extensions' git lines are carried over verbatim. Refused inputs:
user information in the URL (without echoing it), `ssh`/`http`/`file` schemes and paths, queries,
fragments other than `subdirectory=`, absolute or `..` subdirectories, markers.

### Decision 9: Only registered extensions may come from git (*Accepted*)

Every other lock line is an index pin; transitive URL dependencies are refused.

### Decision 10: Build backends and static versions (*Accepted*)

`pyproject.toml` at the commit must declare `[build-system]` and a static name and version, read with
`tomllib`. Any backend is accepted, and `vecten lock` pins it to exact versions used as build
constraints everywhere. The build runs on the host's image build, or on the laptop for `up`, as for an
in-repo extension, never with the credential in reach. It runs repository code, with the same trust as
importing the extension.

### Decision 11: Failures (*Accepted*)

Fetch failures name the host, repository path, ref and one cause: authentication refused (naming the
credential to set), repository not found or not visible, ref not found, commit not reachable, branch or
ambiguous ref, network failure. Raw git output is shown only after masking every token, every entry, URL
user information and `Authorization` values. A `source_sha256` mismatch, in CI or in the image build,
names the extension, the commit and both hashes. A dynamic version names the extension and the commit.
Build failures occur in the image build on the host and name the extension and the commit; on a VM they
go through the agent's existing masking.

### Decision 12: Each ship fetches again (*Accepted*)

Every ship in CI, including a promotion with #37's `vecten promote`, fetches the recorded commit again,
so CI needs the credential on every ship. The fetch is shallow and limited to one commit. A verified
source cache keyed by `source_sha256` can be added later if fetching becomes a cost.

### Decision 13: `vecten ship` from a laptop (*Accepted*)

A laptop never builds a bundle; it dispatches CI. Before dispatching, when the records hold git sources
whose `public` flag is false, it checks by name with `gh` that `VECTEN_GIT_CREDENTIALS` or
`VECTEN_GIT_APP_ID` is configured for the repository or the environment; otherwise it refuses, naming
what to set.

### Decision 14: `vecten extension sync` (*Accepted*)

Installs the committed `requirements.lock` into the environment running Vecten with `--no-deps`:
git-sourced extensions from their fetched copies, non-editable, after verifying `source_sha256`; in-repo
extensions as `project-extensions` does. It refreshes skill links. It reads `GIT_CREDENTIALS` from
`.env`, else from the process environment. It changes no record and refuses stale records. It is useful
for a second laptop and for CI jobs that test against the project's extensions; it is not added to the
rendered `ci.yml`, which would need the credential on every pull request.

### Decision 15: Hosts other than GitHub (*Accepted*)

Any HTTPS git host; tested against GitHub (maintainers) and a local `git http-backend` (CI); GitLab,
Bitbucket and Azure DevOps username conventions documented. If a host refuses to fetch by hash, fetch the
commented tag and check its commit. SSH, plain HTTP and `file:` are refused. App tokens are GitHub-only.

### Decision 16: Documentation (*Accepted*)

`docs/extensions-private-git.md` in vecten-open, for both sides of a team's own repository: layout (one
directory per extension, subdirectories), packaging (static version, pinned backend; compiled code needs
the project's `system` packages), releasing (prefixed tags, tag rulesets that forbid moving and
deleting), credentials and rotation on the laptop and in CI, adopting a new tag, and the reviewer's
checklist for an upgrade. It names no private repository.

### Decision 17: Landing order (*Accepted*)

#50's locking pull request, then `project-extensions`, then this change. The fetch module
(`vecten_shared.git_sources`, which also computes `source_sha256`) may land in parallel with either; the
record, lock, bundle and Dockerfile parts land after both. This change's delta of "Extensions are pip
dependencies everywhere" is written over `project-extensions`' text.

## Risks and trade-offs

- uv behaviour (spike): the `insteadOf` rewrite, the GitHub fast path, the full commit in compile output.
- CI needs the git credential on every ship, promotions included (Decision 12).
- A force-moved tag can orphan a recorded commit: ships fail with "commit not reachable" until
  `extension add` records a new one. Tag rulesets prevent it.
- Bundle size grows by the fetched sources.
- Builds on the host run the extension's build backend from the index, as for any in-repo extension; the
  backend version is pinned by the lock.
- The App token step relies on repository variables being set before the first ship; Decision 13 names
  them.

## Migration plan

Additive: projects without git sources get identical records, Dockerfiles, Compose files and bundles.
Existing projects get the workflow steps through `vecten update`. Nothing changes on VMs. A project that
hosts its shared extensions on a private index can move them one at a time, then drop `private_index`
and the `PACKAGE_INDEX_URL` / `MCP_PACKAGE_INDEX_URL` secrets, which also removes the index credential
from its VMs. To roll back, re-add the extension from the index.
