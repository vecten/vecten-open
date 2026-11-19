## ADDED Requirements

### Requirement: extension new

`vecten extension new <name>` SHALL create `extensions/<name>/` from a minimal skeleton packaged with the CLI, or, with `--template <source>`, from a local directory or a `git+https://<host>/<repo>@<ref>` source with an optional `#subdirectory=`. A git template SHALL be read once into a temporary directory with the person's own git configuration and credentials, and nothing about it SHALL be stored. The command SHALL copy the template's files, replacing `__vecten_name__`, `__vecten_package__`, `__vecten_title__` and `__vecten_version__` in UTF-8 text files and in path names and copying other files unchanged, SHALL not copy `.git`, `__pycache__`, `.venv`, `dist`, `build` or `vecten-template.toml`, and SHALL refuse a symbolic link in the template, an existing target directory, a name that is not a valid distribution name, and a template whose `vecten-template.toml` requires a newer Vecten Open. It SHALL then register the new extension as `vecten extension add ./extensions/<name>` does, unless `--no-add` is given.

#### Scenario: From the built-in skeleton

- **WHEN** `vecten extension new acme-portal` runs in a project
- **THEN** `extensions/acme-portal/` holds a package `acme_portal` with a `pyproject.toml` naming `acme-portal` at a static version, and the extension is registered and locked

#### Scenario: From a git template

- **WHEN** `vecten extension new acme-bot --template 'git+https://github.com/example/templates@teams-bot/v1.0.0#subdirectory=templates/teams-bot'` runs for a person whose git setup can read that repository
- **THEN** `extensions/acme-bot/` holds the template with every placeholder replaced, and no file under the project names the template's URL, ref or credential

#### Scenario: dbt files keep their Jinja

- **WHEN** the template holds a dbt model with `{{ ref('orders') }}`
- **THEN** the copied model holds `{{ ref('orders') }}` unchanged

#### Scenario: Target exists

- **WHEN** `extensions/acme-portal/` already exists
- **THEN** the command exits non-zero and changes nothing
