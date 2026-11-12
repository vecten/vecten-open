## Context

How code reaches an environment today, as checked on `release/0.1.1`:

- **Push.** The rendered `ship.yml` runs on every push to `main`. Its `gate` job runs `vecten ship --env prod --check`, which reads `[environments.prod]` in the pushed `vecten.toml`. Its `ship` job calls `deploy-platform.yml` with `environment: prod`. Both values are literals in the template (`scaffold/template/.github/workflows/ship.yml.jinja`).
- **Dispatch.** `deploy-platform.yml` and `terraform-apply.yml` take only `environment`. From the laptop, `vecten ship --env <env>` and `vecten deploy --env <env>` call `vecten.deploy.github_env.dispatch_and_follow`, which runs `gh workflow run <workflow> --ref <default branch>`. The run checks out the default branch's head, and the result's `matches_local_head` says whether that is what the laptop holds.
- **Branch restriction.** `vecten.deploy.github_env.ensure_environment` creates each GitHub environment deployable from the default branch only, where the plan allows it. `vecten.deploy.guards.require_on_default_branch` refuses a laptop deploy whose commit is not on `origin/<default branch>`.
- **What an environment runs.** `vecten.toml` records `deployed_commit` per environment, but only for `deploy` and only from the laptop: CI never writes `vecten.toml`. A ship records nothing a later job can read. Scaleway hosts keep a pointer object per host and AWS hosts none, and the dbt DAG writes `platform/dbt/built_commit` in the environment's bucket, which another environment's CI identity cannot read.
- **`next`.** `vecten.next.assemble` holds `ENV = "prod"` and walks only `prod`'s deploy prerequisites, deploy gate, post-deploy items and key renewals.
- **`issues`.** `vecten issues --env <env>` already reads any environment. The `operate` skill names `--env prod` everywhere.
- **Facts and content in one file.** `ship_env` reads `vecten.toml` from the checked-out tree for both the gating facts (`require_deployed`, `identity_pending`) and the configuration the bundle carries.

GitHub limits that shape the options:

- `workflow_dispatch` runs a workflow on a branch or a tag, never on a bare commit, and the run's `github.sha` is that ref's head.
- GitHub records a deployment for every job that declares `environment:`, with `github.sha` as its commit. A run dispatched on `main` that checks out an older commit therefore records `main`'s head, not what it shipped.
- Per GitHub's documentation, required reviewers on an environment are available for public repositories, and for private ones only on GitHub Enterprise. Branch policies on a private repository need a paid plan, and `ensure_environment` already handles the plan that lacks them.

## Goals / Non-Goals

**Goals:**

- A declared order of environments; a push ships the first one.
- A later environment ships the exact commit an earlier one shipped, on every GitHub plan.
- Production never runs a commit that review did not see or that the environment before it did not ship.
- `next`, `issues` and the `operate` skill know every environment in the order.
- Today's behaviour without an order, and no new step for existing projects.

**Non-Goals:**

- A branch per environment (Decision 7).
- Promoting automatically, or on a test result in the earlier environment. The gate is "shipped there"; a human decides when.
- Several orders, or a tree of environments. The order is one list.
- Promotion of Terraform state or data between environments. Only code and configuration move.

## Decisions

Every decision below was accepted by the maintainer on 2026-11-12, as recommended, with one rule added to Decision 3: `prod` is the last environment of the order. They are ordered with the hardest to reverse first.

### Decision 1: How a later environment ships the same commit (Accepted)

| Option | Consequences |
|---|---|
| **A. Dispatch on the default branch with a `commit` input** | `deploy-platform.yml` and `terraform-apply.yml` gain a `commit` input. The job runs from the default branch, so the environment's branch restriction and the OIDC trust hold unchanged, and checks out `commit`. Before shipping, the CLI verifies in CI that the commit is on the default branch and that the environment before this one shipped it (Decision 2). Works on every plan. A human who wants an approval step can add required reviewers to the later environment in GitHub where the plan offers them; the dispatched job then waits for them. |
| B. A tag per promotion (`promote/prod/<n>`), dispatched on the tag | The workflow file and the code come from the same commit. But every environment's branch policy must also admit the tag pattern, anyone with write access can tag any commit, a branch commit included, and the ancestry check is needed anyway. Tags also pile up. |
| C. One push run with a second job for the next environment, held by required reviewers | The most native: the same run, the same `github.sha`, and approval in GitHub's interface. But it needs required reviewers, which a private repository has only on GitHub Enterprise. Every push leaves a waiting job, and an approval days later ships a commit older than one already merged. |

**Accepted: A.** C remains possible on top of A: required reviewers on the later environment hold a dispatched promotion just as they would hold C's job.

### Decision 2: The record of what an environment shipped (Accepted)

A promotion must answer two questions: what the environment before this one runs, and whether a given commit was shipped there. CI must be able to answer the second for itself, with the later environment's identity.

| Option | Consequences |
|---|---|
| **A. A commit status, `vecten/ship/<env>`, set by every ship in CI** | `pending` when the ship starts, then `success` or `failure`. Any job can read it through the GitHub API with its own token (`statuses: read`); setting it needs `statuses: write` on the job that ships. Statuses show on the commit in GitHub's interface. Anyone who can write statuses could forge one; such a person can already push to the default branch and dispatch deploys, so the status guards against mistakes, not against people with write access. |
| B. GitHub's deployment records | Correct for a push-triggered first environment. Wrong for every promoted run, which records the default branch's head (Context), so a third environment could not trust the second's record. |
| C. A marker in the environment's bucket | Exact, but the later environment's CI identity cannot read the earlier environment's bucket, and should not. |

**Accepted: A.** "What the environment before this one runs" is the commit, among the default branch's last 200, whose `vecten/ship/<env>` `success` status was set last (its `createdAt`), not the newest such commit in history: a promotion with `--commit` ships an older commit after a newer one. After a failed ship rolls back, that commit's status is `failure`, so the answer is the commit the hosts went back to. (Refined on 2026-11-12 at review.)

### Decision 3: How the order is declared (Accepted)

| Option | Consequences |
|---|---|
| **A. `[deploy] promotion = ["dev", "prod"]` in `vecten.toml`** | One list beside the other deploy answers. It reaches CI in the pushed commit, so the workflows read it when they run. Rules: at least two environments, no repeats, each with a tfvars file, and `prod` last, so no environment promotes from production. A chain of any length works (`["dev", "staging", "prod"]`). Environments outside the list ship by dispatch, as today. Without the key the order is `["prod"]`. |
| B. `promotes_from = "dev"` in each later environment's tfvars | The order is spread over files, cycles and forks must be refused, and a push needs to find the head by reading every file. |
| C. Rendered into `ship.yml` at `init --deploy` | Changing the order needs a re-render, and the workflow and `vecten.toml` can disagree. |

**Accepted: A.**

### Decision 4: What a push ships, and the rendered `ship.yml` (Accepted)

| Option | Consequences |
|---|---|
| **A. The first environment of the order, read when the workflow runs** | The `gate` job runs `vecten ship --check` without `--env`. It writes the first environment's name with the `bootstrapped` and `deployed` facts to the job's outputs. The `ship` job passes `environment: ${{ needs.gate.outputs.env }}` and `commit: ${{ github.sha }}` to `deploy-platform.yml` and grants `statuses: write`. With no order the first environment is `prod`, so the behaviour is today's. |
| B. Render one `ship.yml` per order | See Decision 3, C. |

**Accepted: A.** `github push` keeps waiting for the `ship.yml` run that its push starts; that run now ships the first environment.

### Decision 5: What a promoted run reads from the promoted commit, and what from the default branch (Accepted)

`vecten.toml` holds both the project's configuration, which is promoted with the code, and the facts the CLI records about environments, which describe the environments now. A commit made before `prod` was first deployed says that `prod` is not deployed.

| Option | Consequences |
|---|---|
| **A. Everything from the promoted commit except the facts** | The bundle (DAGs, connectors, dbt, `osi/`, `evals/` and `vecten.toml`), the environment's tfvars and the Terraform roots all come from the promoted commit, so what `prod` gets is what `dev` ran, configuration included. The facts under `[environments.<env>]` come from the default branch's head, where the workflow runs. The CI job installs the `vecten` version that the promoted commit's `[project] scaffold_version` names, not the one the default branch's workflow pins, so the bundle is built by the CLI that built it for `dev`. |
| B. Everything from the promoted commit | Facts recorded after that commit are missing, so a promotion can be refused as "not deployed" or skip its hosts as "identity pending". |
| C. Content from the promoted commit, tfvars and Terraform from the default branch | A `prod` tfvars change would reach `prod` without passing through the order. It also breaks when the branch's tfvars name a variable that the promoted commit's root does not declare. |

**Accepted: A.**

### Decision 6: `deploy` to a later environment (Accepted)

| Option | Consequences |
|---|---|
| **A. `deploy --env <later env>` applies and ships the commit `promote` would choose** | A Terraform change also runs first in the environment before. A first deploy of a later environment needs the environment before it to have shipped once. `deployed_commit` records the promoted commit. |
| B. `deploy` keeps using the default branch's head | One `deploy --env prod` would ship unpromoted code and infrastructure to production. |

**Accepted: A.** `--commit` selects an older shipped commit, as with `promote`.

### Decision 7: The branch restriction and the default-branch rule (Accepted)

| Option | Consequences |
|---|---|
| **A. Keep both; promotion moves commits of the default branch** | Every environment still deploys only from the default branch; dispatched promotions run on it, and the CLI refuses a `commit` that is not its ancestor. Review stays the gate for every environment. |
| B. A branch per environment (`dev` deploys from a `dev` branch) | Each environment would need its own branch policy, and `require_on_default_branch` would accept other branches. A merge from `dev` into `main` makes a new commit, so production would not run the commit `dev` ran, and code could reach `dev` without review. |

**Accepted: A.** The `deploy` spec's requirement "deploy refuses a commit that is not on main" stays as it is.

### Decision 8: The command line (Accepted)

| Option | Consequences |
|---|---|
| **A. A new `vecten promote --env <env> [--commit <sha>]`** | It is refused for an environment outside the order or first in it. It resolves the commit (Decision 2), dispatches `deploy-platform.yml` with it, follows the run, and reports the commit and the run. `--commit` ships any commit the environment before it shipped, which is also how production rolls back to a commit it ran earlier. `promote --status` lists each environment of the order with the commit it last shipped. `ship --env <later env>` from the laptop refuses, naming `promote`. Inside Actions, `ship --env <env> --commit <sha>` does the work. |
| B. `ship --env prod` itself promotes | One verb fewer, but "ship" would mean "the default branch's head" for one environment and "what dev runs" for another. |

**Accepted: A.**

### Decision 9: `vecten next` across environments (Accepted)

`next` reads local files and run records only, and must not call GitHub.

| Option | Consequences |
|---|---|
| **A. Walk the order: each environment through its prerequisites, deploy gate and post-deploy items before the next** | With `["dev", "prod"]`, `next` takes `dev` from bootstrap to its first user and connection, then `prod`. The deploy gate of a later environment says it ships the commit the environment before it shipped. Key renewal and handover items cover every environment with facts, not only `prod`. With no order, `next` reports what it reports today. Pending promotions are not `next` items, because knowing them needs GitHub; `promote --status` and the `operate` skill report them. |
| B. Keep `next` on `prod` | The guide loop would deploy `prod` first and never reach `dev`. |

**Accepted: A.**

### Decision 10: `vecten issues` and the `operate` skill (Accepted)

| Option | Consequences |
|---|---|
| **A. `issues` unchanged; the skill loops over the order and asks before promoting** | `vecten issues --env <env>` already reads any environment. The `operate` skill reads the order from `vecten promote --status --json`, reads issues for each environment from the first, and delivers fixes by pull request as today. A merged fix ships to the first environment. Once that environment no longer lists the issue, the skill relays an `ask`, "Promote <commit> to <next env>?", and runs `vecten promote --env <next env>` only after the human's yes. An issue in a later environment is closed only once it leaves that environment's list. |
| B. `issues` reports pending promotions as issues | `issues` would need GitHub, and a pending promotion is not something broken. |
| C. `issues --env all` | Mixes environments in one list, when the skill acts on each separately. |

**Accepted: A.** The `shared/issues` requirement "Platform issues resolve on green" gains the promotion case.

### Decision 11: Projects whose workflows predate the order (Accepted)

The workflows pin the `vecten` version they were rendered with. An order declared in `vecten.toml` would be ignored by an older `ship.yml`, which ships `prod` on every push.

| Option | Consequences |
|---|---|
| **A. Refuse to record an order until `vecten update` has rendered the workflows of this release** | `vecten.toml`'s strict schema already makes an older CLI fail on the unknown `[deploy] promotion` key, so an older workflow fails without shipping rather than shipping `prod`. The newer laptop CLI also refuses to write the key while `[project] scaffold_version` is older than the release that introduces it, naming `vecten update`. |
| B. Document it | A forgotten `update` ships production on every merge, the failure this change exists to stop. |

**Accepted: A.**

## Interactions

- **#34, a shared Snowflake account.** Independent. A promoted commit carries `vecten.toml`, so each environment derives its Snowflake names from the same file, with its own environment name and account. #34's per-environment account travels in the tfvars, which a promotion takes from the promoted commit (Decision 5).
- **#56, each environment's MCP URL.** `next`'s post-deploy items for `dev` name `dev`'s MCP URL only with #56 in place.
- **#58, per-environment settings in tfvars.** Under Decision 5, `dags_paused_at_creation` and `root_volume_size_gb` reach a later environment from the promoted commit. A `prod`-only tfvars change therefore reaches `prod` on the next promotion, not on the push that merges it.
- **#53, `vecten dbt --env`.** Unaffected: it runs from the laptop's checkout.

## Risks / Trade-offs

- [Statuses are writable by anyone with write access] → They guard against mistakes, not against people with write access, who can already push and dispatch. The design says so.
- [The newest shipped commit is searched among the last 200] → An earlier environment that has not shipped in 200 commits is refused with a message naming `--commit`.
- [The default branch's workflow runs with the promoted commit's code] → The workflow's own steps must keep working across versions. They are thin: checkout, install the named `vecten`, run it. The CLI version comes from the promoted commit.
- [A waiting approval holds the environment's concurrency group] → With required reviewers, an apply or ship of that environment queues behind the waiting promotion. GitHub's interface shows it.
- [Mixed facts and configuration in `vecten.toml`] → Decision 5 reads facts from the default branch. A later change could move facts out of `vecten.toml`; this one does not.

## Migration Plan

- **Existing projects.** Nothing changes until `[deploy] promotion` is set. `vecten update` re-renders `ship.yml`, `deploy-platform.yml` and `terraform-apply.yml`. Without an order the new `ship.yml` ships `prod` on every push, as before, and also sets the `vecten/ship/prod` status.
- **Adopting an order.** Bootstrap and deploy the first environment (for example `dev`) as today. Set `[deploy] promotion = ["dev", "prod"]` with `vecten answer deploy.promotion dev,prod` and push: from that push on, merges ship `dev`, and `vecten promote --env prod` ships `prod`. The first promotion needs one push shipped to `dev` after `vecten update`, because only those ships set a status.
- **Rollback.** Remove `[deploy] promotion` with `vecten answer deploy.promotion none` and push: merges ship `prod` again.
