# Thin wrappers over the commands CI runs (.github/workflows/ci.yml); the workflow stays the
# source of truth, so a step changed there is changed here too.

SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

UNIT_MARKERS := not docker and not network and not terraform and not browser and not snowflake_live
VERSIONS_TOML := packages/vecten-shared/src/vecten_shared/versions.toml
WHEELS_DIR ?= $(or $(TMPDIR),/tmp)/vecten-extension-wheels
# One release branch is open at a time; its merge base is what CI's comment lint compares with.
RELEASE_BRANCH ?= $(shell git for-each-ref --format='%(refname:short)' 'refs/remotes/origin/release/*' | head -n 1)
BASE ?= $(shell git merge-base HEAD $(RELEASE_BRANCH))
MARKER ?= docker or network
TESTS ?=

.PHONY: help sync lint format comments wheels versions test e2e terraform browser images plugin preflight hooks

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

sync: ## Install every package and the dev group from the lock
	uv sync --locked --all-packages

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Apply ruff's fixes and formatting
	uv run ruff check --fix .
	uv run ruff format .

comments: ## Comment lint on what this branch adds since BASE (default: merge base with the open release branch)
	@test -n "$(BASE)" || { echo "BASE is empty: fetch origin or pass BASE=<commit>" >&2; exit 2; }
	scratch=$$(mktemp -d); \
	trap 'git worktree remove --force "$$scratch"' EXIT; \
	git worktree add -q --detach --no-checkout "$$scratch" "$(BASE)"; \
	git -C "$$scratch" read-tree --reset -u HEAD; \
	(cd "$$scratch" && "$(CURDIR)/.venv/bin/windbag" check --staged)

wheels: ## Extension contracts and edition boundary in the release wheels
	rm -rf "$(WHEELS_DIR)"
	uv build --all-packages --wheel --out-dir "$(WHEELS_DIR)"
	uv build packages/vecten/tests/fixtures/extension --wheel --out-dir "$(WHEELS_DIR)"
	uv run python scripts/check_extension_wheels.py "$(WHEELS_DIR)"

versions: ## Versions table agrees with the image build args
	expected=$$(uv run python -c "import tomllib;print(tomllib.load(open('$(VERSIONS_TOML)','rb'))['images']['airflow'])"); \
	tag=$${expected##*:}; version=$${tag%%-*}; \
	args=$$(uv run vecten dev build-images --print --json); \
	grep -qF "AIRFLOW_IMAGE=$${expected}\"" <<<"$$args"; \
	grep -qF "AIRFLOW_VERSION=$${version}\"" <<<"$$args"

test: ## Unit tests (no services needed)
	uv run pytest -m "$(UNIT_MARKERS)" -q -n auto --dist loadgroup

e2e: ## Docker and network tests (make e2e TESTS=<paths> MARKER=docker)
	uv run pytest -m "$(MARKER)" -q -x --timeout 2400 --durations=20 $(TESTS)

terraform: ## Terraform module tests (needs terraform from .terraform-version)
	uv run pytest packages/vecten/tests/test_terraform_modules.py -m terraform -q -n 4 --durations=20

browser: ## Playwright auth tests (needs: uv run --group browser playwright install chromium webkit)
	uv run --group browser pytest packages/vecten-mcp/tests/test_oauth_browser.py packages/vecten/tests/test_eval_harness_person_tools.py -q

images: ## Build the base images locally
	uv run vecten dev build-images

plugin: ## Render the plugin and docs/agent-guide.md from the skills
	uv run vecten dev render-plugin --repo .

hooks: ## Enable the repository's pre-commit hook in this clone
	git config --local core.hooksPath .githooks

preflight: ## CI's lint and unit jobs, before a push: one line per step, failing logs, a summary
	@uv run --no-project python scripts/preflight.py
