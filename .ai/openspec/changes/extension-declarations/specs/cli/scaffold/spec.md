## MODIFIED Requirements

### Requirement: vecten.toml holds editable project configuration

`vecten.toml` SHALL hold the warehouse, users and size, `components`, `profile` defaulting to `standard`, the chosen cloud once the deploy answers are given, `extensions`, the `wanted` source names from the intake that have not yet been added, the declared connectors (each with its name, `source` and `access.groups` as the connectors spec defines), the project's own declared `secrets`, the `settings` of each extension and of the project with their per-environment overrides, the `dependencies` each component adds, and every defaulted setting, and the CLI SHALL read changes to it on the next invocation without re-rendering. A section or key the release does not know SHALL be refused.

#### Scenario: Edit after init

- **WHEN** a user changes a defaulted setting in `vecten.toml`
- **THEN** the next CLI command uses the changed value
- **AND** no re-render is required

#### Scenario: Profile changed by hand

- **WHEN** a user changes `profile` from `standard` to `minimal` and runs `deploy --env prod --plan-only`
- **THEN** the plan shows one VM where there were two

#### Scenario: Malformed configuration

- **WHEN** `vecten.toml` fails to parse
- **THEN** every command except `init` exits non-zero and names the file and the parse error

#### Scenario: Settings and dependencies by hand

- **WHEN** a user adds `[settings.project]`, `[dependencies.platform]` and a top-level `secrets` list to a freshly initialised project
- **THEN** the next CLI command accepts them, and `vecten lock` is the only step before `up` uses them
