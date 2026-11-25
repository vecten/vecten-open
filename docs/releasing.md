# Releasing

A release's live checks deploy real environments to AWS and Scaleway, on Postgres and on
Snowflake, and an agent on your laptop runs them with access of its own to dedicated test
accounts. This page holds all of it. [One-time setup](#one-time-setup) is the maintainer's:
the copy of the tools the agent runs, the accounts and identities, the credential home that
holds them, and the GitHub rules around them. [Settings](#settings) gives the Claude Code
settings that let the agent use them without asking, what bounds the commands it runs and
where those bounds stop. [Running the live checks](#running-the-live-checks) is the agent's
runbook for a release, and [The results file](#the-results-file) the record it keeps.

## One-time setup

You do this alone, once, in one sitting, working down the list. Every item ends with how to
confirm it, so you can stop at any item and know where you are. Have these open:

- the AWS management account of your AWS Organization, and the AWS account that holds the
  existing sandbox DNS zone (to delegate two subdomains from it);
- the Scaleway console, as an owner of the test organisation;
- Snowsight, as ACCOUNTADMIN of the sandbox Snowflake account;
- GitHub, signed in as an owner of the `vecten` organisation and an administrator of
  `vecten/vecten-open`;
- PyPI and TestPyPI, signed in as an owner of the four projects;
- the Google Cloud console project that holds the OAuth client the deployed environments sign
  in with;
- a terminal at the root of a checkout of this repository, with `uv`, `aws` (version 2),
  `scw`, `snow` (the Snowflake CLI), `gh`, `git`, `terraform`, `openssl` and `dig` on the
  `PATH`. `terraform` is the version `terraform/` requires (`required_version`, 1.16 for
  0.1.2); an older one refuses every plan and destroy the run makes from the laptop.

This repository is public, so this page uses placeholders. The real values go in `allow.toml`
and the other files of the credential home, which live only on your laptop:

| Placeholder | Meaning |
|---|---|
| `<sandbox-zone>` | The existing public DNS zone for sandbox deployments |
| `<aws-zone>` | A new subdomain of it for AWS, such as `aws.<sandbox-zone>` |
| `<scaleway-zone>` | A new subdomain of it for Scaleway, such as `scw.<sandbox-zone>` |
| `<livecheck-account-id>` | The 12-digit ID of the new AWS member account |
| `<scaleway-org-id>` | The ID of the Scaleway test organisation |
| `<snowflake-account>` | What `select current_account();` returns in the sandbox account |
| `<sandbox-org>` | The new GitHub organisation for sandbox repositories |
| `<upstream-app>` | The slug of the GitHub App installed on `vecten/vecten-open` |

1. **The tools copy.** The agent runs every live-check command from a second clone of this
   repository that only you update, so that it cannot change the code the
   [Settings](#settings) let it run without asking. Clone it to `~/vecten-live-tools`, on the
   branch that carries `scripts/live/`: the open release branch, until the tooling is on
   `main`. Then give it its own environment:

   ```sh
   git clone https://github.com/vecten/vecten-open.git ~/vecten-live-tools
   git -C ~/vecten-live-tools switch release/<X.Y.Z>   # skip once the tooling is on main
   (cd ~/vecten-live-tools && uv sync --locked --all-packages)
   ```

   The copy's interpreter, `.venv/bin/python` and the Python installation it points at, must
   be one the agent cannot write: Python loads code from that installation (a
   `sitecustomize.py`, say) before the wrapper runs a single check. Every command the wrapper
   starts is denied writing that installation, and denied renaming any directory above it, so
   it cannot be moved aside, changed and moved back. The agent's own shell commands, in the
   command sandbox, cannot write it while it lies outside the checkout and `~/vecten-sandbox`.
   `doctor` fails when it lies inside the directory `doctor` runs from or inside
   `~/vecten-sandbox`.

   Update it yourself, after a change to `scripts/live/` merges into its branch: `git pull`,
   then `uv sync --locked --all-packages`, both in `~/vecten-live-tools`. Never work in it: it
   is not a checkout to edit, build in or switch, the settings deny the agent's file-editing
   tools there, and the wrapper refuses to run a credentialed command from inside it. Every
   other step on this page runs in the checkout you work in.

   Confirm: from the checkout, `~/vecten-live-tools/scripts/live/live doctor` prints
   `tools ok: <the copy's path> at <its commit>` as its first line, and `tools FAIL: …` when
   the copy is run from inside itself or its interpreter lies where the agent writes. Until the
   next item makes the credential home, it then stops with `access: no credential home at …`
   (exit 3).

2. **The credential home.** In the terminal you use for this whole list, make every new file
   owner-only and create the directory layout:

   ```sh
   umask 077
   LIVE=~/.config/vecten-livecheck
   mkdir -p $LIVE/aws $LIVE/scw $LIVE/snowflake $LIVE/github/gh $LIVE/google $LIVE/git
   chmod 700 $LIVE
   touch $LIVE/allow.toml
   ```

   `LIVE` is shorthand for the commands on this page only; in a new terminal, run the first
   two lines again. `allow.toml` lists the identities the wrapper (`scripts/live/access.py` of
   the tools copy, which its launcher `scripts/live/live` starts) accepts; you add one table to
   it at each of the next steps. The wrapper creates `cache/` (minted GitHub tokens), `home/`
   (the home directory of every command it runs with the test credentials) and `home-plain/`
   (that of every command it runs without them) itself. `VECTEN_LIVE_HOME` names
   another directory instead, but every path on this page, and the `denyRead` and `Read` deny
   entries under [Settings](#settings), assume the default.

   Confirm: `~/vecten-live-tools/scripts/live/live doctor` prints seven lines. The first is
   the `tools ok` line of item 1; the five identity lines say
   `FAIL: not configured in allow.toml` for now; the last says `os-sandbox ok`, which proves
   this Mac can start the sandbox the wrapper runs commands in.

3. **AWS.**
   - In the management account, AWS Organizations, add a new member account used for nothing
     but live checks. Note its account ID, `<livecheck-account-id>`.
   - Switch role into it (`OrganizationAccountAccessRole`). In IAM, create the user
     `livecheck` without console access, attach `AdministratorAccess` directly, and create one
     access key for the command line interface.
   - Store the key as profile `livecheck` in the credential home; `aws configure` prompts for
     the key, so it never lands in your shell history:

     ```sh
     AWS_CONFIG_FILE=$LIVE/aws/config AWS_SHARED_CREDENTIALS_FILE=$LIVE/aws/credentials \
       aws configure --profile livecheck
     ```

     Give it the region you deploy sandbox projects to.
   - In Route 53 of the new account, create a public hosted zone `<aws-zone>`. In the account
     that holds `<sandbox-zone>`, add an `NS` record named `<aws-zone>` whose values are the
     four name servers of the new zone.
   - In the management account, AWS Budgets, create a monthly cost budget of $60 filtered to
     the linked account `<livecheck-account-id>`, with an alert to your email at 100% of
     actual cost.
   - Add to `allow.toml` (quoted: an account ID is a string and may start with 0):

     ```toml
     [aws]
     account_id = "<livecheck-account-id>"
     ```

   Confirm: `doctor` prints `aws ok`; `dig +short NS <aws-zone>` prints the four name servers
   of the new zone; the Budgets page lists the budget with your email on its alert.

4. **Scaleway.**
   - Confirm that the existing sandbox API key belongs to an organisation that holds only test
     resources: in the console, look through every project of that organisation, and check
     under IAM that the key's bearer belongs to it. Note the organisation's ID,
     `<scaleway-org-id>`.
   - Move that key's profile from your own `~/.config/scw/config.yaml` into
     `$LIVE/scw/config.yaml`, at the top level of the file rather than under `profiles:`. The
     wrapper names no Scaleway profile, so `scw` reads the top-level one:

     ```yaml
     access_key: <access key>
     secret_key: <secret key>
     default_organization_id: <scaleway-org-id>
     default_project_id: <a project of that organisation>
     default_region: fr-par
     default_zone: fr-par-1
     ```

   - In Domains and DNS, add `<scaleway-zone>` as an external domain in a project of that
     organisation (`vecten bootstrap` refuses a zone that no project of the organisation
     holds). Add the records the console asks for to `<sandbox-zone>`: a validation `TXT`
     record and an `NS` record naming Scaleway's name servers
     (not yet tried against a live account).
   - In the console's billing section, add a consumption alert to your email at a monthly
     amount near the $60 run ceiling (not yet tried against a live account).
   - Add to `allow.toml`:

     ```toml
     [scaleway]
     organization_id = "<scaleway-org-id>"
     ```

   Confirm: `doctor` prints `scaleway ok` (every project the key can list belongs to
   `<scaleway-org-id>`); `dig +short NS <scaleway-zone>` prints Scaleway's name servers and the
   console shows the zone as active; the billing alert is listed.

5. **Snowflake.**
   - Move your connection to the sandbox account out of your own Snowflake configuration
     (`~/.snowflake/`, `~/.config/snowflake/` or `~/Library/Application Support/snowflake/`)
     into `$LIVE/snowflake/connections.toml`, as the table `[livecheck]` (the connection the
     wrapper names, as it names the AWS profile `livecheck`), and move its private key file
     into `$LIVE/snowflake/` beside it. Change the connection's key path (`private_key_file`)
     to the key's new absolute path: the wrapper's sandbox denies every command all three of
     those directories, so a key left there cannot be read. Create an empty
     `$LIVE/snowflake/config.toml` too (`touch $LIVE/snowflake/config.toml`): `snow` writes
     that file when it is missing, and a wrapped command cannot write the credential home
     outside `snowflake/logs/`, so without it every wrapped command would be refused at the
     Snowflake check. Then `chmod 600 $LIVE/snowflake/*`, since `snow` rejects a
     configuration file others can read (not yet tried against a live account).
   - In a Snowsight worksheet in the sandbox account, run `select current_account();` and
     note the result, `<snowflake-account>`.
   - Add a resource monitor that notifies you (not yet tried against a live account).
     Run this as role ACCOUNTADMIN; it needs no database, schema or warehouse. Choose a credit
     quota that comes to roughly $60 at your account's credit price:

     ```sql
     USE ROLE ACCOUNTADMIN;
     CREATE RESOURCE MONITOR livecheck_monthly
       WITH CREDIT_QUOTA = <credits>
       FREQUENCY = MONTHLY
       START_TIMESTAMP = IMMEDIATELY
       TRIGGERS ON 80 PERCENT DO NOTIFY
                ON 100 PERCENT DO NOTIFY;
     ALTER ACCOUNT SET RESOURCE_MONITOR = livecheck_monthly;
     ```

     Snowflake emails account administrators who have a verified email address and have
     turned on notifications in their Snowsight profile; check both for your user.
   - Add to `allow.toml` (case does not matter):

     ```toml
     [snowflake]
     account = "<snowflake-account>"
     ```

   Confirm: `doctor` prints `snowflake ok`; `SHOW RESOURCE MONITORS;` lists
   `livecheck_monthly` at level `ACCOUNT`.

6. **The GitHub sandbox organisation.**
   - Create a new free organisation, `<sandbox-org>`. It holds only sandbox repositories,
     which are public, because a free organisation gives environments to public repositories
     only and `vecten bootstrap` creates one.
   - In its settings, Actions, General: under the approval of workflow runs from fork pull
     requests, require approval for all outside collaborators, so a stranger's pull request
     runs nothing (not yet tried against a live account).
   - In its settings, Developer settings, GitHub Apps, create an App owned by
     `<sandbox-org>`, installable only on that account, with its webhook turned off and these
     repository permissions: Administration, Contents, Actions, Secrets, Variables,
     Environments, Workflows and Pull requests read and write; Metadata read. Note its App ID.
   - Generate a private key for it (the browser downloads a `.pem` file) and install it on
     `<sandbox-org>` with access to all repositories. The installation's settings page has the
     installation ID at the end of its address.
   - Move the key into the credential home and describe the App beside it; `key` is a file
     name in the same directory:

     ```sh
     mv ~/Downloads/<the downloaded key>.pem $LIVE/github/sandbox.pem
     ```

     ```toml
     # $LIVE/github/sandbox.toml
     app_id = "<App ID>"
     installation_id = "<installation ID>"
     key = "sandbox.pem"
     ```

   - Add to `allow.toml`:

     ```toml
     [github]
     sandbox_org = "<sandbox-org>"
     upstream_repo = "vecten/vecten-open"
     ```

   Confirm: `doctor` prints `github-sandbox ok`: the App mints a token and every repository it
   reaches is in `<sandbox-org>` (none yet is fine). `github-upstream` still fails until the
   next item.

7. **GitHub, `vecten/vecten-open`.**
   - Create a second App, owned by the `vecten` organisation, with its webhook turned off and
     these repository permissions: Contents, Pull requests and Actions read and write; Metadata
     read. Leave Workflows at No access. Install it on `vecten/vecten-open` only. Its slug is
     `<upstream-app>`. Generate its key and describe it as in the previous item, in
     `$LIVE/github/upstream.pem` and `$LIVE/github/upstream.toml`.

     Without the Workflows permission, GitHub refuses a push of the App's whose commits add or
     change a file under `.github/workflows/`. So a workflow that runs on the App's pushes is
     some version a maintainer wrote, not necessarily the current one, and only as far as
     GitHub's check behaves as the test before the packages step shows. Two things follow. A
     fix that touches `.github/workflows/` is pushed by you, not the agent
     ([Findings](#findings) says how the agent hands it over). And the first candidate can
     only be cut once `publish.yml` is on the release branch, since the App cannot push a
     candidate branch that adds it.
   - Import the two tag rulesets checked in beside the branch rulesets: in the repository's
     settings, Rules, Rulesets, New ruleset, Import a ruleset, once with
     `.github/rulesets/release-tags.json` and once with `.github/rulesets/candidate-tags.json`.
     The first restricts creating, updating and deleting tags that match `v*` but not
     `v*rc*`, and only the repository's Admin role, which only the maintainers hold, bypasses
     it: release tags are yours alone. The second restricts updating and deleting tags that
     match `v*rc*`, and nothing bypasses it: the App can push a candidate tag, but once pushed
     the tag cannot be moved or removed.
   - Open the branch rulesets on `main` and `release/*` and confirm that `<upstream-app>` is
     not in either bypass list.
   - In Environments, create `candidate` with no protection rules, and `release` with the
     maintainers as required reviewers and, under Deployment branches and tags, Selected
     branches and tags with two tag rules, `v*.*.[0-9]` and `v*.*.[0-9][0-9]`. Release tags
     match them and candidate tags (`v<version>rcN`) do not, so no candidate's run can
     deploy to `release`. The `publish` workflow publishes candidates from the first; from the
     second it publishes a release after one approval, yours, of its packages job, and the
     images follow without another.
   - List the repository's Actions secrets and variables (`gh secret list --repo
     vecten/vecten-open` and `gh variable list --repo vecten/vecten-open`). The workflows that
     run on the App's pushes run the code it pushed, and that code can read every
     repository-level secret a workflow hands it, so any that is not a credential of the test
     accounts moves into an environment with required reviewers, such as `release`.
   - Before the next step, test what GitHub's Workflows check covers. The registry permission
     of the next step is repository-wide and holds for any version of any workflow, and an
     earlier version of `publish.yml` whose `images` job runs tree code
     (`uv run vecten dev build-images --push`) is part of this repository's history: the
     earlier commits of the pull request that brought in the current one carry it, and they
     stay reachable through that pull request's ref after a squash merge. On a throwaway
     repository you own, with a throwaway App that has Contents read and write and no
     Workflows permission, installed on that repository only (not yet tried against a live
     account):
     1. Commit a workflow file `.github/workflows/probe.yml`, then a second commit that changes
        it, both pushed as yourself to the default branch.
     2. (a) An older workflow under a new commit: branch `probe-a` from the first commit,
        change a file outside `.github/`, commit, tag it `v0.0.1rc1`, and push the branch and
        the tag with the App's installation token:

        ```sh
        git push https://x-access-token:<token>@github.com/<you>/<throwaway> probe-a v0.0.1rc1
        ```

     3. (b) A stranger's commit: from another account's fork, open a pull request whose commit
        changes `.github/workflows/probe.yml`; fetch it with
        `git fetch origin pull/<number>/head`, tag it with `git tag v0.0.2rc1 FETCH_HEAD`, and
        push the tag with the App's token as in (a).

     Refused looks like this, for each ref pushed, and no such ref appears on the repository:

     ```text
     ! [remote rejected] probe-a -> probe-a (refusing to allow a GitHub App to create or
     update workflow `.github/workflows/probe.yml` without `workflows` permission)
     ```

     If GitHub refuses both, the arrangement holds as this item describes:
     continue with the next step. If it accepts either, do not give the repository's Actions
     the Write role. The alternative, not built, is a registry credential stored in a GitHub
     environment whose deployments are limited to the default branch, used by a workflow that
     runs from the default branch after the tag's run has built the images (a `workflow_run`
     workflow), so that no workflow at a tag ever holds it. It can only work once that
     workflow is on `main`; until then the maintainer pushes candidate images.
   - In the `vecten` organisation's packages, open `vecten-airflow` and `vecten-mcp`, and in
     each one's package settings give the `vecten-open` repository's Actions the Write role.
     With it, a workflow run in this repository can request `packages: write` on the two
     images, with no approval, and deployed projects pull their images by tag. The App cannot
     use that to push a released tag, provided the test above refused both pushes: its pushes
     then run only workflow versions you wrote, and in the current `publish.yml` the one job
     that requests `packages: write`, `images`, executes no script, build backend or CLI from
     the tagged tree (only pinned actions and its own shell), and tags both images with the
     pushed tag's name, which for the App is always a candidate's (`v*rc*`). What remains:
     - A candidate's image contents are whatever its tree builds, which is the point of a
       candidate.
     - Before each push, the `images` job asks the registry whether that image's tag exists,
       in shell written in the workflow file, on every attempt, a rerun of the failed jobs
       included, and pushes each image only when its own tag is absent. A tag, a candidate's or
       a release's, is therefore pushed once and never over. That holds for the current
       `publish.yml`; an older version of it, which the test above keeps from running, has no
       such check.
     - The tree's Dockerfiles do run in the `images` job: their `RUN` steps, and the frontend a
       `# syntax=` line or a `BUILDKIT_SYNTAX` build argument names, run inside BuildKit's
       container after `docker/login-action` has written the token to the runner's Docker
       configuration. That container is not handed the registry credential, so reaching it
       from a Dockerfile takes an escape from the container. The stronger arrangement, not
       built: build the images to an archive in a job with no write permission, and push them
       from a job that runs no build.
     - The `images` job inspects the images after its registry login, but `verify` inspects
       them without one, so both packages must be public. If one is private, the run stops at
       `verify`, after the images were pushed.

     PyPI is not exposed the same way, since its trusted publisher is bound to the `release`
     environment.

   Confirm: `doctor` prints `github-upstream ok` (the App reaches exactly
   `vecten/vecten-open`). With your own `gh` (not yet tried against a live account):

   ```sh
   gh api repos/vecten/vecten-open/rulesets --jq '.[] | "\(.id) \(.target) \(.name)"'
   gh api repos/vecten/vecten-open/rulesets/<ruleset id> \
     --jq '{conditions, rules: [.rules[].type], bypass: .bypass_actors}'
   gh api repos/vecten/vecten-open/environments \
     --jq '.environments[] | {name, rules: [.protection_rules[].type]}'
   gh api repos/vecten/vecten-open/environments/release/deployment-branch-policies \
     --jq '.branch_policies[] | {name, type}'
   gh secret list --repo vecten/vecten-open
   gh variable list --repo vecten/vecten-open
   ```

   The second command, once for each tag ruleset id the first prints: `release tags` includes
   `refs/tags/v*`, excludes `refs/tags/v*rc*`, has the rules `creation`, `update` and
   `deletion`, and its only bypass is the Admin role (`actor_id` 5); `candidate tags` includes
   `refs/tags/v*rc*`, has the rules `update` and `deletion`, and no bypass. `candidate` has no
   rules and `release` has `required_reviewers` and `branch_policy`, whose policies are the two
   tag patterns, each of type `tag`. The secrets and variables left at repository level are
   credentials of the test accounts only. Do not test a ruleset by pushing a tag: a tag that
   gets through starts a publish.

8. **The git identity of the agent's commits.** The wrapper gives git
   `$LIVE/git/config` as its global configuration when that file exists, and none otherwise.
   Give it the bot identity of `<upstream-app>`, so the agent's commits are attributed to the
   App:

   ```sh
   gh api '/users/<upstream-app>[bot]' --jq .id
   git config --file $LIVE/git/config user.name '<upstream-app>[bot]'
   git config --file $LIVE/git/config user.email '<id>+<upstream-app>[bot]@users.noreply.github.com'
   ```

   where `<id>` is the number the first command prints (not yet tried against a live account).

   Confirm: `git config --file $LIVE/git/config --list` prints both lines.

9. **Package indexes.** On TestPyPI, add a trusted publisher to each of the four projects
   (`vecten`, `vecten-shared`, `vecten-platform`, `vecten-mcp`): GitHub, owner `vecten`,
   repository `vecten-open`, workflow `publish.yml`, environment `candidate`. On PyPI, the
   same for each of the four with environment `release`. The `publish` workflow signs in to
   each index as itself.

   Confirm each project's Publishing page on both indexes lists the expected publisher and
   each Collaborators page lists only the intended owners and maintainers. A candidate
   tests publishing to TestPyPI. A release tag on `main` tests PyPI.

   Registering a trusted publisher leaves existing API tokens active. Once these
   registrations and the required reviewers for the GitHub `release` environment are in
   place, each owner and maintainer should check the API tokens in their own account on
   each index. Review both account-wide tokens and tokens scoped to any of the four projects. If another
   project still uses an account-wide token, move that project to its own publishing
   credential before revoking the old token. Each token owner should confirm that no
   remaining token can upload these projects. See the account settings on
   [PyPI](https://pypi.org/manage/account/#api-tokens) and
   [TestPyPI](https://test.pypi.org/manage/account/#api-tokens).

   Track confirmation from the owner of the token used for the manual 0.1.0 upload in
   [issue #22](https://github.com/vecten/vecten-open/issues/22). Record any outstanding
   confirmation there without including a token value.

10. **Google sign-in.** Each combination deploys under a fixed domain, so its callback address
    is registered once, not per run.
    - Write `$LIVE/google/hostnames.txt`, one line per combination, the combination name and
      its domain:

      ```text
      aws-postgres aws-postgres.<aws-zone>
      aws-snowflake aws-snowflake.<aws-zone>
      scaleway-postgres scaleway-postgres.<scaleway-zone>
      scaleway-snowflake scaleway-snowflake.<scaleway-zone>
      ```

    - In the Google Cloud console, APIs and services, Credentials, open the OAuth client the
      deployed environments sign in with and add `https://mcp.<domain>/oidc/callback` for each
      of the four domains to its authorised redirect URIs.
    - Copy the client's ID and its secret from the same page into the credential home, one at a
      time, so the secret never passes through your shell history. The ID is not a secret; the
      agent configures the deployed environments with it:

      ```sh
      pbpaste > $LIVE/google/oidc-client-id       # after copying the client ID
      pbpaste > $LIVE/google/oidc-client-secret   # after copying the client secret
      ```

    - Name the Google account that signs in during the checks, in `$LIVE/google/admin.toml`. The
      agent makes it the first admin and the first user of every sandbox MCP:

      ```toml
      email = "<the Google account's address>"
      firstname = "<its first name>"
      ```

      This address is written into each sandbox project's `vecten.toml` (`[mcp] admins`), so it
      is public in that project's repository for as long as the repository exists. Your
      ordinary address will do; a Google account made for these checks works the same, if you
      prefer to keep yours out of them.

    Confirm: the client lists the four redirect URIs; `cat $LIVE/google/oidc-client-id` prints
    the client ID, and `oidc-client-secret` holds the secret and nothing else (check it in an
    editor rather than printing it). `~/vecten-live-tools/scripts/live/live show admin-email`
    prints the address, and `show hostname aws-postgres` the first domain.

11. **`allow.toml`, complete.** It now holds the five IDs, and nothing else:

    ```toml
    [aws]
    account_id = "<livecheck-account-id>"

    [scaleway]
    organization_id = "<scaleway-org-id>"

    [snowflake]
    account = "<snowflake-account>"

    [github]
    sandbox_org = "<sandbox-org>"
    upstream_repo = "vecten/vecten-open"
    ```

    Confirm: `stat -f %Sp $LIVE` prints `drwx------`, so only you can read the credential
    home.

12. **Settings.** Paste the two blocks under [Settings](#settings).

    Confirm: `claude auto-mode config` shows the environment and allow entries.

13. **The whole check.** From the root of the checkout:

    ```sh
    ~/vecten-live-tools/scripts/live/live doctor
    ```

    Confirm: it exits 0 and every line says `ok`:

    ```text
    tools ok: <your home>/vecten-live-tools at <its commit>
    aws ok
    scaleway ok
    snowflake ok
    github-sandbox ok
    github-upstream ok
    os-sandbox ok
    ```

    A `FAIL` line names the reason; go back to that item. Then
    `~/vecten-live-tools/scripts/live/live run -- git config --global --list` prints the
    identity of item 8, which shows the guard passes and a command runs. Last, start a new
    Claude Code session in the checkout and ask it to run the same `doctor` command: it runs
    without a permission prompt and prints the same seven lines. Ask it too to change a line
    of `~/vecten-live-tools/scripts/live/access.py`: its edit tools are denied, and a shell
    command that writes there fails in the command sandbox.

## Settings

The agent cannot write either settings file, so you paste these two blocks yourself, merging
them into whatever the files already hold, and check the result with `claude auto-mode config`.

In `.claude/settings.local.json` of the checkout, which git ignores. It lets the agent run one
command, the launcher of the tools copy (`~/vecten-live-tools/scripts/live/live`, setup item
1), without a prompt and outside the command sandbox; keeps the agent's own file-editing tools
out of the tools copy; makes `~/vecten-sandbox` (the throwaway sandbox projects) a directory it
can work in; keeps every other command, and the agent's own file-reading tools, out of the
credential home; and lets commands in the command sandbox reach the image registry and the
address checker of the runbook:

```json
{
  "permissions": {
    "allow": ["Bash(~/vecten-live-tools/scripts/live/live *)"],
    "deny": ["Read(~/.config/vecten-livecheck/**)", "Edit(~/vecten-live-tools/**)",
             "Write(~/vecten-live-tools/**)"],
    "additionalDirectories": ["~/vecten-sandbox"]
  },
  "sandbox": {
    "excludedCommands": ["~/vecten-live-tools/scripts/live/live *"],
    "filesystem": {"allowWrite": ["~/vecten-sandbox"], "denyRead": ["~/.config/vecten-livecheck"]},
    "network": {"allowedDomains": ["ghcr.io", "checkip.amazonaws.com"]}
  }
}
```

The launcher starts the wrapper (`access.py`) of the tools copy with the copy's own Python, and
the wrapper runs the other live-check scripts beside it by name: `live combos` runs
`combos.py`, `live cost` `cost.py`, `live candidate` `candidate.py` and `live project`
`sandbox_project.py`. So this one rule covers every live-check command, and each runs the code
of the tools copy, not of the checkout the agent edits. Any other form, such as running a
script of the checkout itself, falls outside the rule and gets the permission prompt.
`github_app.py`, whose `token` command prints a live token, is not run by itself at all: the
wrapper runs it to mint the token a command gets.

In `~/.claude/settings.json`, the only file the auto-mode classifier reads this key from. It
tells the classifier that the test accounts, the sandbox organisation and `~/vecten-sandbox`
are the agent's to change, and that the wrapper and candidate pushes are the agent's to run:

```json
{
  "autoMode": {
    "environment": ["$defaults",
      "The live-check test accounts (one AWS account, one Scaleway organisation, one Snowflake account) and every repository of the sandbox GitHub organisation exist only for release testing and are this agent's to create, change and destroy.",
      "~/vecten-sandbox holds throwaway sandbox projects owned by this agent."],
    "allow": ["$defaults",
      "Commands run through `~/vecten-live-tools/scripts/live/live run`, typed in exactly that form with nothing in front, which refuses any identity outside the test accounts.",
      "Pushing branches and tags matching v*rc* to vecten/vecten-open when done through `~/vecten-live-tools/scripts/live/live run --github upstream` or `~/vecten-live-tools/scripts/live/live candidate cut`, typed in exactly that form with nothing in front."]
  }
}
```

### Why the wrapper runs outside the command sandbox

Claude Code's command sandbox cannot hold the wrapper, for two reasons: the commands it runs
include `uv`, which cannot reach the network inside it (it panics; reproduced on 2026-11-19),
and macOS cannot start one sandbox inside another, so the wrapper's own `sandbox-exec` exits 71
and the command never starts. `doctor` runs under the same sandbox as the commands, so from
inside Claude Code it too must be run as `~/vecten-live-tools/scripts/live/live doctor`, the
excluded form.

Two entries keep the agent out of the credential home. `sandbox.filesystem.denyRead` binds
its shell commands only; its own file-reading tools follow `permissions.deny`, hence the
`Read` entry there. Only the excluded launcher can read the home.

Three things keep the agent from changing the tools copy, and so the code the rule lets it run
unasked: the `Edit` and `Write` deny entries bind its own file-editing tools; its shell
commands run in the command sandbox, which lets them write the checkout and `~/vecten-sandbox`
but not the copy; and every command the wrapper starts is denied the copy and its interpreter
by the OS profile below.

### What bounds a wrapped command

Outside the OS profile the wrapper does three things only: it parses its arguments, reads the
credential home (the allow list, the secret values it redacts and looks for, a
`--stdin-file`), and mints the GitHub token, which `github_app.py` signs with the system's
`/usr/bin/openssl`. The token command reads and writes only the credential home's `cache/`,
which no command the wrapper starts may write, and neither reads nor writes through a link
there. For the secret check the wrapper also reads the files and git objects it looks into,
without running anything. Every tool it runs on files the agent can write runs inside the
profile:
every identity probe, every wrapped command, the `git` of the secret check and of `doctor`, and
the subcommands that need no credential.

A wrapped command is one that runs with the test credentials: `live run -- <command>`, every
`live candidate` call and every `live cost` call but `estimate` and `spent`. `live combos`,
`live cost estimate`, `live cost spent` and `live project` run under the same profile, with no
credential and no identity check: their environment is built from the same allowlist, without
the tools' configurations, the App token, the git credential helper or `VECTEN_LIVE`. They read
whatever path they are given (`live combos --ledger` and `--map`, `live cost --created`), and
`live cost` prices with the `vecten` of the tools copy. `live project check-public` runs both of
its checks: the wrapper runs the secret check first and hands its findings to the script, which
cannot start a sandbox inside its own. Both checks, for credential values and for forbidden file
names, cover everything a push of the project can publish: every version of every file, under
every path it had, in every commit a branch, tag or HEAD reaches, the staged content of every
index entry, and the working-tree content of every file `git add --all` would stage (tracked
files and untracked ones that the project's own `.gitignore` files and `.git/info/exclude` do
not ignore; no global ignore file counts, and a commit replaced through `refs/replace/` counts
as itself).
`secrets-check` looks at the same three for credential values. Both take the project's top
level and refuse a subdirectory. A git command that fails makes either check refuse, never
pass. `doctor` checks every identity as
below, and `show` reads only the credential home. A wrapped command refuses (exit 3) to run
inside the tools copy, as the current directory or as its `--cwd`: the copy is not a place to
work in.

What bounds a wrapped command is the identity guard, the command's own home and environment,
and the OS sandbox, all below. Together they keep tools, and slips, away from your own
credentials and accounts; they do not contain an agent that sets out to work around them
([Limits](#limits) says why). The executable list is no bound: `uv` is on it, and `uv run`
starts any program, which the runbook itself relies on. The list only keeps the common case
tidy. `live run -- <command>` refuses a first word outside `vecten`, `aws`, `scw`, `snow`,
`gh`, `git`, `terraform` and `uv`, and a first word that is a path (`./vecten`,
`/opt/bin/aws`): only bare names, found on `PATH`, run. `docker` is not among them: local
Docker work needs no cloud credential, so it runs outside the wrapper.

Before every command the wrapper asks each tool who it is and refuses (exit 3) unless the AWS
account, every Scaleway project's organisation, the Snowflake account and the GitHub App's
repositories are the ones in `allow.toml`; a probe that fails or gives no answer within 30
seconds refuses too. The probes get no standard input, so nothing piped to the wrapper reaches
them. `--github upstream` uses the `vecten/vecten-open` App, and the default,
`--github sandbox`, the sandbox organisation's.

Two options shape the command itself. `--cwd <directory>` runs it in that directory (the probes
still run where the wrapper started), which must exist. `--stdin-file <path>` gives it a file
of the credential home, named relative to the home, as standard input; a path that leads
outside the home, through `..` or a symlink, or names no file is refused, and the wrapper never
prints the file. That is how a secret such as the Google client secret reaches a command without
passing through the agent.

`live show <name>` prints one setting that is not a secret, and nothing else:
`sandbox-org` and `upstream-repo` from `allow.toml`, `hostname <combination>` from
`google/hostnames.txt`, `oidc-client-id`, and `admin-email` and `admin-firstname` from
`google/admin.toml`. Any other name, or a missing file, key or line, is a one-line refusal
(exit 3) that prints no file content, and so is a value that holds a secret of the credential
home, such as the client secret pasted into the wrong file.

The command then runs with:

- **Its own OS sandbox.** On macOS, every probe and the command run under a Seatbelt profile
  (`sandbox-exec`) that allows everything by default and denies only what it names. The
  processes the command starts inherit it, but a process can have another started outside it
  (see [Limits](#limits)). Each path below is denied both as written and where a symlink
  resolves to. For each path it names whose writing it denies, renaming any directory above it
  is denied too, so no directory can be moved aside, written beneath and moved back; the
  `.claude` rule below needs no such rule, since it matches by name wherever the path lies.
  The profile denies:
  - reading and writing, under your real home, the directories `~/.aws`, `~/.config/scw`,
    `~/.snowflake`, `~/.config/snowflake`, `~/.ssh`, `~/.config/gh`, `~/.gnupg`, `~/.kube`,
    `~/.config/gcloud`, `~/.terraform.d`, `~/Library/Keychains`,
    `~/Library/Application Support/snowflake`, `~/.dbt`, `~/.snowsql` and `~/.config/op`, and
    the files `~/.docker/config.json`, `~/.netrc`, `~/.pypirc`, `~/.npmrc`,
    `~/.git-credentials`, `~/.config/git/credentials`, `~/.terraformrc`,
    `~/.claude/.credentials.json` and `~/.pgpass`;
  - reading any file named `.env` or `.env.<name>` anywhere under your real home, except under
    `~/vecten-sandbox`, whose projects' own `.env` files `vecten` reads;
  - writing the credential home, except its `home/`, `home-plain/`, `snowflake/logs/` and
    `github/gh/`, which the tools write; so no command can edit `allow.toml`, the guard's own
    list, or the token cache `cache/`;
  - writing anything under `~/.claude`, the agent's own settings, hooks, plugins, skills and
    memory: a wrapped command has its own home and no business there;
  - writing any directory named `.claude`, wherever on the disk it lies, its `settings.json`
    and `settings.local.json`, and its `hooks/`, `skills/`, `commands/` and `agents/` trees,
    whose scripts and prompts the agent runs: whatever directory the wrapper was started in or
    given as `--cwd`, and wherever a checkout is moved to (`.claude-plugin/` and the rest of a
    `.claude` directory, such as `worktrees/`, are not matched);
  - writing the files that steer every later shell or every later `git`: `~/.zshrc`,
    `~/.zprofile`, `~/.zshenv`, `~/.bashrc`, `~/.bash_profile`, `~/.profile`, `~/.gitconfig`
    and everything under `~/.config/git/`;
  - writing the tools copy the wrapper runs from, `~/vecten-live-tools`, and the Python
    installation its interpreter runs from, so no command can change the code the settings let
    the agent run unasked, or plant code Python loads before the wrapper's first check;
  - every unix-socket connection but the name resolver's (`/private/var/run/mDNSResponder`)
    and those beneath the run's own `TMPDIR`, so no Docker daemon, ssh-agent or gpg-agent is
    reachable while Terraform still reaches its provider plugins.
- **Its own home.** `HOME` is `<credential home>/home`, or `<credential home>/home-plain` for
  a subcommand that needs no credential, so a tool's default lookups miss your own files. The
  wrapper refuses (exit 3) a home that is a link or a file rather than a directory.
- **Its own temporary directory.** For `live run`, `TMPDIR` is a new directory under
  `<credential home>/home/tmp`, made for this one run and open to you alone; the next run
  removes any older than a day.
- **An environment built from an allowlist.** Only `PATH`, `LANG`, `TERM`, `TMPDIR`, `USER`,
  `LOGNAME`, `SHELL`, `TZ`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, the proxy variables and the
  `LC_*` variables pass through. The wrapper sets the rest: each tool's configuration in the
  credential home (`AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`, `AWS_PROFILE=livecheck`,
  `SCW_CONFIG_PATH`, `SNOWFLAKE_HOME`, `GH_CONFIG_DIR`), a freshly minted App token in
  `GH_TOKEN`, git's global configuration from `<credential home>/git/config`, only `gh` as
  git's credential helper for github.com, GitHub SSH remotes rewritten to HTTPS, git over
  SSH refused, and `GIT_NO_LAZY_FETCH=1`, so git fetches nothing from a promisor remote the
  repository names. A subcommand that needs no credential gets the same environment without
  the tools' configurations, with the unconfigured placeholder in `GH_TOKEN`, no credential
  helper and no `VECTEN_LIVE`.

The permission rule matches the command's text, so it matches only the launcher typed in full
with nothing in front. `VECTEN_LIVE_HOME` (and `HOME`) choose the credential home the wrapper
reads, so a command with an environment assignment in front of the launcher falls outside the
rule and goes to the permission prompt or the auto-mode classifier. That is intended.

### Limits

- **The wrapper is not a containment.** It keeps tools, and slips, away from your own
  credentials and accounts. The tools copy and the OS profile close the easy ways around it.
  The code the settings let the agent run unasked, outside the command sandbox, is the copy's,
  and the agent cannot change it: its file-editing tools are denied the copy, its sandboxed
  shell commands cannot write there, and every command the wrapper starts is denied the copy
  and its interpreter. And everything the wrapper runs on files the agent writes, `git` in the
  checkout and in the sandbox projects included, runs inside the profile; outside it the
  wrapper only parses its arguments, reads the credential home and mints the GitHub token
  ([What bounds a wrapped command](#what-bounds-a-wrapped-command)). It still does not contain
  an agent that deliberately works around it:
  - The profile is a list of denied places. Its write denies cover the tools copy and its
    interpreter, the credential home outside `home/`, `home-plain/`, `snowflake/logs/` and
    `github/gh/`, all of `~/.claude`, and the shell and git startup files, with the renaming of
    every directory above each; and, wherever a checkout lies, its `.claude` directory, settings
    files and hook, skill, command and agent trees, matched by name. A command it holds can
    still write the rest of your home directory, and read any credential file it does not
    name.
  - A process inside the profile can have another started outside it, through
    LaunchServices, Apple Events or launchd.
  - A wrapped `uv run …`, such as the release commit's `uv run vecten dev bump-version` or the
    `uv run --no-project` form of a sandbox project's `vecten`, still runs code from the
    working checkout or the run's own environments, which the agent can change; it runs inside
    the profile, with the test credentials.
  - On Linux there is no profile: `doctor` prints `os-sandbox absent (not macOS)`, a wrapped
    command is bounded by its home, its environment and the guard only, and the subcommands
    that need no credential by their environment only.

  Text written by a stranger can reach the agent: the runbook has it read pull request bodies
  and work in public repositories.
- **A list of denied places cannot be completed.** Each successive review of this wrapper
  found another way around its list: through the working checkout, git's own configuration,
  the interpreter, a renamed directory, a link in the token cache, a checkout's agent files,
  the same checkout moved out of the home and back. Each was closed by naming one more place.
  Such a list can be extended but not completed, so expect the next way around rather than
  rule it out. The dependable forms of containment, a profile that allows writing only to
  named places, or a separate macOS user for the agent, are not built.
- **A candidate's images are pushed without an approval.** Setup item 7 gives this
  repository's Actions the Write role on the two images, for any version of any workflow, and
  an earlier version of `publish.yml` whose `images` job runs tree code is part of this
  repository's history. The App can cause only candidates' image tags to be pushed provided
  GitHub refuses its pushes that would run another workflow version (the test in item 7, not
  yet tried against a live account): the current `images` job, the one that can push an
  image, executes no script, build backend or CLI from the tagged tree and takes the tag from
  the pushed candidate tag. What remains, as the item says: the tree's Dockerfiles run in that
  job inside BuildKit's container, which is not handed the registry credential, so reaching it
  takes an escape from that container (building to an archive without write permission and
  pushing from a job that runs no build would close it; not built); the check that keeps a tag
  from being pushed twice is in the current workflow file only, so it holds only as far as the
  test in item 7 does; and
  `setup-buildx-action` and `setup-qemu-action` pull the BuildKit and binfmt images by moving
  tags into the job that holds the credential, a supply-chain dependency the tree cannot
  reach.
- A symlink inside a denied directory that points outside it stays readable.
- Whether the deny on `~/Library/Keychains` stops a keychain read through `/usr/bin/security`
  has not been verified.
- The executable list bounds nothing while `uv` is on it, since `uv run` starts any program.
- The subcommands that need no credential act on what they are given, inside the profile:
  `live combos --ledger` and `--map` and `live cost --created` read whatever path they name;
  `live combos record` writes the ledger of the checkout it runs in, or the one `--ledger`
  names; `live project candidate` and `release` rewrite the workflows of the project named,
  skip a workflow that is a link within the project, and refuse one that leads outside it.
- The public check (`live project check-public`, and `secrets-check` for its credential half)
  finds only what it looks for: the values of the credential home and the file names `.env`,
  `.env.*`, `*.pem` and `*.key`. It looks wherever a push can publish them: every path a file
  had in any commit a branch, tag or HEAD reaches, what is staged, and what `git add --all`
  would stage. A hit in a commit is not fixed by removing the file: the history has to be
  rewritten, or the project rendered afresh.
- **Sandbox repositories are public.** While one exists, it publishes the project's domain
  (in `vecten.toml` and `deploy/envs/prod.tfvars`), on a Snowflake combination the Snowflake
  account identifier (in the tfvars), the sign-in address of `google/admin.toml` (in
  `vecten.toml`'s `[mcp] admins`), the Google OAuth client ID (in `vecten.toml`; its numeric
  prefix is the Google Cloud project number), and the bot's git identity of setup item 8 (in
  every commit), besides the workflow logs. That is accepted for those repositories only; the
  run deletes each one when its environment is destroyed.

## Running the live checks

For the agent. When the maintainer says "run the live checks for `<version>`", you work down
this section in order, alone, and stop only where a step says so. Everything you need is on
this page; a new session resumes from the [results file](#the-results-file). Steps marked "(not
yet tried against a live account)" rest on setup or cloud details no run has proven yet: when
one fails, record exactly what happened in the results file and fix the step on this page in
the run's pull request.

| Placeholder | Meaning |
|---|---|
| `<version>` | The release, `X.Y.Z`, as in the branch `release/<version>` |
| `<candidate>` | The candidate under test, `<version>rcN`; its tag is `v<candidate>` |
| `<previous>` | The newest release on PyPI, the one the upgrade path starts from |
| `<combination>` | `aws-postgres`, `aws-snowflake`, `scaleway-postgres` or `scaleway-snowflake` |
| `<run>` | `$HOME/vecten-sandbox/<version>`, the run's own directory |
| `<cli>` | `<run>/cli/<cli version>`, a virtual environment holding one published `vecten` |
| `<name>` | `live-X-Y-Z-<combination>`, the version's dots as hyphens: project and repository |
| `<project>` | `<run>/<combination>`, the sandbox project |
| `<prefix>` | `vecten-<name>-prod`, the prefix of every cloud name bootstrap gives the project |
| `<results>` | `.claude/live-checks-<version>.md` in the checkout, the results file |
| `<created>` | `.claude/live-checks-<version>.created.json` in the checkout, the created file |

Every sandbox project has one environment, `prod`. `$HOME` is your real home directory: write
it as `$HOME` so your shell expands it before the wrapper gives the command a home of its own.

### Prompts to expect

With the [Settings](#settings) in place, every step runs without a permission prompt except
these, which need the maintainer:

- [the sign-in sitting](#the-sign-in-sitting), once per environment;
- the first `ssh` to each Scaleway host ([A shell on a host](#a-shell-on-a-host)), which raises
  a permission prompt once per host;
- the image check of [Candidate](#candidate) step 4, if the command sandbox refuses the Docker
  socket;
- decisions: a `FAIL` from `doctor`, a run over the $60 ceiling, a finding to block on or
  follow up, and every other step that says to tell the maintainer and stop or wait.

Any other prompt is a gap in this page: record it in Hand-offs with its cause. A live-check
command that prompts because it was not typed in the launcher form of
[How commands run](#how-commands-run), item 1, is not such a gap: it is a mistake in the
command; type it again in that form.

### How commands run

1. Run every command from the root of the checkout, never from the tools copy. Every
   live-check command runs through the launcher of the tools copy, typed in exactly this form:
   `~/vecten-live-tools/scripts/live/live <subcommand> …`. Use no variable, alias or other path
   for it, and put nothing in front of it, no environment assignment and no `cd`: the
   permission rule matches the command's text, and any other form raises a prompt. Running
   text on this page names such a command `live <subcommand>`; every command block writes it
   in full, and so do you. The subcommands are `run` (item 2), `doctor`, `show` (item 6), and
   `combos`, `cost`, `candidate` and `project`, which run `combos.py`, `cost.py`,
   `candidate.py` and `sandbox_project.py` of the tools copy with the arguments that follow.
   They run the copy's code, so a change to `scripts/live/` in the checkout takes effect only
   once the maintainer has merged it and updated the copy (setup item 1). Any other command of
   the checkout runs from its virtual environment, as `.venv/bin/<tool> …`, since `uv` cannot
   work inside the command sandbox.
2. A command that needs a test credential (every cloud, GitHub and git command, including local
   git) runs through the wrapper:

   ```sh
   ~/vecten-live-tools/scripts/live/live run [--github upstream] [--cwd <directory>] \
     [--stdin-file <path in the credential home>] -- <command> …
   ```

   The first word of `<command>` is a bare name, one of `vecten`, `aws`, `scw`, `snow`, `gh`,
   `git`, `terraform` or `uv`; a path is refused (exit 2). `--github upstream` gives GitHub and
   git the `vecten/vecten-open` App; without it they get the sandbox organisation's App.
   `--cwd` runs the command in that directory. `--stdin-file` feeds it a file of the credential
   home as standard input. Exit 3 means the wrapper refused: its message names the identity, the
   option or the directory at fault; tell the maintainer and do not reach the account another
   way. Every live-check command exits the same way: 2 is a usage error or a refusal before
   anything ran, 3 a refusal by the tool its message names, and a traceback is exit 1 and
   always a finding about the tooling. A step names any other exit it relies on, such as the
   sweep's 1 and 4.
3. `live cost sweep` and every `live candidate` call run with the test credentials as
   `live run` does, the identity check first: `candidate` with the `vecten/vecten-open` App,
   `cost sweep` with the sandbox organisation's. Never put them inside `live run`.
   `live combos`, `live cost estimate`, `live cost spent` and `live project` need no
   credential: they run in the same OS sandbox, with none.
4. A `vecten` command in a sandbox project is written below as `vecten <arguments>`, short for
   this full form, which runs the CLI in `<cli>` with `<project>` as its working directory:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --cwd <project> -- \
     uv run --no-project --python <cli>/bin/python vecten <arguments>
   ```

   The wrapper takes bare names only, so the CLI of `<cli>` runs through `uv`, which puts
   `<cli>/bin` first on the command's `PATH`. A step that needs `--stdin-file` adds it beside
   `--cwd`. Every such command goes through the wrapper, even one that needs no credential, so
   that a `vecten login` session and the commands that use it share the wrapper's home. A
   `vecten` command that runs in the checkout is written `.venv/bin/vecten …` and needs no
   wrapper. The form was tried outside the wrapper only (not yet tried against a live
   account).
5. `docker` and `ssh` are not allowed in the wrapper: local Docker work, such as
   `.venv/bin/vecten dev check-images`, needs no credential and runs without it, and so does
   `ssh` (see [A shell on a host](#a-shell-on-a-host)).
6. The credential home is closed to your own commands. The settings in it that are not secret
   come from `show`, which prints one value and nothing else:

   ```sh
   ~/vecten-live-tools/scripts/live/live show sandbox-org            # <sandbox-org>
   ~/vecten-live-tools/scripts/live/live show hostname <combination> # its domain
   ~/vecten-live-tools/scripts/live/live show oidc-client-id
   ~/vecten-live-tools/scripts/live/live show admin-email            # the Google account
   ~/vecten-live-tools/scripts/live/live show admin-firstname        # that signs in
   ```

   Exit 3 names what is missing; that is the maintainer's to add (setup items 6 and 10). None
   of these values goes into anything checked in.
7. `vecten deploy`, `vecten destroy`, `vecten github push` on a deployed project and
   `gh run watch` follow a workflow for many minutes, the publish run for up to three hours.
   Run them in the background and wait for their exit. Each run gets a GitHub token with at
   least 55 minutes left; a follow that outlives it stops on `HTTP 401` while the workflow
   runs on. Resume it with `gh run watch <run id> --exit-status` through `live run`, and
   rerun a `vecten deploy` whose follow was cut, since it applies nothing twice.

### Rules

1. Live actions in the test accounts and in the sandbox organisation need no approval. Record
   each one in the results file.
2. A run destroys what it created, without asking, and never anything else. The created file
   is the list of what the run may destroy: a resource whose project and environment it does
   not list is not the run's, whatever its name.
3. Over $60 estimated for the run, ask the maintainer and wait for the answer. What the run
   has spent so far is checked against the same ceiling at [Resuming](#resuming) and whenever
   the run stops to wait for the maintainer:

   ```sh
   ~/vecten-live-tools/scripts/live/live cost spent \
     --created .claude/live-checks-<version>.created.json
   ```

   Exit 3 (over the $60 ceiling) means ask the maintainer, with the figures, before
   continuing.
4. Never read or print a credential. A secret of the credential home reaches a command through
   the wrapper's `--stdin-file`; a key the run generates itself reaches it through a redirect;
   neither passes through your output.
5. Every cloud, GitHub and git command goes through the wrapper: `live run`, or
   `live candidate` and `live cost sweep`, which pass through it themselves.
6. Never push to `main` or to `release/*`, and never merge. Pushes to `vecten/vecten-open` are
   candidate branches and tags (`live candidate cut` makes them), branches of your own pull
   requests, and, once the release is published, the deletion of the run's candidate branches
   ([The release commit](#the-release-commit), step 7).
7. A candidate number is used once. A tag is never moved, deleted or pushed again.
8. Nothing you write to `vecten/vecten-open` (a commit, a pull request, an issue, a comment)
   and no evidence line you write into a checked-in file names an account, an account ID, a
   hostname or the sign-in address. A sandbox repository is different: while it exists it
   publishes what [Limits](#limits) lists under "Sandbox repositories are public": the
   project's domain, on a Snowflake combination the Snowflake account identifier, the sign-in
   address, the Google OAuth client ID and the bot's git identity. That is accepted for
   sandbox repositories only, which the run deletes at Destroy. The results file and `<run>`
   stay on the laptop.

### Start

1. If `<results>` exists, this is a resumed run: follow [Resuming](#resuming) instead of this
   list.
2. Check every identity:

   ```sh
   ~/vecten-live-tools/scripts/live/live doctor
   ```

   It must exit 0, its first line `tools ok: …` and the six after it `ok`. A `FAIL` line is
   the maintainer's to fix: send it to them and stop, with one exception: a `tools FAIL` line
   that says the command ran from the directory being worked in means it ran inside the tools
   copy; run it again from the checkout.
3. Fetch and stand on the release branch:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- git fetch origin --tags
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git switch --detach origin/release/<version>
   ```

4. Confirm CI is green on the release head:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     gh run list --repo vecten/vecten-open --branch release/<version> --limit 10 \
     --json workflowName,headSha,conclusion,url
   ```

   The newest run of each workflow on the head commit (what
   `~/vecten-live-tools/scripts/live/live run --github upstream -- git rev-parse HEAD` prints)
   has conclusion `success`. Otherwise tell the maintainer and stop: live checks run on a green
   head.
5. Create the run's files. `mkdir -p <run>/cli`, write `<created>` as
   `{"environments": []}`, and write `<results>` with the headings of
   [The results file](#the-results-file).
6. Sweep for leftovers before creating anything:

   ```sh
   ~/vecten-live-tools/scripts/live/live cost sweep \
     --created .claude/live-checks-<version>.created.json
   ```

   Exit 0 (`nothing left`): carry on. Exit 1: each `leftover:` line is something no
   environment of this run accounts for. It is not the run's: never destroy it. Copy the lines
   into the results file, send them to the maintainer, and carry on. Exit 4: a listing failed
   and the sweep could not look. That is not a pass: run it once more, and if it fails again,
   send the `could not list` lines to the maintainer and stop, since the run could not prove
   its own clean-up either. Exit 2: the created file is malformed; fix it.
7. Name the owed combinations:

   ```sh
   ~/vecten-live-tools/scripts/live/live combos plan
   ```

   The first line names the ledger and file map it read, the checkout's own under
   `scripts/live/`. Each line after it is an owed combination, the ledger tag it last ran at
   and the files changed since.
   You may add a combination and say why in the results file; dropping an owed one is the
   maintainer's decision. When `not owed` lists all four, the run has only the release's own
   list.
8. Price the run, 24 hours per owed combination:

   ```sh
   ~/vecten-live-tools/scripts/live/live cost estimate \
     --combinations <owed,comma,separated> --hours 24
   ```

   Exit 0: carry on. Exit 3 (over the $60 ceiling): ask the maintainer, with the figures, and
   wait. If the run will keep environments up longer than estimated, price it again with the
   longer hours before it gets there.
9. Write the plan and the estimate at the top of the results file, then send the maintainer one
   message with the plan, the estimate and the leftovers if any. Carry on without waiting.

### The release's own list

1. List the unticked tasks that need a real system:

   ```sh
   grep -n -E '^- \[ \]' .ai/openspec/changes/*/tasks.md
   ```

   Keep each task marked `live`, `[deploy]` or `[private repo]`, and any other whose text
   names behaviour of AWS, Scaleway, Snowflake, GitHub or a deployed host. Leave out tasks
   that build or specify something (they are not checks) and those an earlier evidence line
   says cannot run (a feature the test accounts lack).
2. List the fixes merged since the last tag. Pull requests are squash-merged into the release
   branch, so list its commits by first parent; each subject ends in its pull request number:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git log --first-parent --oneline v<previous>..origin/release/<version>
   ```

   For each pull request, read it
   (`live run --github upstream -- gh pr view <number> --repo vecten/vecten-open`) and keep the
   ones whose proof needs a real system: a unit test cannot show it.
3. Write one results row per check, with no result yet, grouped by the combination it runs
   on. A check that needs something the test accounts do not have (a private extensions
   repository, a chat app) gets BLOCKED with that reason at once.
4. A check owed on a combination `live combos plan` did not name adds that combination: estimate
   again (Start, step 8) with it.

### Candidate

1. Open fixes go into the candidate: a fix of this run's findings that the maintainer has not
   merged yet. List them:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     gh pr list --repo vecten/vecten-open --base release/<version> --state open \
     --json number,headRefName,title
   ```

2. Cut the next candidate, with `--with origin/<branch>` for each fix branch of step 1 that
   the candidate needs (none on a first candidate):

   ```sh
   ~/vecten-live-tools/scripts/live/live candidate cut --version <version> \
     [--with origin/<branch> …]
   ```

   It cuts the candidate in the checkout it runs in and prints one JSON line with
   `candidate`, `branch`, `tag`, `commit` and `pushed: true`, and leaves the checkout on
   `rc/<candidate>`; switch back with
   `live run --github upstream -- git switch --detach origin/release/<version>`. Exit 3 names
   the step that failed and has undone the local branch and tag. If the message ends in
   "branch rc/<candidate> is already on origin", the tag was not pushed: tell the maintainer
   and stop. That number is spent, and a later cut takes the one after it.
3. Pushing the tag starts the `publish` workflow. Find its run and follow it:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     gh run list --repo vecten/vecten-open --workflow publish.yml --branch v<candidate> \
     --json databaseId,status,conclusion,url --limit 1
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     gh run watch <databaseId> --repo vecten/vecten-open --exit-status
   ```

   Its jobs: `target` checks the tag against the tree, `packages` publishes to TestPyPI,
   `images` asks the registry before each image's push and builds and pushes only an image
   whose tag is absent, and `verify` installs the published packages and runs
   `vecten dev check-images` on the two images. `verify` inspects the images without a login,
   so a package that is not public stops the run at `verify`. If the `images` job's check
   fails with `refused: inspecting … failed` although the registry answered, its "not found"
   wording differs from what the check expects: the run fails without pushing anything. That
   is a finding to report, with the message (not yet tried against a live account). The run
   registers a few seconds after the push: until `gh run list` prints a run for
   `v<candidate>`, wait and list again.
   `gh run watch` exit 0: published. Otherwise read the failed step, with
   `live run --github upstream -- gh run view <databaseId> --repo vecten/vecten-open --log-failed`.
   A failure that needs no change to the tree (the index or the registry was unreachable, a
   runner died) is rerun with
   `live run --github upstream -- gh run rerun <databaseId> --repo vecten/vecten-open --failed`:
   the workflow is built for that, skipping packages the index already holds and never
   pushing over a published image tag, since the `images` job asks the registry again on every
   attempt (this rests on the test of setup item 7). A version left with one image pushed and
   the other missing is mended the same way, for a candidate or a release: the rerun pushes the
   missing image and leaves the existing one untouched. A failure that needs any change is a
   [finding](#findings), and its fix is cut as the next candidate number. A tag is never moved,
   deleted or pushed again (not yet tried against a live account).
4. Confirm both images hold both platforms. This needs the Docker daemon running and no
   credential:

   ```sh
   .venv/bin/vecten dev check-images --version <candidate>
   ```

   It exits 0. If Claude Code's command sandbox refuses the Docker socket, send the maintainer
   the line and record the hand-off.

5. Install the published candidate in its own environment, as the `publish` workflow's
   verification does:

   ```sh
   ~/vecten-live-tools/scripts/live/live run -- \
     uv venv --no-config --python 3.14 <run>/cli/<candidate>
   ~/vecten-live-tools/scripts/live/live run -- uv pip install --no-config \
     --python <run>/cli/<candidate> \
     --find-links https://test.pypi.org/simple/vecten/ \
     --find-links https://test.pypi.org/simple/vecten-shared/ \
     --find-links https://test.pypi.org/simple/vecten-platform/ \
     --find-links https://test.pypi.org/simple/vecten-mcp/ \
     vecten==<candidate> vecten-shared==<candidate> \
     vecten-platform==<candidate> vecten-mcp==<candidate>
   <run>/cli/<candidate>/bin/vecten version
   ```

   The last line prints `vecten <candidate>`. Record steps 2 to 5 as rows.

### One combination, start to finish

The standing regression, run for each owed combination; the release's own checks for that
combination run on its environment as it comes up. The first owed combination of a release
starts from `<previous>` and upgrades (step 12); the others start from the candidate.

1. **Values.** The domain is what `show hostname <combination>` prints (How commands run,
   item 6), and its zone is the domain without its first label; `<sandbox-org>` is what
   `show sandbox-org` prints. Then the values of the cloud:

   - region: on AWS,
     `~/vecten-live-tools/scripts/live/live run -- aws configure get region`; on Scaleway,
     `fr-par`;
   - profile: `standard` on AWS, `minimal` on Scaleway (the profiles `live cost` priced);
   - on Snowflake, the account's `<ORG-ACCOUNT>` identifier:

     ```sh
     ~/vecten-live-tools/scripts/live/live run -- snow sql -c livecheck --format json \
       -q "select current_organization_name() || '-' || current_account_name()"
     ```

2. **Render.** `<cli>` is `<run>/cli/<candidate>`, or `<run>/cli/<previous>` on the upgrade
   path. For `<previous>`, install it first as in Candidate step 5, from PyPI, so without the
   `--find-links` lines and with `<previous>` in every pin. Then render the project into
   `<project>`:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --cwd <run> -- \
     uv run --no-project --python <cli>/bin/python \
     vecten init <combination> --name <name> --users 5 --source placeholder \
     --components platform,mcp --size small --profile <profile> --cloud <cloud> \
     --region <region> --warehouse <postgres|snowflake> --domain <domain> --dns-zone <zone> \
     --github-repository <sandbox-org>/<name> [--snowflake-account <ORG-ACCOUNT>]
   ```

   It prints `rendered N files into <project>` with no "deploy layer not rendered". On
   Snowflake the two combinations share one account, so give the project its own names:
   `vecten answer snowflake_shared_account yes`, then `vecten init --deploy`; `vecten.toml`
   then holds `shared_account = true` under `[warehouse]` (not yet tried against a live
   account).
3. **Candidate workflows.** On the candidate path:

   ```sh
   ~/vecten-live-tools/scripts/live/live project candidate <project> <candidate>
   ```

   It exits 0 and the workflows install `vecten==<candidate>` with the TestPyPI
   `--find-links`. On the upgrade path skip it now; it follows `vecten update` in step 12.
4. **Check before it goes public.** Stage the project and check it:

   ```sh
   ~/vecten-live-tools/scripts/live/live run -- git -C <project> init -b main
   ~/vecten-live-tools/scripts/live/live run -- git -C <project> add --all
   ~/vecten-live-tools/scripts/live/live project check-public <project>
   ```

   The check looks at everything a push could publish: every path a file had in any commit a
   branch, tag or HEAD reaches, what is staged, and what `git add --all` would stage, including
   untracked files the project's own ignore files do not ignore. `<project>` is the project's
   top level; a subdirectory is refused (exit 3). Exit 0, printing nothing, is a pass. Exit 1
   lists each path that holds a secret, by name or by value, once, with where it was found (in
   the working tree, staged, in a commit), and never the value. One found in the working tree
   or staged: take it out of the index and out of the project, and never push it. One found in
   a commit: removing the file is not enough, since the push would publish the commit too; the
   history has to be rewritten, or the project rendered afresh in a new directory, before
   anything is pushed.
   Exit 3 names why the check could not run, a git command that failed included. The
   repository is public from step 6 on, so run the last two commands again before every later
   `vecten github push` (steps 9, 10, 12 and 15), and push only after exit 0.
5. **Record the environment.** Before the first thing that leaves the laptop, add an entry to
   `environments` in `<created>`:

   ```json
   {"combination": "<combination>", "project": "<name>", "env": "prod",
    "repository": "<sandbox-org>/<name>", "created_at": "<now>", "destroyed_at": null}
   ```

   where `<now>` is `date -u +%Y-%m-%dT%H:%M:%S+00:00`. `live cost` reads `combination`,
   `project`, `env`, `created_at` and `destroyed_at`; `repository` is for you.
6. **Repository.** Create it, then make it public, since a free organisation gives environments
   to public repositories only:

   ```sh
   vecten github init
   ~/vecten-live-tools/scripts/live/live run -- \
     gh repo edit <sandbox-org>/<name> --visibility public --accept-visibility-change-consequences
   ```

   `github init` prints that it pushed, and
   `live run -- gh repo view <sandbox-org>/<name> --json visibility` says `PUBLIC` (not yet
   tried against a live account).
7. **Bootstrap.** `vecten bootstrap --env prod`. It prints the state bucket, the identities and
   the GitHub environment it prepared. On Scaleway it finds the project holding the zone
   itself, which rests on the zone setup of setup item 4 (not yet tried against a live
   account).
8. **Snowflake deploy user.** On Snowflake only. The environment gets a deploy user of its own,
   as `docs/deploy-aws.md` (step 4) and the Snowflake section of the project's
   `docs/runbook.md` describe: a key-pair service user whose default role holds CREATE
   DATABASE, CREATE WAREHOUSE, CREATE ROLE, CREATE USER and MANAGE GRANTS on the account, and
   nothing else, on a key-pair-only sign-in policy. Never deploy as the `livecheck`
   connection's user: it is ACCOUNTADMIN, and would hide the permission mistakes the check
   exists to find. `<NAME>` is `<name>` in upper case with its hyphens as underscores.
   1. A key pair, inside the project's `.vecten/`, which the project's `.gitignore` keeps out
      of git; the private key's `.key` name makes `check-public` refuse it if it is ever
      staged, committed, or no longer ignored:

      ```sh
      mkdir -p <project>/.vecten/deploy
      openssl genrsa 2048 | openssl pkcs8 -topk8 -nocrypt -out <project>/.vecten/deploy/deploy.key
      openssl rsa -in <project>/.vecten/deploy/deploy.key -pubout \
        -out <project>/.vecten/deploy/deploy.pub
      ```

   2. Write `<run>/<combination>-deploy-user.sql`, with the public key as one line without
      its `-----BEGIN` and `-----END` lines (`grep -v -- ----- deploy.pub | tr -d '\n'`), and
      run it as ACCOUNTADMIN through the wrapper (not yet tried against a live account):

      ```sql
      USE ROLE ACCOUNTADMIN;
      CREATE ROLE DEPLOY_<NAME>_PROD COMMENT = 'live check <name>';
      GRANT CREATE DATABASE, CREATE WAREHOUSE, CREATE ROLE, CREATE USER, MANAGE GRANTS
        ON ACCOUNT TO ROLE DEPLOY_<NAME>_PROD;
      CREATE USER DEPLOY_<NAME>_PROD TYPE = SERVICE DEFAULT_ROLE = DEPLOY_<NAME>_PROD
        RSA_PUBLIC_KEY = '<public key>' COMMENT = 'live check <name>';
      GRANT ROLE DEPLOY_<NAME>_PROD TO USER DEPLOY_<NAME>_PROD;
      CREATE DATABASE ADMIN_<NAME>_PROD COMMENT = 'live check <name>: deploy sign-in policy';
      CREATE AUTHENTICATION POLICY ADMIN_<NAME>_PROD.PUBLIC.DEPLOY_SIGN_IN
        AUTHENTICATION_METHODS = ('KEYPAIR');
      ALTER USER DEPLOY_<NAME>_PROD
        SET AUTHENTICATION POLICY ADMIN_<NAME>_PROD.PUBLIC.DEPLOY_SIGN_IN;
      ```

      ```sh
      ~/vecten-live-tools/scripts/live/live run -- \
        snow sql -c livecheck -f <run>/<combination>-deploy-user.sql
      ```

      Add `"snowflake": ["USER DEPLOY_<NAME>_PROD", "ROLE DEPLOY_<NAME>_PROD",
      "DATABASE ADMIN_<NAME>_PROD"]` to the environment's entry in `<created>`.
   3. Store the user and its key, as the project's runbook says, the key by a redirect from
      its file. Write `DEPLOY_<NAME>_PROD` alone to `<project>/.vecten/deploy/user`, then:

      ```sh
      vecten secret set DEPLOY_SNOWFLAKE_USER --env prod < <project>/.vecten/deploy/user
      vecten secret set DEPLOY_SNOWFLAKE_PRIVATE_KEY --env prod \
        < <project>/.vecten/deploy/deploy.key
      ```

      `vecten secret set` reads the value from standard input when that is not a terminal and
      drops one final newline. Each prints `stored <NAME> in the prod secrets store`.

   On every cloud, `vecten next` names any other secret the project declares.
9. **Push and deploy.** After the check of step 4, `vecten github push`, then
   `vecten deploy --env prod`. Deploy dispatches
   the apply workflow and follows it; it exits 0 when Terraform applied and every host shipped.
   Then `vecten github push` again, to push the `deployed_commit` deploy recorded. A failed
   run is a finding: its URL goes in the row. On Snowflake the first deploy ships no host until
   an ACCOUNTADMIN has created the MCP's identity (`docs/mcp-identity.md`, "A Snowflake
   warehouse the platform builds"). Print the script, run it, record the run and push:

   ```sh
   vecten mcp provisioner-script --env prod > <project>/.vecten/provisioner.sql
   ~/vecten-live-tools/scripts/live/live run -- \
     snow sql -c livecheck -f <project>/.vecten/provisioner.sql
   vecten mcp provisioner-script --env prod --confirm-run
   vecten github push
   ```

   In the full form the `>` goes at the end of the wrapper line; the wrapper and `uv` print
   nothing on standard output, so the file holds only the SQL and its `-- then:` comments. The
   push ships every host (not yet tried against a live account).
10. **Sign-in setup.** Run `vecten next --json` and answer its items for `prod`, running it
    again after each:

    - "Which OIDC issuer and client id": the client id is what `show oidc-client-id` prints.

      ```sh
      vecten answer mcp.oidc_issuer https://accounts.google.com
      vecten answer mcp.oidc_client_id <client id>
      ```

    - "Who is the first admin": `vecten answer mcp.admins <address>`, where `<address>` is what
      `show admin-email` prints.
    - "Register … /oidc/callback": the address is registered already (setup item 10). Store the
      client secret from the credential home through the wrapper, then confirm:

      ```sh
      ~/vecten-live-tools/scripts/live/live run --cwd <project> \
        --stdin-file google/oidc-client-secret -- \
        uv run --no-project --python <cli>/bin/python \
        vecten secret set MCP_OIDC_CLIENT_SECRET --env prod
      vecten answer oidc_registered.prod yes
      ```

      The first command prints `stored MCP_OIDC_CLIENT_SECRET in the prod secrets store` (not
      yet tried against a live account).
    - "Push the project's changes": the check of step 4, then `vecten github push`, which
      waits for the ship.
    - "Sign in to the MCP as an admin": goes into [the sign-in sitting](#the-sign-in-sitting),
      step 13; so does every item that follows it, such as "Who should have access first".

    An item this list does not name is a gap in this page: record it, answer it from what its
    own text says if that is a fact you have, and otherwise put it in the sign-in sitting.
11. **One pipeline run.** The `placeholder` source never syncs, so trigger `dbt_build` by
    hand. On AWS, through SSM ([A shell on a host](#a-shell-on-a-host)), on the platform host:

    ```sh
    sudo docker exec $(sudo docker ps --filter name=scheduler --format {{.Names}} | head -1) \
      airflow dags trigger dbt_build
    ```

    then the same with `airflow dags list-runs dbt_build` in place of the trigger, until the
    run is `success`. On Scaleway, which has no run command, the trigger goes into the sign-in
    sitting and you read the run in the shell overlay. Either way `vecten issues --env prod`
    lists no failure of `dbt_build` (not yet tried against a live account).
12. **Upgrade path only.** After step 11 passes on `<previous>`, switch the project to the
    candidate CLI: from here on, `<cli>` is `<run>/cli/<candidate>`. Then:

    ```sh
    vecten update
    ~/vecten-live-tools/scripts/live/live project candidate <project> <candidate>
    # Run the check of step 4 before pushing.
    ```

    For an existing environment with a bound login route (including a container app), the
    upgrade must grant the setup host access to `MCP_GATE_DB_PASSWORD` before its first new
    ship. On Scaleway run `vecten bootstrap --env prod` now to update the host key's policy.
    On either cloud, pause the automatic push-triggered ship while updating the default branch.
    Run from the project checkout:

    ```sh
    gh workflow disable ship.yml
    vecten github push --no-wait
    vecten deploy --env prod
    gh workflow enable ship.yml
    ```

    Deploy applies the new infrastructure permissions and ships that commit. Re-enable the
    push workflow after a successful deploy. Environments without a bound login route use the
    ordinary sequence:

    ```sh
    vecten github push
    vecten deploy --env prod
    ```

    `update` prints `updated <previous> → <candidate>` and the files it changed. If a later
    candidate replaces this one, run `live project release <project>` before the next
    `vecten update`: update refuses workflows carrying the TestPyPI
    patch. If `update` or the ship reports a lock pinning the old version, run `vecten lock` in
    the project form with `--env-file <run>/testpypi.env` after `--no-project`, where that
    file holds one line, `UV_FIND_LINKS=` followed by the four TestPyPI addresses of Candidate
    step 5, comma-separated (not yet tried against a live account). The plan of the deploy and
    the result of every check that follows are the upgrade's evidence.
13. **The sign-in sitting.** As the next section says. Once `vecten login --complete` reports
    `authenticated: true`, answer the `vecten next` items that follow it for `prod`, as in step
    10. "Who should have access first":
    `vecten user add --email <address> --name <first name> --env prod`, with the address
    `show admin-email` prints and the first name `show admin-firstname` prints.
14. **A query through the MCP.** In the sitting, the maintainer asks one question through their
    AI chat interface connected to the MCP, such as "Which datasets can you see?"; their
    answer is the evidence.
15. **A push that ships.** Change one line of `README.md` in the project, run the check of
    step 4, then `vecten github push -m "live: ship check"`. It exits 0 once the ship it
    started succeeds.
16. **The release's own checks** for this combination, each with its row. A check that needs
    a shell on a Scaleway host waits for step 17.
17. **The shell overlay** on Scaleway, last, as [A shell on a host](#a-shell-on-a-host) says.
18. **Destroy**, as [Destroy](#destroy) says, once every check on this environment has its
    result and no open decision needs the environment.

### The sign-in sitting

1. Collect everything on one environment that needs a signed-in person: the `vecten login`
   device code, the query through the MCP, on Scaleway the `dbt_build` trigger, and every check
   of the release's own list that needs a browser (a gated app, a cookie read in the browser's
   developer tools).
2. Just before sending, run `vecten login --env prod --json`: it prints `verification_uri` and
   `user_code`.
3. Send the maintainer one message, steps numbered, each with what to send back:

   ```text
   Sign-in sitting for <combination> (<candidate>), about <minutes> minutes.
   1. Open <verification_uri>, enter <user_code>, sign in with Google. Send back: done.
   2. In your AI chat interface, connect the MCP at <the URL `vecten mcp connection --env prod`
      prints> and ask: "<question>". Send back: the answer's first lines.
   3. (Scaleway) Open https://airflow.<domain>, trigger dbt_build. Send back: done.
   4. <each browser check: what to open, what to look at, what to send back>
   ```

4. Carry on meanwhile with checks that need no person, on this environment or another.
5. When the answer comes: `vecten login --env prod --complete --json` prints
   `authenticated: true`. If the device code lapsed first, run step 2 again and send only the
   new code. Record each step's answer in its row.

### A shell on a host

1. **AWS, through SSM.** Find the instances and run a command on one:

   ```sh
   ~/vecten-live-tools/scripts/live/live run -- aws ec2 describe-instances --region <region> \
     --filters Name=tag:vecten:project,Values=<name> Name=instance-state-name,Values=running \
     --query "Reservations[].Instances[].[InstanceId, Tags[?Key=='Name'] | [0].Value]" --output text
   ~/vecten-live-tools/scripts/live/live run -- aws ssm send-command --region <region> \
     --instance-ids <instance id> --document-name AWS-RunShellScript \
     --parameters 'commands=["<command>"]' --query Command.CommandId --output text
   ~/vecten-live-tools/scripts/live/live run -- aws ssm get-command-invocation --region <region> \
     --command-id <command id> --instance-id <instance id> \
     --query "[Status, StandardOutputContent, StandardErrorContent]" --output text
   ```

   Repeat the last command while the status is `Pending` or `InProgress`
   (not yet tried against a live account).
2. **Scaleway, last.** A Scaleway host has no SSH access and no run-command service, so this
   overlay comes only after every other check on that environment has its result, and the
   destroy follows it:
   1. A key pair for this run:
      `mkdir -p <run>/ssh && ssh-keygen -t ed25519 -N "" -C <name> -f <run>/ssh/<combination>`.
   2. The resources project, its server, zone, public address and security group:

      ```sh
      ~/vecten-live-tools/scripts/live/live run -- scw account project list -o json
      ~/vecten-live-tools/scripts/live/live run -- \
        scw instance server list project-id=<resources project id> zone=all -o json
      ```

      The resources project is the one whose description is `vecten:resources:<name>:prod`;
      the server's JSON holds `id`, `zone`, `public_ip.address` and `security_group.id`.
   3. The key and the firewall rule, then a reboot, at which the server takes the project's
      keys:

      ```sh
      ~/vecten-live-tools/scripts/live/live run -- scw iam ssh-key create name=<name> \
        public-key=@<run>/ssh/<combination>.pub project-id=<resources project id> -o json
      ~/vecten-live-tools/scripts/live/live run -- scw instance security-group create-rule \
        security-group-id=<security group id> protocol=TCP direction=inbound action=accept \
        ip-range=<your public address>/32 dest-port-from=22 dest-port-to=22 zone=<zone>
      ~/vecten-live-tools/scripts/live/live run -- \
        scw instance server reboot <server id> zone=<zone>
      ~/vecten-live-tools/scripts/live/live run -- scw instance server wait <server id> zone=<zone>
      ```

      Write the key's `id` in the results file: the destroy removes it. Your public address is
      what `curl -s https://checkip.amazonaws.com` prints; the settings allow that domain, so
      it runs in the command sandbox without a prompt.
   4. The shell, outside the wrapper:

      ```sh
      ssh -F none -i <run>/ssh/<combination> -o IdentitiesOnly=yes \
        -o UserKnownHostsFile=<run>/ssh/known_hosts -o StrictHostKeyChecking=accept-new \
        root@<public address> '<command>'
      ```

      The first `ssh` to each host raises a permission prompt for the maintainer, once per
      host; record it in Hand-offs.
   5. Run the checks, record them, then go straight to [Destroy](#destroy).

   (Not yet tried against a live account.)

### Findings

1. A check that fails is a finding. Reproduce it once.
2. Write its row: FAIL, the command, the key output or run URL, and what you expected.
3. Propose "blocks" (the release waits for its fix) or "follow-up" (an issue for later), and
   tell the maintainer at once, in one message: the check, what happened, your proposal.
4. While the decision is open, do only work that stands either way:
   - checks the finding does not touch;
   - the fix, on its own branch from `origin/release/<version>`, with the failing test first,
     as a pull request against `release/<version>`:

     ```sh
     ~/vecten-live-tools/scripts/live/live run --github upstream -- \
       git switch --create fix/<short-name> origin/release/<version>
     # the failing test, then the fix; the unit tests and ruff of AGENTS.md pass, run as
     # .venv/bin/pytest … and .venv/bin/ruff … in place of uv run pytest and uv run ruff
     ~/vecten-live-tools/scripts/live/live run --github upstream -- git add <files>
     ~/vecten-live-tools/scripts/live/live run --github upstream -- \
       git commit -m "fix(<scope>): <what>"
     ~/vecten-live-tools/scripts/live/live run --github upstream -- git push origin fix/<short-name>
     ~/vecten-live-tools/scripts/live/live run --github upstream -- \
       gh pr create --repo vecten/vecten-open --base release/<version> --head fix/<short-name> \
       --title "fix(<scope>): <what>" --body "<the finding, the failing test, the fix>"
     ```

     The exception is a fix that adds or changes a file under `.github/workflows/`: the App
     holds no Workflows permission, so GitHub refuses its push. Commit it as above, skip the
     push and the pull request, write the commit to a patch, and send the maintainer its path
     with the pull request's title and body; they push the branch and open the pull request.
     Record the hand-off.

     ```sh
     ~/vecten-live-tools/scripts/live/live run --github upstream -- \
       git format-patch -1 --stdout > <run>/fix-<short-name>.patch
     ```

5. Not while it is open: checks downstream of the finding, a new candidate, destroying an
   environment the fix would be retested on, the ticks, the release commit.
6. When nothing of the first kind is left, run `live cost spent` (Rules, item 3), then stop and
   tell the maintainer that the run waits for their decision.
7. After a blocking fix: cut the next candidate with `--with origin/fix/<short-name>`, and move
   each environment still up to it as One combination step 12 does, with
   `live project release <project>` before `vecten update`. A fix that touches
   `.github/workflows/` reaches a candidate only once the maintainer has merged it into
   `release/<version>`: cut the next candidate without its `--with`, since the App's push of a
   candidate branch that merges it is refused too (not yet tried against a live account). A
   result recorded on the earlier candidate stands unless the fix changed a file its check
   exercises. To see which:

   ```sh
   ~/vecten-live-tools/scripts/live/live combos affected v<earlier candidate> v<candidate>
   ```

   It compares the trees beneath the two candidates' bump commits, so the version files every
   bump changes do not count. The first line names the file map it read; each line after it
   names a combination and the changed files that touch it; the last, `untouched:`, the
   combinations nothing touches. Rerun the checks of the
   combinations it names, and only those; when it names none (`untouched:` lists all four),
   nothing reruns live.
8. A follow-up: the maintainer opens the issue, or you open it on their word with
   `live run --github upstream -- gh issue create --repo vecten/vecten-open`; its link goes in
   the row's note.

### Destroy

For each environment of `<created>` whose `destroyed_at` is `null`, once its checks are done.
`vecten` commands are the project form. Every `aws` and `scw` line in steps 4 and 5 runs
through the wrapper: put `~/vecten-live-tools/scripts/live/live run --` in front of it.

1. On Snowflake, note `snowflake_base` under `[environments.prod]` in `<project>/vecten.toml`
   first: destroy clears it. Call it `<BASE>`.
2. Destroy the infrastructure and the data:

   ```sh
   vecten destroy --env prod --confirm prod --delete-data prod --json
   ```

   It prints `state_bucket` among its fields; note it. On Snowflake it may refuse with nothing
   destroyed and print the statements an ACCOUNTADMIN runs. Run them, then destroy again:

   ```sh
   ~/vecten-live-tools/scripts/live/live run -- \
     snow sql -c livecheck -q "USE ROLE ACCOUNTADMIN; <the statements, in order>"
   ```

3. On Snowflake, drop the deploy user of One combination step 8, now that destroy no longer
   needs it, then delete its key pair (`rm -r <project>/.vecten/deploy`):

   ```sh
   ~/vecten-live-tools/scripts/live/live run -- snow sql -c livecheck -q \
     "USE ROLE ACCOUNTADMIN;
      ALTER USER IF EXISTS DEPLOY_<NAME>_PROD UNSET AUTHENTICATION POLICY;
      DROP USER IF EXISTS DEPLOY_<NAME>_PROD; DROP DATABASE IF EXISTS ADMIN_<NAME>_PROD;
      DROP ROLE IF EXISTS DEPLOY_<NAME>_PROD;"
   ```

   Then confirm nothing of the environment stays outside Terraform:

   ```sh
   ~/vecten-live-tools/scripts/live/live run -- snow sql -c livecheck -q \
     "USE ROLE ACCOUNTADMIN; SHOW DATABASES LIKE '%<BASE>%'; SHOW ROLES LIKE '%<BASE>%';
      SHOW USERS LIKE '%<BASE>%'; SHOW WAREHOUSES LIKE '%<BASE>%';
      SHOW SECURITY INTEGRATIONS LIKE '%<BASE>%'; SHOW USERS LIKE '%<NAME>%';
      SHOW ROLES LIKE '%<NAME>%'; SHOW DATABASES LIKE '%<NAME>%';"
   ```

   Every listing is empty. A row whose name ends in `<BASE>_PROD` or `<NAME>_PROD` is this
   run's: drop it with `DROP <kind> IF EXISTS <name>;` and list again. Any other row is not the
   run's (not yet tried against a live account).
4. On AWS, remove what bootstrap made, with `<account>` from
   `aws sts get-caller-identity --query Account --output text`:

   ```sh
   # The environment's secrets
   aws secretsmanager list-secrets --region <region> \
     --filters Key=name,Values=vecten/<name>/prod/ --query "SecretList[].Name" --output text
   aws secretsmanager delete-secret --region <region> --secret-id <secret> \
     --force-delete-without-recovery
   # The state bucket, versioned, so every version goes first
   aws s3api list-object-versions --bucket <state bucket> --output json \
     --query "{Objects: [Versions, DeleteMarkers][].{Key: Key, VersionId: VersionId}}" \
     > <run>/<combination>-versions.json
   aws s3api delete-objects --bucket <state bucket> \
     --delete file://<run>/<combination>-versions.json
   aws s3api delete-bucket --bucket <state bucket>
   # The deploy and plan roles: for <role> in <prefix>-deploy and <prefix>-plan
   aws iam list-attached-role-policies --role-name <role> \
     --query "AttachedPolicies[].PolicyArn" --output text
   aws iam detach-role-policy --role-name <role> --policy-arn <policy arn>
   aws iam list-role-policies --role-name <role> --query "PolicyNames" --output text
   aws iam delete-role-policy --role-name <role> --policy-name <policy name>
   aws iam delete-role --role-name <role>
   # The permissions boundary
   aws iam list-policy-versions --policy-arn arn:aws:iam::<account>:policy/<prefix>-boundary \
     --query "Versions[?!IsDefaultVersion].VersionId" --output text
   aws iam delete-policy-version --policy-arn arn:aws:iam::<account>:policy/<prefix>-boundary \
     --version-id <version>
   aws iam delete-policy --policy-arn arn:aws:iam::<account>:policy/<prefix>-boundary
   ```

   Run `delete-secret`, `detach-role-policy`, `delete-role-policy` and `delete-policy-version`
   once per name the listing before them prints; skip `delete-objects` when `Objects` is
   empty, and repeat the list and delete when the listing held more than 1000 versions. Leave
   the account's GitHub OIDC provider: every AWS environment of the account shares it
   (not yet tried against a live account).
5. On Scaleway, remove what bootstrap made. `<resources id>` and `<state id>` are the projects
   whose descriptions are `vecten:resources:<name>:prod` and `vecten:state:<name>:prod`
   (`scw account project list -o json`):

   ```sh
   # Secrets in the resources project
   scw secret secret list project-id=<resources id> region=all -o json
   scw secret secret delete <secret id> region=<region>
   # The overlay's SSH key, if the run added one
   scw iam ssh-key delete <ssh key id>
   # Each application named <prefix>-deploy, -plan, -platform, -mcp or -main
   scw iam application list -o json
   scw iam api-key list bearer-id=<application id> -o json
   scw iam api-key delete <access key>
   scw iam policy list application-ids.0=<application id> -o json
   scw iam policy delete <policy id>
   scw iam application delete <application id>
   # The state bucket, vecten-<name>-prod-state-<first 8 characters of the state id>
   scw object bucket delete <state bucket> region=<region>
   # The two projects
   scw account project delete project-id=<resources id>
   scw account project delete project-id=<state id>
   ```

   If a project refuses deletion because it still holds something, the error names it: list
   that kind with `scw <product> … list project-id=<id> -o json`, delete what the list shows,
   and write the commands into this page in the run's pull request
   (not yet tried against a live account).
6. Delete the repository:
   `~/vecten-live-tools/scripts/live/live run -- gh repo delete <sandbox-org>/<name> --yes`.
7. Set the entry's `destroyed_at` in `<created>` to `<now>`. Leave `<project>` on the laptop:
   it holds the evidence.
8. Sweep:

   ```sh
   ~/vecten-live-tools/scripts/live/live cost sweep \
     --created .claude/live-checks-<version>.created.json
   ```

   Exit 0 proves the removal. Cloud APIs can list a just-destroyed resource for a few minutes,
   so on an exit 1 right after a destroy, wait five minutes and sweep once more before you
   treat a line as a leftover. Exit 1: a `leftover:` line whose project and environment are an
   entry of `<created>` is the run's; remove it with the matching command above, then sweep
   again. A line of any other project is not the run's: never remove it. An exit 1 whose every
   line also appeared in the Start sweep's output counts as clean for this run, and those lines
   go in the report; a line that did not appear there is new, and you report it to the
   maintainer at once. Exit 4: the sweep could not look, which is not a pass; run it
   again, and report the `could not list` lines if it persists. The sweep does not look at
   Snowflake: step 3 is that check.

### Finish

1. Record the ledger and tick the specs on one branch:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git switch --create docs/live-checks-<version> origin/release/<version>
   ~/vecten-live-tools/scripts/live/live combos record <combination> v<candidate>
   ```

   `record` runs once for each combination whose standing regression passed in full, with the
   candidate it last passed on. It writes the checkout's own `scripts/live/ledger.toml`, and
   refuses where there is none; it prints `ledger <that file>`, then
   `<combination> = v<candidate>`.
2. For each passed check, tick its task in `.ai/openspec/changes/<change>/tasks.md` and add one
   evidence line under it, in the form the file already uses:
   `  - Live, <candidate> on <cloud>: <what was seen>.` A check that passed on one cloud of two
   stays unticked, with `  - Partly run live for <version> (<candidate>, <cloud>): …`. No
   account, account ID or hostname goes in.
3. Commit, push and open the pull request:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git add scripts/live/ledger.toml .ai/openspec/changes
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git commit -m "docs(specs): live checks for <version>"
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git push origin docs/live-checks-<version>
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     gh pr create --repo vecten/vecten-open --base release/<version> \
     --head docs/live-checks-<version> --title "docs(specs): live checks for <version>" \
     --body "<one line per check: task, result, candidate>"
   ```

4. Price what the run spent:

   ```sh
   ~/vecten-live-tools/scripts/live/live cost spent \
     --created .claude/live-checks-<version>.created.json
   ```

   Exit 3 means the run went over the $60 ceiling: say so in the report.
5. Send the maintainer the report: the results (counts of PASS, FAIL and BLOCKED, then each
   FAIL and BLOCKED row), the decisions still open, the `spent` total, anything still running
   (every entry of `<created>` without `destroyed_at`, and why), every hand-off of the run with
   its cause, and the pull requests opened.

### The release commit

Only once no decision is open and the maintainer has merged the fixes and the ticks:

1. Branch from the release branch:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- git fetch origin
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git switch --create chore/release-<version> origin/release/<version>
   ```

2. In `CHANGELOG.md`, move the entries under `## [Unreleased]` into a new
   `## [<version>] - <YYYY-MM-DD>` below it, leaving `## [Unreleased]` empty, and update the
   compare links at its foot: `[Unreleased]` compares `v<version>...HEAD`, and a new
   `[<version>]` line compares `v<previous>...v<version>`.
3. Bump the version and confirm the lock:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     uv run vecten dev bump-version <version>
   ~/vecten-live-tools/scripts/live/live run --github upstream -- uv sync --locked --all-packages
   ```

4. Stage the changed tracked files, commit, push and open the pull request:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- git add --update
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git commit -m "chore(release): <version>"
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git push origin chore/release-<version>
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     gh pr create --repo vecten/vecten-open --base release/<version> \
     --head chore/release-<version> --title "chore(release): <version>" \
     --body "<the changelog's entries for <version>>"
   ```
5. After the maintainer merges it, open the pull request from `release/<version>` to `main`:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     gh pr create --repo vecten/vecten-open --base main --head release/<version> \
     --title "Release <version>" --body "<the changelog's entries for <version>>"
   ```

6. The rest is the maintainer's: they merge, push the tag `v<version>` and approve the
   `publish` run in the `release` environment (CONTRIBUTING.md, Releasing). A release takes
   one approval, for the packages; the images build after it and can take long, up to three
   hours; the release is done when the run's `verify` job is green.
7. Once the release is published, delete the run's candidate branches on
   `vecten/vecten-open`, one for each candidate the Run block lists; the candidate tags stay:

   ```sh
   ~/vecten-live-tools/scripts/live/live run --github upstream -- \
     git push origin --delete rc/<candidate>
   ```

   `rc/` branches are made by the release tooling, never merged, and deleted after the release
   (CONTRIBUTING.md, Branches).

## The results file

`<results>` is the run's local record; `.claude/` is ignored by git, so it never leaves the
laptop. It holds, in this order:

1. **Run**: the version, the candidates cut (tag, commit, publish run URL), `<previous>`, and
   the plan and estimate from Start.
2. **Results**: one table, one row per check, in the order the checks ran:

   ```text
   | Check | Result | Command | Evidence | Note |
   |---|---|---|---|---|
   | aws-postgres: deploy | PASS | `vecten deploy --env prod` | apply run <URL>, exit 0 | rc1 |
   ```

   `Check` names the combination and the step, or the change and task number
   (`extension-services 4.8`), or the pull request (`#123`). `Result` is PASS, FAIL or
   BLOCKED; a row without one is a check still to run. `Evidence` holds the key lines of the
   output or the run's URL; a BLOCKED row says what it waits for in `Note`. A rerun on a later
   candidate is a new row, never an edit of the old one.
3. **Decisions**: each finding, the proposal, the maintainer's answer when it comes.
4. **Hand-offs**: every time the run needed the maintainer for something other than the
   sign-in sitting or a finding's decision, with its cause.

`<created>` is the second file: `{"environments": [...]}`, one entry per environment as
[One combination](#one-combination-start-to-finish) step 5 shows, written when the
environment is recorded and given `destroyed_at` when Destroy step 7 is reached.

### Resuming

A new session starts here when `<results>` exists:

1. Read `<results>` and `<created>` in full.
2. Run Start steps 2 and 3, then the sweep of Start step 6: exit 0 means the created file
   accounts for everything that runs. Then price what the run has spent so far with
   `live cost spent` (Rules, item 3): exit 3 means ask the maintainer before continuing.
3. The newest candidate in the Run block is the one under test; install it again (Candidate
   step 5) if `<run>/cli/<candidate>` is missing.
4. For each entry of `<created>` without `destroyed_at`, run `vecten deploy --check --env prod`
   in its project: it says whether the environment is deployed. `<cli>` is the CLI the project
   is on: `<run>/cli/<candidate>` once its `vecten update` (One combination, step 12) has a
   PASS row, and `<run>/cli/<previous>` for an upgrade-path project whose update has none yet.
   `[project] scaffold_version` in its `vecten.toml` names that version.
5. Continue with the first row that has no result, in the order this page gives; never repeat
   a step that created something an entry of `<created>` already records.

## Proving run

The first run of this runbook, one combination from Start to its sweep, is itself a check of
the runbook. Every hand-off to the maintainer other than the sign-in sitting goes in the
results file's Hand-offs with its cause, and is fixed in the tooling or on this page before the
next release's run.
