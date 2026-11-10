# Vecten Open: working in this repository

Four Python distributions released together: vecten (the CLI), vecten-platform (ingestion
and modelling runtime), vecten-mcp (the warehouse server) and vecten-shared (runtime
contracts). Python 3.14 and uv.

## Commands

- Setup: `uv sync --locked --all-packages`
- Lint: `uv run ruff check .` and `uv run ruff format --check .`
- Unit tests: `uv run pytest -m "not docker and not network and not terraform and not browser and not snowflake_live" -n auto --dist loadgroup`
- Docker, network, Terraform and browser tiers run in CI; run one locally with its marker.
- After editing a skill under packages/vecten/src/vecten/knowhow/skills/:
  `uv run vecten dev render-plugin --repo .`, then commit the rendered plugin and
  docs/agent-guide.md.

## Rules

- Runtime packages import independently of the CLI and of each other.
- Package versions, image tags and Terraform refs stay consistent with
  packages/vecten-shared/src/vecten_shared/versions.toml; `vecten dev bump-version`
  changes them.
- packages/vecten/src/vecten/deploy/vm/apply.py runs on the VM's system Python 3.9 with
  the standard library only; terraform/scaleway/modules/host/agent/apply.py is its
  byte-identical copy.
- Requirements live in .ai/openspec/specs/ (one folder per capability); a behaviour
  change starts there. The glossary is docs/GLOSSARY.md.
- A code comment states the constraint that makes the code correct, never the change
  history.
- Branch from the open release/<version> branch and open pull requests against it; main
  takes only release/ and hotfix/ merges. CONTRIBUTING.md has the branch and release rules.
- Never commit secrets. The maintainer merges; deployments and publication need explicit
  approval.
