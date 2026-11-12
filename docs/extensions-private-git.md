# Extensions from a private git repository

A team that shares extensions across several projects can keep them in one git repository of
its own and install them straight from a tag of that repository: no private package index, the
same commit on every laptop, in CI and on every host, and no credential for the private code on
any deployed machine. This page covers both sides: the repository that holds the extensions, and
the projects that use them. For extensions in general, read
[Installing and writing extensions](extensions.md) first.

The examples use a made-up organisation, `acme`, whose repository `github.com/acme/extensions`
holds two extensions.

## How it works

1. `vecten extension add` names a tag or a full commit of the repository. Vecten resolves the
   tag to its commit once, fetches that commit, and records it in `requirements.txt`, every lock
   and `extensions.lock.json`, together with `source_sha256`, a hash of the extension's files
   at that commit.
2. On a laptop the git credential lives in the project's `.env`; in CI, in a GitHub secret or a
   GitHub App. Vecten hands it to git alone, through a file that exists for one fetch, and never
   to pip, uv or a package's build.
3. `vecten ship` runs in CI. It fetches each recorded commit again, refuses a source whose
   `source_sha256` differs from the record, and puts the source into every host's bundle at
   `extensions/<name>/`.
4. Each host builds the extension from the bundle like an [in-repo
   extension](extensions.md#extensions-in-the-project), after checking `source_sha256` once
   more. The host needs no git, reaches no git host and holds no credential.

`vecten up` does the same on the laptop, from a copy it keeps under `.vecten/stack/sources/`,
never in the project's tracked files.

## The repository

### Layout

Keep one directory per extension, each a complete Python package with its own `pyproject.toml`:

```text
extensions/
  acme-resolution/
    pyproject.toml
    src/acme_resolution/...
  acme-portal/
    pyproject.toml
    src/acme_portal/...
```

A project names the directory with `#subdirectory=extensions/acme-resolution`. A repository
holding a single extension at its root needs no subdirectory. The directory may not contain
symbolic links or git submodules, and a file kept in Git LFS arrives as its pointer file,
not its content.

### Packaging

`pyproject.toml` must declare, without running any code:

- `[build-system]` with its `requires` and `build-backend`;
- a static `[project]` `name`, equal to the name a project gives it;
- a static `[project]` `version`; `dynamic = ["version"]` is refused.

Any build backend works. `vecten extension add` pins the backend and its dependencies to exact
versions, and every build of that commit uses those pins. The extension's own dependencies come
from PyPI, or from the project's private index when it has one; the git host is asked for the
extension's repository and nothing else. Scope dependencies to a component with the `platform`,
`mcp` and `cli` extras, as for any extension.

The extension is built on each host that installs it, for that host's architecture. An extension
with C or Rust code needs a compiler in the image: list it in the project's
`[dependencies.platform] system` (and `[dependencies.mcp] system` when the MCP image installs
it), as [Dependencies and locks](extensions.md#dependencies-and-locks) describes.

### Releasing

Tag each release with the extension's name as a prefix: `acme-resolution/v1.2.0`. When a tag
has the form `[<prefix>/]v<version>`, `vecten extension add` refuses it unless `<version>` is the
version in `pyproject.toml` at that commit, so a tag cannot claim a release it does not hold.

Protect the tags. A tag moved to another commit or deleted can leave projects pointing at a
commit the host no longer serves, and their ships then fail with "commit not reachable" until
someone records a new one. On GitHub, add a tag ruleset (Settings, Rules, Rulesets, New tag
ruleset) targeting `*/v*` and `v*` that restricts updates and deletions.

## A project that uses it

### Add an extension

```sh
vecten extension add 'acme-resolution @ git+https://github.com/acme/extensions@acme-resolution/v1.2.0#subdirectory=extensions/acme-resolution'
```

The form is `<name>[extras] @ git+https://<host>/<path>@<ref>[#subdirectory=<dir>]`. The ref is
a tag or a full 40- or 64-character commit hash. These are refused, and no file changes:

- a branch, an abbreviated hash, a name that is both a tag and a branch, or a missing ref;
- any scheme other than `git+https`, including `ssh://` and plain `http://`;
- a URL with a user name or token in it (credentials belong in `GIT_CREDENTIALS`), a query, or a
  fragment other than `subdirectory=`;
- a subdirectory that is absolute or leaves the repository;
- a package whose name differs from the name given, or whose metadata is dynamic.

What the command records:

| File | What it holds for the extension |
|---|---|
| `vecten.toml` | Its name and extras, as for any extension. |
| `requirements.txt` | `acme-resolution @ git+https://github.com/acme/extensions@<full commit>#subdirectory=extensions/acme-resolution  # tag acme-resolution/v1.2.0` |
| every lock | The same line. |
| `extensions.lock.json` | `source` (URL, commit, subdirectory, tag, and whether the host answered with no credential), `source_sha256` and the pinned `build_requires`. |

`requirements.txt` is the source of truth for the commit. `vecten lock` keeps it and never moves
it to where the tag points now. `up`, `ship` and the image build refuse records that disagree,
naming `vecten extension add`.

### The credential on a laptop

Put `GIT_CREDENTIALS` in the project's `.env`, which is never committed:

```sh
GIT_CREDENTIALS=https://x-access-token:<token>@github.com/acme
```

Each entry is `https://<username>:<token>@<host>[/<path>]`; separate several entries with
spaces. An entry with a path covers only repositories under that path, so one host can carry a
token per organisation; the entry with the longest matching path wins. When `.env` holds no
`GIT_CREDENTIALS`, Vecten reads it from the environment.

On GitHub, use a fine-grained personal access token limited to the extensions repository with
read access to its contents. Other hosts take their own user names with an access token:

| Host | Username | Token |
|---|---|---|
| GitHub | `x-access-token` (any non-empty name works) | fine-grained personal access token |
| GitLab | `oauth2` | personal, group or project access token with `read_repository` |
| Bitbucket Cloud | `x-token-auth` | repository or workspace access token |
| Azure DevOps | any non-empty name | personal access token with Code (Read) |

A public repository needs no credential. `vecten secret set GIT_CREDENTIALS --env <env>` is
refused: the git credential never goes into an environment's secrets store, because no deployed
machine needs it.

### The credential in CI

`vecten ship` in CI fetches every git-sourced extension again on every ship, a promotion of an
already-shipped commit included, so CI needs read access to the repository. Give it one of
these, or both:

- **A secret.** Set `VECTEN_GIT_CREDENTIALS` to the same entry format, as a repository secret or
  an environment secret; an environment secret overrides the repository's for that
  environment:

  ```sh
  gh secret set VECTEN_GIT_CREDENTIALS --env prod
  ```

- **A GitHub App.** Create an App with read access to repository contents and install it on the
  extensions repository. Then set the variables `VECTEN_GIT_APP_ID` (the App's client ID or App
  ID), `VECTEN_GIT_APP_OWNER` (the organisation it is installed in) and
  `VECTEN_GIT_APP_REPOSITORIES` (`extensions`, or a comma-separated list), and the secret
  `VECTEN_GIT_APP_PRIVATE_KEY` (the App's private key). Each ship then mints a token that lasts
  for that job only and reads only the repositories named. The token is minted only when all
  four are set, so it never reaches every repository the App is installed on.

With both, the step that runs `vecten` gets the App's token as an entry for `github.com` and the
secret's entries beside it, so an App on GitHub and a token for another host can serve one
project. For a repository that two entries cover, the entry with the longer path wins, as on a
laptop.

The rendered `deploy-platform.yml` and `terraform-apply.yml` pass the credential, as
`GIT_CREDENTIALS`, to the step that runs `vecten ship` or `vecten deploy` and to no other step;
`ship.yml` passes both secrets to `deploy-platform.yml` by name. A project rendered by an earlier
release gets these steps with `vecten update`. GitHub masks the secret and the minted token in
logs, and Vecten registers every token inside a multi-entry value with GitHub's masking before
running git.

Before `vecten ship --env <env>` or `vecten deploy --env <env>` on a laptop dispatches CI, it
checks by name that `VECTEN_GIT_CREDENTIALS`, or all four names of the GitHub App, are set for
the repository, the environment or the organisation, when any extension's repository needed a
credential at `extension add`. Otherwise it refuses and names what is missing. Listing secrets
and variables needs admin rights on the repository: for a person without them, the check skips
the lists it may not read, says so in a warning, and dispatches. It cannot read a secret's
value: a missing, wrong or expired token fails the ship in CI, before any host changes.

### Rotating the credential

- **Laptop.** Replace the token in `.env`. Nothing else holds it.
- **CI secret.** Run `gh secret set VECTEN_GIT_CREDENTIALS --env <env>` (or without `--env` for
  the repository secret) with the new value, then revoke the old token at the host.
- **GitHub App.** Generate a new private key in the App's settings, set it with
  `gh secret set VECTEN_GIT_APP_PRIVATE_KEY`, then delete the old key.

A revoked or expired token fails the next ship in CI naming `authentication refused`, with every
host left on its previous release.

### Upgrade to a new tag

```sh
vecten extension add 'acme-resolution @ git+https://github.com/acme/extensions@acme-resolution/v1.3.0#subdirectory=extensions/acme-resolution'
```

The command fetches the new commit, checks the tag against the version, records the commit and
its `source_sha256`, re-locks, and names the previous and the new commit. Re-adding a tag that
was moved records the commit it points at now and says that the tag moved; an unchanged tag
changes nothing. Naming an index version (`acme-resolution==1.3.0`) or a path
(`./extensions/acme-resolution`) switches the source, and the output says so. A bare name for a
git-sourced extension is refused. Commit the changed records and open a pull request.

On another laptop, or in a CI job that tests against the project's extensions, install what the
records hold:

```sh
vecten extension sync
```

It installs the committed `requirements.lock` into the environment running Vecten, each
git-sourced extension from a fetched copy whose `source_sha256` matches the record, and each
in-repo extension from its folder, then refreshes the skill links. It changes no record. It
refuses a stale lock, naming `vecten lock`, and a git source whose lines disagree with
`extensions.lock.json` or whose fetched source has another hash, naming `vecten extension add`. The rendered `ci.yml` does not run it,
since that would put the credential in every pull request's job.

## Reviewing an upgrade

A pull request that moves an extension to a new tag changes `requirements.txt`, the locks and
`extensions.lock.json`. The reviewer checks:

1. **The tag against the commit.** `git ls-remote https://github.com/acme/extensions
   'refs/tags/acme-resolution/v1.3.0^{}' refs/tags/acme-resolution/v1.3.0` lists the commit the
   tag points at; it matches the commit in `requirements.txt`.
2. **The version.** `version` in `extensions.lock.json` is the version the tag names.
3. **The source change.** `source_sha256` changed with the commit. Review the extension's diff
   between the two commits in its repository, as for any dependency upgrade.
4. **New secrets and settings.** The extension's entry in `extensions.lock.json` lists the
   secrets it reads and its settings schema; a new secret must be set before the ship.
5. **Dependency changes in each lock.** `requirements.lock`, `requirements-airflow.lock` and
   `requirements-mcp.lock` show what the new version adds or moves for the CLI, the Airflow image
   and the MCP image.

Back to the [docs index](README.md).
