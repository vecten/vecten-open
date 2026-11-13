## ADDED Requirements

### Requirement: Git-sourced extensions ship as verified source in the bundle

`vecten ship`, run in CI, SHALL fetch the recorded commit of every git-sourced extension the records hold, as the `shared/extension-points` spec states, and SHALL refuse, before any host changes, a fetched source whose `source_sha256` differs from `extensions.lock.json`.

The ship SHALL place each git-sourced extension's source in every host bundle at `extensions/<name>/`, beside the in-repo extensions, together with `extensions.lock.json`.

The generated project-layer Dockerfile SHALL verify each git-sourced directory's `source_sha256` against `extensions.lock.json`, then install it exactly as it installs an in-repo extension: from the bundle's `extensions/` context, with `--no-deps` and the pinned build constraints, after the lock is installed with `--no-deps`, followed by `pip check`.

No deployed host SHALL fetch private code, hold a git credential or need git to build the layer. The VM agent, `host.json` and the base images SHALL be unchanged by git sources. `vecten up` SHALL build the local layer through the same Dockerfile path, from a fetched copy kept under `.vecten/` and never written into the project's tracked files.

#### Scenario: VM without a credential

- **WHEN** a project with an extension from a private git tag ships to an environment
- **THEN** each VM builds its project layer from the bundled source and starts the profile
- **AND** no secret store, host file or container on the VM holds a git credential

#### Scenario: Bundled on amd64, built on arm64

- **WHEN** the ship runs on an amd64 runner and the platform VM is arm64
- **THEN** the VM builds the bundled source for arm64, and the layer's other packages come from the index for arm64

#### Scenario: Tampered source

- **WHEN** a bundled git-sourced directory's files differ from the `source_sha256` in `extensions.lock.json`
- **THEN** the VM's layer build fails naming the extension, the commit and both hashes, and the host keeps its previous bundle

#### Scenario: Index-only projects unchanged

- **WHEN** a project whose extensions all come from an index ships
- **THEN** its bundle, Dockerfiles and Compose file are the same as before this change

#### Scenario: A promotion fetches again

- **WHEN** a commit already shipped to `staging` is promoted to `prod`
- **THEN** the ship fetches each git-sourced extension's recorded commit again with the CI credential and checks its `source_sha256`

### Requirement: The rendered workflows hand the git credential only to the vecten step

The rendered workflow that ships SHALL accept two optional secrets, `VECTEN_GIT_CREDENTIALS` and `VECTEN_GIT_APP_PRIVATE_KEY`, and its callers SHALL pass them by name.

When the variable `VECTEN_GIT_APP_ID` is set, a step SHALL mint a GitHub App installation token for the owner and repositories that `VECTEN_GIT_APP_OWNER` and `VECTEN_GIT_APP_REPOSITORIES` name. That step SHALL use an action pinned by commit in the versions table.

Only the step that runs `vecten ship` SHALL receive `GIT_CREDENTIALS`. Its value SHALL be the minted token in the credential format when a token was minted, and `VECTEN_GIT_CREDENTIALS` otherwise.

A `vecten ship` or `vecten deploy` run from a laptop, for a project with a git source whose record is not marked `public`, SHALL refuse before dispatching when neither `VECTEN_GIT_CREDENTIALS` nor `VECTEN_GIT_APP_ID` is configured for the repository or the environment. It SHALL check this by name only, and SHALL name the secret or variable to set. `extension add` SHALL mark a source `public` only when it resolved the ref without any credential.

#### Scenario: Ship with a GitHub App

- **WHEN** `VECTEN_GIT_APP_ID`, `VECTEN_GIT_APP_OWNER` and `VECTEN_GIT_APP_PRIVATE_KEY` are set and the ship workflow runs
- **THEN** a token is minted for that job only, the `vecten ship` step alone receives it, and the job log holds no part of the token or the key

#### Scenario: Laptop dispatch without a CI credential

- **WHEN** `vecten ship --env prod` runs on a laptop for a project with an extension from a private tag, and the repository has neither `VECTEN_GIT_CREDENTIALS` nor `VECTEN_GIT_APP_ID`
- **THEN** the command exits non-zero naming both, and no workflow is dispatched
