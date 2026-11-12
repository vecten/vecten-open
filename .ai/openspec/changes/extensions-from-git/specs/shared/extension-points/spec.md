## MODIFIED Requirements

### Requirement: Extensions are pip dependencies everywhere

An extension SHALL be resolved as an ordinary pip dependency, at the same version on a laptop, in CI, in the Airflow image and in the MCP image. It SHALL come from one of three sources:

- a package index;
- a directory under the project's `extensions/` folder, whose version is the project commit;
- a tag or commit of a separate git repository reached over HTTPS.

Nothing from an extension that comes from an index or a git source SHALL be vendored into the project; an in-repo extension is the project's own code.

The extension's own dependencies SHALL be scoped per component through its standard extras:

- the extra `platform` is installed in the Airflow image;
- the extra `mcp` is installed in the MCP image;
- the extra `cli` is installed in the CLI's environment.

Each extra is installed only where the extension provides it. The extension's base dependencies are installed in all three.

Every third-party package, including the dependencies of in-repo and git-sourced extensions, SHALL come from the package index: public PyPI, or the private index when `[packages] private_index` is set.

The credential for a private package index SHALL come from the secrets store. The credential for a private git repository SHALL be held only in a laptop's `.env` and in CI. It SHALL never be held on a deployed host or in an environment's secrets store.

#### Scenario: Same version everywhere

- **WHEN** an extension is pinned in the project's requirements
- **THEN** the laptop, CI, the Airflow image and the MCP image install that same version

#### Scenario: Same commit everywhere

- **WHEN** an extension from a git source is recorded at commit `3f2c…e1`
- **THEN** the laptop, CI, the Airflow image and the MCP image install the extension built from that commit, whose `source_sha256` is the one recorded

#### Scenario: Heavy dependency for the DAG only

- **WHEN** an extension declares `lightgbm` in its `platform` extra and nothing in `mcp` or `cli`, and `vecten lock` runs
- **THEN** `requirements-airflow.lock` pins `lightgbm`, and neither `requirements-mcp.lock` nor `requirements.lock` holds it

#### Scenario: Extension without extras

- **WHEN** an extension declares no `platform`, `mcp` or `cli` extra
- **THEN** its dependencies are pinned in every enabled component's lock, as before

#### Scenario: Dependencies of a git source come from the index

- **WHEN** an extension from a private git repository depends on `httpx`, and no private index is set
- **THEN** `httpx` is resolved from PyPI, and the git host is asked for nothing but the extension's own repository

#### Scenario: Update

- **WHEN** the extension's version, or its git commit, is changed in the requirements
- **THEN** no file under the project changes other than the requirements, the lockfiles and `extensions.lock.json`

## ADDED Requirements

### Requirement: A git source is pinned to a commit

`vecten extension add` SHALL accept a requirement of the form `<name>[extras] @ git+https://<host>/<path>@<ref>`, with an optional `#subdirectory=<dir>`.

The ref SHALL name a tag or a full commit hash. A tag name MAY contain `/`, as in `acme-resolution/v1.2.0`. The command SHALL resolve the ref against the repository and SHALL record the commit in these places:

- `requirements.txt`, holding the source with the full commit in place of the ref, followed by a comment that names the tag;
- every lock, holding the same line;
- `extensions.lock.json`, recording the URL, the commit, the subdirectory, the tag and the source's `source_sha256`.

`vecten.toml` SHALL list only the name and extras.

The command SHALL refuse:

- a branch;
- an abbreviated hash;
- a name that is both a tag and a branch;
- a missing ref;
- a scheme other than `git+https`;
- a URL carrying user information or a query;
- a fragment other than `subdirectory`;
- a subdirectory that is absolute or leaves the repository;
- a distribution whose name differs from the name given;
- a tag of the form `[<prefix>/]v<version>` whose version differs from the version at that commit.

`requirements.txt` is the source of truth for the commit. `vecten lock` and every re-lock SHALL keep the commit it records and SHALL never move that commit to where a tag points now. `extension add` with that extension's source is the way to change the commit. When a person edits the commit in `requirements.txt` by hand, `vecten lock` SHALL adopt it only if the tag named in the line's comment points at that commit and names the package's version at that commit; otherwise it SHALL refuse, naming `vecten extension add`, and change no file. A commit recorded without a tag comment is adopted as named.

The only lines that may carry a URL in `requirements.txt` and in the locks SHALL be lines for extensions registered in `vecten.toml`. Each such line SHALL carry a full commit and SHALL agree with `extensions.lock.json`. `up`, `ship` and the image build SHALL refuse records that disagree, naming `vecten extension add`.

#### Scenario: Add from a prefixed tag in a monorepo

- **WHEN** `vecten extension add 'acme-resolution @ git+https://github.com/acme/extensions@acme-resolution/v1.2.0#subdirectory=extensions/acme-resolution'` runs, the tag points at commit `3f2c…e1`, and the package's static version is `1.2.0`
- **THEN** `vecten.toml` lists `acme-resolution`
- **AND** `requirements.txt` and every lock hold `acme-resolution @ git+https://github.com/acme/extensions@3f2c…e1#subdirectory=extensions/acme-resolution`, with the comment `# tag acme-resolution/v1.2.0`
- **AND** `extensions.lock.json` records the same commit, subdirectory and tag

#### Scenario: Tag and version disagree

- **WHEN** the tag `acme-resolution/v1.3.0` points at a commit whose `pyproject.toml` says version `1.2.9`
- **THEN** the command exits non-zero naming both versions, and no file under the project changes

#### Scenario: Branch refused

- **WHEN** the ref is `main` and `main` is a branch
- **THEN** the command exits non-zero, says that a branch does not pin a version and asks for a tag or a commit, and no file under the project changes

#### Scenario: Moved tag does not change installs

- **WHEN** a recorded tag is moved to another commit, and `vecten lock` then runs for an unrelated change
- **THEN** the git extension's records still hold the recorded commit

#### Scenario: Commit edited by hand

- **WHEN** a person replaces the recorded commit in `requirements.txt` and its comment names a tag that points at a different commit
- **THEN** `vecten lock` exits non-zero naming the tag, both commits and `vecten extension add`, and no file under the project changes

#### Scenario: Upgrade to a new tag

- **WHEN** `vecten extension add` names the same extension at a newer tag
- **THEN** `requirements.txt`, the locks and `extensions.lock.json` hold the new commit and its `source_sha256`, `vecten.toml` does not change, and the output names the previous and new commit

#### Scenario: Bare name for a git-sourced extension

- **WHEN** `vecten extension add acme-resolution` runs while `acme-resolution` comes from a git source
- **THEN** the command exits non-zero, saying to name a tag, or to name an index version to switch the source

#### Scenario: Credentials in the URL

- **WHEN** the requirement's URL carries `x:secret@` before the host
- **THEN** the command exits non-zero, saying that credentials belong in `GIT_CREDENTIALS`
- **AND** neither the URL nor `secret` appears in the output

### Requirement: The git credential stays on the laptop and in CI

On a laptop, the credential for git repositories SHALL be `GIT_CREDENTIALS` in `.env`. In CI, it SHALL be the `GIT_CREDENTIALS` variable in the environment of the step that runs `vecten`, and in no other step.

Its value SHALL be one or more whitespace-separated entries `https://<username>:<token>@<host>[/<path>]`. An entry with a path SHALL apply only to repositories under that path.

`vecten secret set` SHALL refuse `GIT_CREDENTIALS` and `MCP_GIT_CREDENTIALS` with `--env`.

The credential SHALL reach git only through a credential helper that answers from a file that exists for the fetch alone. While git runs:

- the user's and the system's git configuration SHALL be ignored;
- interactive prompts SHALL be turned off;
- git's trace output SHALL be turned off.

The credential SHALL be deleted before any build backend runs. It SHALL never be written into any of these:

- `vecten.toml`, `requirements.txt`, a lock or `extensions.lock.json`;
- a URL or a command argument;
- a bundle, a build argument or an image layer;
- a deployed host or an environment's secrets store.

A repository that git can read without a credential SHALL need none.

When `GITHUB_ACTIONS` is `true`, `vecten` SHALL register each token found in the value with GitHub's log masking before it runs git.

#### Scenario: Private repository on a laptop

- **WHEN** `GIT_CREDENTIALS` in `.env` holds a token for `github.com/acme`, and `vecten extension add` names a tag of `github.com/acme/extensions`
- **THEN** the extension is recorded and installed
- **AND** the token appears in no process argument, no record, no output and no build backend's environment

#### Scenario: Cloud secret refused

- **WHEN** `vecten secret set GIT_CREDENTIALS --env prod` runs
- **THEN** the command exits non-zero, saying that the git credential lives only in `.env` and in CI

#### Scenario: Public repository

- **WHEN** no `GIT_CREDENTIALS` is set and the extension's repository is public
- **THEN** the extension is added, built and shipped without a credential

### Requirement: A git source is identified by a hash of its source

The source at the recorded commit SHALL declare, in `pyproject.toml`, read without running any code:

- `[build-system]`;
- a static `[project]` name;
- a static `[project]` version.

`extension add` SHALL resolve `[build-system] requires` to exact versions and record them in `extensions.lock.json`. Every build of that commit SHALL use them as build constraints.

`extension add` and `vecten lock` SHALL record the source's `source_sha256` in `extensions.lock.json`: the sha256 over the sorted list of, for every file in the extension's directory at the commit (the subdirectory when one is given), its path relative to that directory, the sha256 of its bytes and its executable bit, excluding only `.git`. A symbolic link in that directory SHALL be refused. CI, after every fetch, and the image build, before installing, SHALL refuse a source whose `source_sha256` differs from the record.

The extension SHALL be built on the host that installs it, as an in-repo extension is, so an extension that compiles code builds for that host's architecture and needs a compiler from the project's `system` packages. Any build backend SHALL be accepted. The build MAY run code from the repository, as importing the extension does, and SHALL never run with the git credential in reach.

#### Scenario: Compiled extension

- **WHEN** an extension from a git source contains a C extension module and the project's `[dependencies.platform] system` lists a compiler
- **THEN** the platform host builds it for its own architecture, and its other hosts do the same

#### Scenario: Dynamic version refused

- **WHEN** the extension's `pyproject.toml` at the commit lists `version` under `dynamic`
- **THEN** `vecten extension add` exits non-zero, saying that a git source needs a static version

#### Scenario: Source hash mismatch

- **WHEN** the source fetched for a recorded commit has a `source_sha256` that differs from `extensions.lock.json`
- **THEN** the command that fetched it exits non-zero naming the extension, the commit and both hashes, and nothing ships

#### Scenario: Symbolic link refused

- **WHEN** the extension's directory at the commit contains a symbolic link
- **THEN** `vecten extension add` exits non-zero naming the link, and no file under the project changes

### Requirement: Git fetch failures name the cause, never the credential

A failed git fetch SHALL exit non-zero naming the host, the repository path, the ref and one of these causes:

- authentication refused, naming the credential to set;
- repository not found, or not visible to the credential;
- ref not found;
- commit not reachable;
- branch or ambiguous ref;
- network failure.

Any git output that is shown SHALL first have removed from it:

- every token and every entry of the credential;
- any user information in a URL;
- any `Authorization` header value.

#### Scenario: Wrong token

- **WHEN** `GIT_CREDENTIALS` holds an expired token, and `vecten extension add` names a tag of that host
- **THEN** the command exits non-zero naming the host, the repository, `authentication refused` and `GIT_CREDENTIALS`
- **AND** no part of the token appears in the output

#### Scenario: Recorded commit no longer reachable

- **WHEN** CI ships a recorded commit that the git host no longer serves
- **THEN** the ship fails before any host changes, naming the repository, the commit and `vecten extension add`

### Requirement: The development flow for private git extensions is documented

The repository SHALL document, in `docs/extensions-private-git.md`, how a team keeps shared extensions in a private git repository and how a project consumes them. The page SHALL cover:

- repository layout, with one directory per extension;
- packaging: a static version, a pinned build backend, and the project's `system` packages for compiled code;
- releasing: prefixed tags, and tag rules that forbid moving or deleting a tag;
- the requirement form, including subdirectories and tags with a prefix;
- `GIT_CREDENTIALS` on the laptop;
- the `VECTEN_GIT_CREDENTIALS` secret, or a GitHub App, in CI;
- rotating the credential;
- upgrading to a new tag, and `vecten extension sync`;
- what the reviewer of an upgrade pull request checks.

The page SHALL name no private repository.

#### Scenario: Reviewer's checklist

- **WHEN** a reviewer opens the page while reviewing a pull request that moves an extension to a new tag
- **THEN** the page lists what to compare:
  - the tag against the commit;
  - the version;
  - the `source_sha256` change;
  - new secrets and settings in `extensions.lock.json`;
  - the dependency changes in each lock
