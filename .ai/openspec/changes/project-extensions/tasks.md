# Tasks: extensions that live inside the project repository

Lands after #50's locking pull request. Groups are pull requests in order. **[deploy]** marks a step
that needs a real deploy on a maintainer sandbox; everything else runs in the unit, `docker` or
`network` tiers.

## 0. Spike (no merge)

- [x] 0.1 Confirm that uv resolves a requirements input holding `./extensions/<name>` (relative to the input file) and that Vecten can drop that line from the output while keeping the closure.
- [x] 0.2 Confirm that pip, at the version each base image holds, applies constraints to isolated builds (`PIP_CONSTRAINT` or `--build-constraint`); otherwise settle the two-stage `--no-build-isolation` fallback.
- [ ] 0.3 Confirm Compose `build.additional_contexts` with the pinned Compose and buildx plugins, on Docker Desktop and on both clouds' VM images, and a read-write bind mount from that context in a `RUN`.
- [x] 0.4 Confirm that `pip install --no-deps -r <lock>` in both images matches today's result for an index-only project with a full-closure lock.

## 1. In-repo extensions in the records and the CLI (after #50 PR 1)

- [ ] 1.1 Path parsing and validation for `extension add` (design Decision 2): normalisation, `..` refused, exactly `extensions/<dir>/`, no symbolic link on the way or under it, directory equals the normalised distribution name, `[build-system]` and static name and version read with `tomllib`. Messages name the rule.
- [ ] 1.2 `requirements.txt` writes and parses `./extensions/<name>[extras]` lines; `vecten.toml` keeps names and extras.
- [ ] 1.3 `vecten lock`: feed in-repo extensions to uv as inputs, drop their lines from every lock, refuse a lock that pins an in-repo extension's name from an index, record `source.path`, version, extras and `metadata_sha256` in `extensions.lock.json`, and add each `metadata_sha256` to the lock input hashes. Resolve and record each in-repo extension's build requirements.
- [ ] 1.4 CLI install: lock with `--no-deps`, in-repo extensions editable with `--no-deps`; skill links into the project tree keep the machine-local record.
- [ ] 1.5 Unit tests: every scenario of "In-repo extensions are registered only from the project's extensions folder" and "An in-repo extension is recorded by its path…"; code-only edits keep the locks fresh; a `pyproject.toml` edit makes them stale; the dependency-confusion scenario with a fake index.

## 2. Images, Compose and the bundle

- [ ] 2.1 `vecten.stack.images`: when in-repo extensions exist, install the lock with `--no-deps`, then each in-repo extension from the mounted additional context with `--no-deps` and the pinned build constraints, then `pip check`; keep today's Dockerfile for projects without locks or in-repo extensions.
- [ ] 2.2 `vecten.stack.compose`: `additional_contexts: {extensions: ../../extensions}` on each built service, for local and VM modes, only when the project has in-repo extensions.
- [ ] 2.3 `vecten.deploy.bundle`: add `extensions` to `CONTENT`; skip `dist`, `build` and `*.egg-info` under it as well as the existing skipped names; the existing symbolic-link refusal applies.
- [ ] 2.4 Tests, unit tier: byte-identical Compose files, Dockerfiles and bundles for a project without `extensions/`; the generated install step never resolves by name.
- [ ] 2.5 Tests, `docker` tier: a fixture project with an in-repo extension declaring `vecten.dags`, `vecten.dbt_packages`, `vecten.osi`, `vecten.skills` and `vecten.mcp.tools`; `vecten up` runs the DAG, builds the model, merges the document, links the skill and offers the tool; no layer of either image holds the extension's source tree.
- [ ] 2.6 **[deploy]** AWS and Scaleway sandboxes: ship the fixture project; both hosts run the extension at the shipped commit; an arm64 VM builds it from an amd64 runner's bundle; a code-only change ships without `vecten lock`.

## 3. `vecten extension new`

- [ ] 3.1 The built-in minimal skeleton under the CLI package: `pyproject.toml` (hatchling pinned, static version, `requires-python`), a package with one `vecten.skills` entry point and package-data layout for `dags/`, `dbt/`, `osi/` and `skills/`, a test.
- [ ] 3.2 Copy with substitution (design Decision 6): the four placeholders in UTF-8 text and path names, binary files unchanged, skipped names, symbolic links refused, `vecten-template.toml` read and checked and not copied, existing target refused.
- [ ] 3.3 `--template` sources: a local directory; `git+https://…@<ref>[#subdirectory=…]` cloned once into a temporary directory with the person's own git setup; nothing recorded.
- [ ] 3.4 Register through `extension add` unless `--no-add`.
- [ ] 3.5 Tests: every scenario of "extension new"; a git template from a local `git http-backend`; dbt Jinja untouched; no URL, ref or credential in any written file.

## 4. Documentation

- [ ] 4.1 `docs/extensions.md`: section "Extensions in the project" (layout, `add`, `new`, dependencies and locking, package data, reviewing), and "Install and update" no longer says paths are refused.
- [ ] 4.2 `docs/cli.md`: `extension add <path>`, `extension new`. `docs/GLOSSARY.md`: "In-repo extension"; "Bundle" lists `extensions/`.
- [ ] 4.3 Changelog line under `[Unreleased]`.
- [x] 4.4 Move the producer guide and template skeleton into the template repository (outside this repository).
- [ ] 4.5 Agree the final text of "Extensions are pip dependencies everywhere" with #50 and the follow-up, and archive.
