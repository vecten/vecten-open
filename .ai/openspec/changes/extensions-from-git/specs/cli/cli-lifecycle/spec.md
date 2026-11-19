## ADDED Requirements

### Requirement: extension sync

`vecten extension sync` SHALL install the project's committed `requirements.lock` into the Python environment that runs Vecten, and SHALL refresh the extensions' skill links.

The lock SHALL be installed with `--no-deps`. In-repo extensions SHALL be installed from their directories, as `vecten extension add` installs them. Git-sourced extensions SHALL be fetched through the same path as `extension add`, their `source_sha256` verified against `extensions.lock.json`, and installed from the fetched copy, not editable.

The git credential SHALL be taken from `GIT_CREDENTIALS` in `.env`, or from the process environment when `.env` holds none.

The command SHALL change no record. It SHALL refuse, naming `vecten lock`, when the records disagree.

#### Scenario: CI installs the project's extensions

- **WHEN** a CI job with `GIT_CREDENTIALS` in its environment runs `vecten extension sync` in a project with an extension from a private git tag
- **THEN** the environment holds the extension built from the recorded commit, whose source hash is the one recorded, and the job log holds no part of the token

#### Scenario: Stale records

- **WHEN** `vecten.toml` lists an extension that `requirements.lock` does not hold
- **THEN** the command exits non-zero naming `vecten lock`, and installs nothing
