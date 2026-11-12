# Tasks: install extensions from a git tag in a separate private repository

Lands after #50's locking pull request and after `project-extensions`. Groups 1 and 2 may land in
parallel with either. **[private repo]** needs a real private GitHub repository and a maintainer's
token (a new maintainers-only marker, like `snowflake_live`); **[deploy]** needs a maintainer sandbox.

## 0. Spike (no merge)

- [ ] 0.1 `uv pip compile` writes the full commit for `@<tag>`, with `--universal` and with `--python-platform linux`.
- [ ] 0.2 uv fetches through `git` and honours `url.file://<copy>.insteadOf=…` passed through `GIT_CONFIG_*` with `GIT_CONFIG_GLOBAL=/dev/null`; the lock keeps the HTTPS URL. Whether uv's GitHub fast path bypasses the copy.
- [ ] 0.3 `git fetch --depth 1 <url> <commit>` on GitHub and on a local `git http-backend`.
- [ ] 0.4 Record results in design.md; switch Decision 2 to its laptop fallback if 0.2 fails.

## 1. `vecten_shared.git_sources`

- [ ] 1.1 Parse and validate a git source (scheme, no user information, query or foreign fragment, relative subdirectory, extras, no markers); refusals never echo the URL.
- [ ] 1.2 Parse `GIT_CREDENTIALS` entries; write a mode-0600 credential-store file; `credential.useHttpPath` when an entry has a path.
- [ ] 1.3 The isolated git environment (Decision 2).
- [ ] 1.4 Ref resolution with prefixed tags (Decision 7).
- [ ] 1.5 Fetch a commit into a throwaway bare repository; tag fallback; "commit not reachable".
- [ ] 1.6 Classified failures and masking (Decision 11); `::add-mask::` per token under `GITHUB_ACTIONS=true`.
- [ ] 1.7 Tests, unit tier, against a local `git http-backend` with basic authentication: tag, commit, prefixed tag, branch, ambiguous ref, wrong token (no substring of length ≥ 4 in output), missing commit, public repository, a user helper in a fake global config left unused, `GIT_TRACE_CURL` in the parent environment leaking nothing.
- [ ] 1.8 Test, `network` tier: a public GitHub repository by tag, by commit and with a subdirectory.
- [ ] 1.9 Test, **[private repo]**: fine-grained token; revoked token; GitHub App installation token.

## 2. Source identity

- [ ] 2.1 Check out the extension's directory from the fetched commit into a fetched copy; refuse symbolic links; read static name and version and `[build-system]` with `tomllib`.
- [ ] 2.2 `source_sha256` (Decision 4) in `vecten_shared.git_sources`, and `python -m vecten_shared.git_sources verify <dir> <extensions.lock.json>` for the image build.
- [ ] 2.3 Tests: the hash is unchanged by file order and modification times, changed by any file's bytes, path or executable bit; a symbolic link is refused; the same commit gives the same hash on macOS and Linux runners.

## 3. Records and `extension add`

- [ ] 3.1 Parse the git form beside index names and in-repo paths; `vecten.toml` keeps names and extras.
- [ ] 3.2 Resolve, fetch, check the tag against the version, record `source_sha256` and build pins; inspect entry points from the copy; refuse a different distribution name.
- [ ] 3.3 `requirements.txt` lines with the full commit and `# tag` comment; carry git lines over verbatim on other changes.
- [ ] 3.4 Upgrade semantics (Decision 8), `--json` with `source`, `source_sha256` and `previous`.
- [ ] 3.5 `vecten lock` and the record checks: accept a URL line only for a registered extension, `git+https`, full commit, no user information, identical in `requirements.txt` and every lock, agreeing with `extensions.lock.json`; never re-resolve a tag.
- [ ] 3.6 `vecten secret set` refuses `GIT_CREDENTIALS` and `MCP_GIT_CREDENTIALS` with `--env`.
- [ ] 3.7 Tests, unit tier: every scenario of the extension-points delta against the local `git http-backend`.

## 4. Ship, images and workflows

- [ ] 4.1 `vecten.deploy.bundle`: fetch each git source in CI, check `source_sha256`, write the source to `extensions/<name>/` in every host bundle beside in-repo extensions; refuse a mismatch before any host changes. `vecten.stack.up`: the same from a fetched copy under `.vecten/`, never in the project tree.
- [ ] 4.2 `vecten.stack.images`: verify each git-sourced directory's `source_sha256` against the bundled `extensions.lock.json`, then install it through `project-extensions`' in-repo step; unchanged for projects without git sources.
- [ ] 4.3 Rendered `deploy-platform.yml`, `ship.yml`, `terraform-apply.yml`: optional secrets passed by name, the App token step, `GIT_CREDENTIALS` only on the `vecten ship` step; `actions/create-github-app-token` pinned in the versions table.
- [ ] 4.4 Laptop dispatch checks (Decision 13).
- [ ] 4.5 Tests: rendered workflows (only the `vecten` step gets the credential); byte-identical bundles for projects without git sources; `docker` tier: both images from a bundled git source, no layer holds the token or the fetched source tree, a tampered source fails the build naming both hashes.
- [ ] 4.6 **[private repo]** `extension add` from a private monorepo with two extensions under subdirectories and prefixed tags, then `vecten up`.
- [ ] 4.7 **[deploy]** AWS and Scaleway sandboxes: ship with an App token and with `VECTEN_GIT_CREDENTIALS`; no VM secret store, host file or container holds a git credential; an arm64 VM builds the source an amd64 runner bundled; a promotion of the same commit fetches again with the CI credential; a revoked token fails the ship in CI before any host changes.

## 5. `vecten extension sync`

- [ ] 5.1 The command (Decision 14) and its tests, including in-repo extensions.

## 6. Documentation and archive

- [ ] 6.1 `docs/extensions-private-git.md` (Decision 16); `docs/extensions.md` links it.
- [ ] 6.2 `docs/cli.md`, `docs/GLOSSARY.md` ("Git source"), changelog.
- [ ] 6.3 Agree the final text of "Extensions are pip dependencies everywhere" with `project-extensions`, and archive.
