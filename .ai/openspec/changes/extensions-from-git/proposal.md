# Install extensions from a git tag in a separate private repository

Issue: [#35](https://github.com/vecten/vecten-open/issues/35). Follow-up to the change
`project-extensions`, which lands first; lower priority.

## Why

`project-extensions` lets a project keep its own extensions in its repository. A team that shares
extensions across several projects keeps them in one separate private repository instead, and today
can install them only through a private package index. That index replaces PyPI, so it must proxy
every public dependency, and its credential sits on every VM. The team should be able to install a tag
of its own repository directly, with the same commit everywhere and no credential for private code on
any host.

## What changes

- `vecten extension add` accepts `'<name>[extras] @ git+https://<host>/<repo>@<tag-or-commit>[#subdirectory=<dir>]'`.
  A branch, an abbreviated hash, a missing ref and any scheme other than `git+https` are refused; tags
  may carry a prefix (`<extension>/v1.2.0`) for repositories holding several extensions.
- The tag is resolved to its commit once. `requirements.txt` holds the line with the full commit and a
  `# tag <tag>` comment; `extensions.lock.json` mirrors the commit and records a hash of the source at
  that commit (`source_sha256`) and the pinned build requirements. `vecten lock` writes both, and the record checks refuse
  a mismatch. `vecten.toml` keeps names and extras.
- The git credential lives only in a laptop's `.env` (`GIT_CREDENTIALS`) and in CI (the
  `VECTEN_GIT_CREDENTIALS` secret, or a GitHub App token minted per job). Never on a VM, never in an
  environment's secrets store. Vecten fetches the commit itself into a throwaway copy, deletes the
  credential, then builds with none in reach.
- `vecten ship`, in CI, fetches each git-sourced extension's recorded commit, checks its `source_sha256`
  against `extensions.lock.json` and puts the source in every host bundle at `extensions/<name>/`. The
  host builds it as it builds an in-repo extension, after verifying the hash: no git, no fetch of private
  code, no credential, no change to the VM agent. Compiled extensions build on the host like in-repo
  ones.
- `vecten extension sync` installs the committed CLI lock on another laptop or in CI.
- `docs/extensions-private-git.md` covers repository layout, releasing and protecting tags, credentials
  and their rotation, adopting a new tag, and reviewing an upgrade.

## Capabilities

- **Modified** `shared/extension-points`: "Extensions are pip dependencies everywhere" (written over
  `project-extensions`' text) names the git source and where its credential may live; new requirements
  for pinning to a commit, the credential, the verified source, failures and the documentation.
- **Added to** `shared/deploy`: bundled sources; the rendered workflows hand the credential only to the
  `vecten` step.
- **Added to** `cli/cli-lifecycle`: `extension sync`.

## Impact

- Code: new `vecten_shared.git_sources` (ref resolution, fetch, credential helper, classified errors, `source_sha256`); `vecten.extensions` and
  `vecten lock` (records); `vecten.stack.images` (the hash check before the in-repo install step); `vecten.deploy.bundle` and
  `vecten.stack.up` (fetched sources into `extensions/`); the rendered `deploy-platform.yml`, `ship.yml` and
  `terraform-apply.yml`; `vecten secret set` (refuses the git credential with `--env`).
- Not changed: the VM agent and its Terraform copy, `host.json`, Compose build secrets, the base images,
  the cloud secrets stores.
- Docs: `docs/extensions.md`, new `docs/extensions-private-git.md`, `docs/cli.md`, `docs/GLOSSARY.md`,
  the changelog.
