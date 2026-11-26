# Tasks: extensions that live inside the project repository

Lands after #50's locking pull request. Groups are pull requests in order. **[deploy]** marks a step
that needs a real deploy on a maintainer sandbox; everything else runs in the unit, `docker` or
`network` tiers.

## 0. Spike (no merge)

- [x] 0.1 Confirm that uv resolves a requirements input holding `./extensions/<name>` (relative to the input file) and that Vecten can drop that line from the output while keeping the closure.
- [x] 0.2 Confirm that pip, at the version each base image holds, applies constraints to isolated builds (`PIP_CONSTRAINT` or `--build-constraint`); otherwise settle the two-stage `--no-build-isolation` fallback.
- [ ] 0.3 Confirm Compose `build.additional_contexts` with the pinned Compose and buildx plugins, on Docker Desktop and on both clouds' VM images, and a read-write bind mount from that context in a `RUN`.
  - Partly run live for 0.1.1: `additional_contexts` with a `RUN` bind mount from the context builds on Docker Desktop and on the AWS VM image with the pinned Compose and buildx (in-repo extensions ship there). The build mounts read-only and works on a copy (2.7); a read-write mount also builds on Docker Desktop. Scaleway's image not run.
  - Partly run live for 0.1.2 (rc1 AWS, rc4 Scaleway): both clouds' VM images built an in-repo extension through `additional_contexts` with the pinned plugins.
- [x] 0.4 Confirm that `pip install --no-deps -r <lock>` in both images matches today's result for an index-only project with a full-closure lock.

## 1. In-repo extensions in the records and the CLI (after #50 PR 1)

- [x] 1.1 Path parsing and validation for `extension add` (design Decision 2): normalisation, `..` refused, exactly `extensions/<dir>/`, no symbolic link on the way or under it, directory equals the normalised distribution name, `[build-system]` and static name and version read with `tomllib`. Messages name the rule.
  - Built: `vecten/in_repo.py`; `tests/test_in_repo.py`.
- [x] 1.2 `requirements.txt` writes and parses `./extensions/<name>[extras]` lines; `vecten.toml` keeps names and extras.
  - Built: `extensions.py` writes and parses the lines, `locking.py` lists them; `tests/test_cmd_lock.py`. No test uses extras on an in-repo line.
- [x] 1.3 `vecten lock`: feed in-repo extensions to uv as inputs, drop their lines from every lock, refuse a lock that pins an in-repo extension's name from an index, record `source.path`, version, extras and `metadata_sha256` in `extensions.lock.json`, and add each `metadata_sha256` to the lock input hashes. Resolve and record each in-repo extension's build requirements.
  - Built: `extensions.py` (`_lock_project`, `_refuse_index_pins_of_in_repo_names`, the record fields); `tests/test_cmd_lock.py`.
- [x] 1.4 CLI install: lock with `--no-deps`, in-repo extensions editable with `--no-deps`; skill links into the project tree keep the machine-local record.
  - Built: `install_lock` installs the lock, then each in-repo extension editable with `--no-deps`; `tests/test_install_lock.py`, `tests/test_extension_sync.py`.
- [x] 1.5 Unit tests: every scenario of "In-repo extensions are registered only from the project's extensions folder" and "An in-repo extension is recorded by its path…"; code-only edits keep the locks fresh; a `pyproject.toml` edit makes them stale; the dependency-confusion scenario with a fake index.
  - Built: `tests/test_in_repo.py`, `tests/test_cmd_lock.py`. The dependency-confusion case stubs the resolver rather than serving a fake index.

## 2. Images, Compose and the bundle

- [x] 2.1 `vecten.stack.images`: when in-repo extensions exist, install the lock with `--no-deps`, then each in-repo extension from the mounted additional context with `--no-deps` and the pinned build constraints, then `pip check`; keep today's Dockerfile for projects without locks or in-repo extensions.
  - Built: `stack/images.py` (`write_project_layer`, `_build_sources`); `tests/test_extension_builds.py`.
- [x] 2.2 `vecten.stack.compose`: `additional_contexts: {extensions: ../../extensions}` on each built service, for local and VM modes, only when the project has in-repo extensions.
  - Built: `stack/compose.py`; `tests/test_stack_compose_declarations.py` (VM mode asserted).
- [x] 2.3 `vecten.deploy.bundle`: add `extensions` to `CONTENT`; skip `dist`, `build` and `*.egg-info` under it as well as the existing skipped names; the existing symbolic-link refusal applies.
  - Built: `deploy/bundle.py`; `tests/test_deploy_bundle.py`.
- [x] 2.4 Tests, unit tier: byte-identical Compose files, Dockerfiles and bundles for a project without `extensions/`; the generated install step never resolves by name.
  - Built: release goldens and `test_a_project_without_git_sources_ships_the_bytes_it_shipped_before_them`. The laptop Dockerfiles have no golden.
- [ ] 2.5 Tests, `docker` tier: a fixture project with an in-repo extension declaring `vecten.dags`, `vecten.dbt_packages`, `vecten.osi`, `vecten.skills` and `vecten.mcp.tools`; `vecten up` runs the DAG, builds the model, merges the document, links the skill and offers the tool; no layer of either image holds the extension's source tree.
  - Partly built: `tests/test_in_repo_images_docker.py` builds the images and checks the entry points; no `docker`-tier `vecten up` of the fixture runs the DAG, builds the model, merges the document, links the skill and offers the tool.
- [ ] 2.6 **[deploy]** AWS and Scaleway sandboxes: ship the fixture project; both hosts run the extension at the shipped commit; an arm64 VM builds it from an amd64 runner's bundle; a code-only change ships without `vecten lock`.
  - Partly run live for 0.1.1 (rc2, AWS): both hosts run the in-repo extension at the shipped commit, and a code-only change shipped without `vecten lock`. arm64 (every VM was x86_64) and Scaleway not run.
  - Partly run live for 0.1.2 (rc1 AWS, rc4 Scaleway): both hosts ran the extension at the shipped commit, and on Scaleway a code-only change shipped without `vecten lock` and was served. The arm64 build not run.
- [x] 2.7 Build each source extension (in-repo or git) with its own install and its own `build_requires` file, never one constraints file for all; build from a copy the image user owns, so setuptools or maturin can write into the tree. `docker` tier: two extensions pinning different hatchling versions and a setuptools one build in one image.

## 3. `vecten extension new`

- [ ] 3.1 The built-in minimal skeleton under the CLI package: `pyproject.toml` (hatchling pinned, static version, `requires-python`), a package with one `vecten.skills` entry point and package-data layout for `dags/`, `dbt/`, `osi/` and `skills/`, a test.
  - Partly built: `extension_skeleton/` has the pinned `pyproject.toml`, the package and its `vecten.skills` entry point, but no `dags/`, `dbt/` or `osi/` layout.
- [x] 3.2 Copy with substitution (design Decision 6): the four placeholders in UTF-8 text and path names, binary files unchanged, skipped names, symbolic links refused, `vecten-template.toml` read and checked and not copied, existing target refused.
  - Built: `extension_new.py` (`_placeholders`, `_copy`, `_check_template`); `tests/test_extension_new.py`.
- [x] 3.3 `--template` sources: a local directory; `git+https://…@<ref>[#subdirectory=…]` cloned once into a temporary directory with the person's own git setup; nothing recorded.
  - Built: `extension_new.py` (`_git_source`, `_clone` into a temporary directory); the tests stub `_clone`.
- [x] 3.4 Register through `extension add` unless `--no-add`.
  - Built: `extension_new.py` registers through `add_extension`, `--no-add` on the command; `tests/test_extension_new.py`.
- [ ] 3.5 Tests: every scenario of "extension new"; a git template from a local `git http-backend`; dbt Jinja untouched; no URL, ref or credential in any written file.
  - Partly built: every scenario except a git template served by a local `git http-backend`; the git tests stub `_clone`.

## 4. Documentation

- [x] 4.1 `docs/extensions.md`: section "Extensions in the project" (layout, `add`, `new`, dependencies and locking, package data, reviewing), and "Install and update" no longer says paths are refused.
  - Built: `docs/extensions.md`, "Extensions in the project".
- [x] 4.2 `docs/cli.md`: `extension add <path>`, `extension new`. `docs/GLOSSARY.md`: "In-repo extension"; "Bundle" lists `extensions/`.
  - Built: `docs/cli.md`; `docs/GLOSSARY.md` "In-repo extension" and "Bundle".
- [x] 4.3 Changelog line under `[Unreleased]`.
  - Built: `CHANGELOG.md` under `[Unreleased]`.
- [x] 4.4 Move the producer guide and template skeleton into the template repository (outside this repository).
- [ ] 4.5 Agree the final text of "Extensions are pip dependencies everywhere" with #50 and the follow-up, and archive.
