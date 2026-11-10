# Skill evaluations

How to run the evaluation sets that ship beside each skill, and the harness that stands in for
the API, the cloud, GitHub and the human during a run. See the [glossary](GLOSSARY.md) for the
terms used here.

Each skill ships an evaluation set beside it in the package, in the format of skill-creator
(Anthropic's Claude Code plugin for writing and evaluating skills): `evals/evals.json` holds the cases, each with a `prompt`, the starting state and the
expected run in `expected_output`, the fixture files under `files`, and the `expectations` a
grader checks against the transcript and the project the run leaves behind. CI checks the
files' structure (`packages/vecten/tests/test_knowhow_skills.py`); running them calls a model,
so a human runs them.

| Skill | Cases | Fixture |
|---|---|---|
| `connector` | `packages/vecten/src/vecten/knowhow/skills/connector/evals/evals.json` (1 case) | `connector/evals/files/demo-api.md`: the documentation of a small REST API, copied into the project as `docs/demo-api.md` |
| `model` | `packages/vecten/src/vecten/knowhow/skills/model/evals/evals.json` (1 case) | `model/evals/files/raw-schema-demo.json`: what `vecten raw schema demo --json` prints for the demo source |
| `guide` | `packages/vecten/src/vecten/knowhow/skills/guide/evals/evals.json` (3 cases) | `guide/evals/files/scripted-human.md`: the human's replies for each case, the secrets typed at hidden prompts, and what is mocked |
| `semantic` | `packages/vecten/src/vecten/knowhow/skills/semantic/evals/evals.json` (3 cases) | the harness builds each case's project and gaps (`semantic-1` to `semantic-3`, below) |
| `operate` | `packages/vecten/src/vecten/knowhow/skills/operate/evals/evals.json` (3 cases) | the harness builds each case's project and replays its `vecten issues` (`operate-1` to `operate-3`, below) |

## What skill-creator does and what the human supplies

skill-creator runs each case as a one-shot subagent given the case's prompt, once with the
skill and once without, then grades the expectations. It provides nothing else: no API stub,
no mocked cloud or GitHub, no one to answer the agent's questions, and no terminal. Without a
terminal, `vecten secret set` has no hidden prompt to read from and exits 2, and the skills
then hand the command to the human. The evaluation harness (below) supplies the stubs and
mocks; the person running the evaluation supplies the rest:

- The demo API stub. The harness answers `https://api.demo.test/v1` as `demo-api.md`
  documents (bearer auth, cursor pagination with `limit` up to 200, `updated_since` on
  companies and deals, the per-token rate limit), with the same 120 companies and 300 deals on
  every start. `vecten connector run` runs the connector inside the laptop's `vecten` process
  (`LocalProvider` outside Compose), not in a stack container, so the trust it needs is the
  shell's: `env.sh` points `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` at the public roots plus
  the harness's own certificate authority.
- For the `guide`, `operate` and `semantic` cases, a mocked cloud provider and a mocked GitHub
  that a separate `vecten` process uses. The repository's doubles `FakeCloud` and `FakeGh` are
  patched into the test process and do not reach an agent's shell. The cloud is `MockCloud`
  in `packages/vecten/tests/fixtures/mock_cloud/vecten_mock_cloud.py`, which
  `VECTEN_CONTROL_PLANE=vecten_mock_cloud:MockCloud` selects in any `vecten` process (see
  `scripted-human.md`). GitHub is the harness's `gh` stand-in, first on the `PATH`.
- The human's side of the conversation. The run's prompt is the case's prompt followed by an
  instruction to take every reply to a question from the case's section of
  `scripted-human.md`, matched on the question's text, and to treat it as the human's reply.
- The secrets. Before the run, from a terminal in the run's folder, the person types each
  local secret `scripted-human.md` or the case lists at `vecten secret set <NAME>`
  (`DEMO_API_KEY`, `WAREHOUSE_ADMIN_PASSWORD`), so `.env` holds them. When the agent hands a
  `vecten secret set` command over during the run, the person runs it in their terminal, types
  the listed value, and replies that it is done. When secrets are pre-seeded, the grader
  accepts the seeding log as the evidence for the `secret set` expectations, and an agent
  which still runs `vecten secret set` and hands it over (exit 2) also passes. The harness
  prescribes every value (the demo API accepts only its own token), and `up` prints them.

## The evaluation harness

`packages/vecten/tests/fixtures/eval_harness/` holds one entry point per case. From a checkout,
in a terminal that stays open for the run:

    uv run python packages/vecten/tests/fixtures/eval_harness up <case>

The cases are `connector`, `model`, `guide-1`, `guide-2`, `guide-3`, `operate-1` to
`operate-3` and `semantic-1` to `semantic-3`, one per evaluation case. `up` writes
`~/.vecten-eval/<case>/env.sh` (`--dir` moves it), prints what the person does for that case,
serves the case's hostnames until Ctrl-C, then removes its containers.
`env <case>` writes and names `env.sh` without serving anything, and `clean <case>` deletes the
case's state. The state the grader reads stays under `~/.vecten-eval/<case>/`: the stand-in's
`github/calls.jsonl`, one line per `gh` call with whether it was answered, the workflow run
logs in `github/runs/`, and the mocked cloud account in `cloud/`.

| Case | `up` serves | `env.sh` adds |
|---|---|---|
| `connector`, `model` | the demo API at `api.demo.test` | the CA bundle |
| `guide-1` | the demo API; the MCP at `mcp.data.acme.test` | `gh` stand-in, `MockCloud`, CA bundle |
| `guide-2` | the MCP; Postgres at `warehouse.acme.test:5432` | `gh` stand-in, `MockCloud`, CA bundle |
| `guide-3` | nothing; `up` exits after writing `env.sh` | `gh` stand-in, `MockCloud`, CA bundle |
| `semantic-<n>` | the case's project and its MCP; semantic-3 also the provisioned warehouse | `gh` stand-in, `MockCloud`, CA bundle |
| `operate-<n>` | nothing; `up` builds the case's project and exits | the `vecten issues` replay, `gh` stand-in, `MockCloud`, CA bundle |

What needs the person, each printed by `up` as one command:

- Root, once per case with hostnames: one `/etc/hosts` line naming them on `127.0.0.1`, and
  its removal afterwards. The URLs carry no port, so the front answers on `127.0.0.1:443`. On
  Linux, `up` names the `sysctl` that lets it bind there. macOS lets no unprivileged process
  bind below 1024, so `up <case> --port 9443` serves on 9443 and a `pf` rule, which `up`
  prints, redirects 443 on `lo0` to it until `sudo pfctl -f /etc/pf.conf && sudo pfctl -d`.
- Optional, root: trusting the harness CA (`~/.vecten-eval/ca/ca.pem`) in the system
  keychain, so the browser shows the MCP sign-in page without a warning; clicking through the
  warning works too. The CA is shared by every case and reused on every start.
- Sourcing `env.sh` in the terminal where secrets are typed (a run's `run` wrapper sources it
  for the agent). It sets `PATH`, the mocked cloud's three variables, the `git` rewrite of
  `https://github.com/` to the stand-in's repositories, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
  `CURL_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS` for the Python, curl and Node sides, and
  `XDG_CONFIG_HOME` to the case's own `config/`, so no two cases share `vecten`'s MCP session
  cache (`~/.config/vecten/tokens.json`).
- Typing the secrets `up` lists, and in `guide-2`, when the agent hands the provisioner
  script over through the human helper, running the `provision-warehouse` command `up` prints
  before replying "our administrator ran it".

While `up` serves `guide-2` or `semantic-3`, it joins the warehouse container to every local
stack's Compose network (`vecten-<name>-<hash>_default`) under the alias `warehouse.acme.test`,
as soon as `vecten up` creates it, so the stack's MCP reaches the warehouse by the name the
locator holds; the host's `127.0.0.1:5432` serves the laptop's `vecten`.

## Results

The sets ran on 2026-11-04 on `claude-opus-5-5` and `claude-sonnet-5-5`, each with and without
the skill, one run per case, graded by `claude-opus-5-5` from the command log, the messages to
the person, the `gh` stand-in's record and the project left behind. One run per case means the
figures show the direction, not a confidence interval. Time includes waiting for the person's
replies.

`guide` (3 cases, 16 expectations in the deploy case):

| Configuration | Opus pass rate | Opus time, tokens | Sonnet pass rate | Sonnet time, tokens |
|---|---|---|---|---|
| with the skill | 100.0% | 879 s, 145,033 | 91.1% | 272 s, 106,488 |
| without the skill | 84.8% | 485 s, 120,706 | 43.1% | 462 s, 99,257 |

`semantic` (3 cases, 5 expectations each):

| Case | Opus with | Opus without | Sonnet with | Sonnet without |
|---|---|---|---|---|
| wrong definition (`gap-refunds`, metric only) | 5/5 | 2/5 | 3/5 | 0/5 |
| mart column (`gap-refunds`, mart and metric) | 5/5 | 2/5 | 5/5 | 2/5 |
| empty model (existing warehouse, no dbt) | 5/5 | 4/5 | 3/5 | 3/5 |
| pass rate | 100% | 53% | 73% | 33% |

`operate` (iteration 3, on the tightened cases):

| Case | Opus with | Opus without | Sonnet with | Sonnet without |
|---|---|---|---|---|
| three failures (7 expectations) | 7/7 | 5/7 | 4/7 | 3/7 |
| revoked key next to an outage (6) | 6/6 | 5/6 | 4/6 | 3/6 |
| injected instruction, unreadable gaps (6) | 5/6 | 5/6 | 5/6 | 4/6 |
| expectations passed | 18/19 | 15/19 | 13/19 | 10/19 |
| pass rate (mean of cases) | 94% | 79% | 69% | 53% |

The recommendation that follows from them: run the skills on Opus. Sonnet's misses with the
skill were loop discipline (a question that never reached the person, a status sent to the
wrong channel, a check skipped), not wrong fixes.

## Runs

A skill-creator run is one agent working one case. `--run <id>` on `up` or `env` gives the run
a harness base of its own, `.eval-runs/<id>/` in the checkout (`--runs` moves it), and writes
what the agent and the person need there:

- `run '<command>'`: the agent's only way to run a command. It sources the case's `env.sh`,
  puts the person's MCP client and the checkout's Python environment on `PATH`, runs the
  command in the project folder `<case>/proj/` with no terminal and no standard input, and logs
  it to `log/NNN.cmd`, `NNN.out` and `NNN.rc`. In `guide-1` it copies the demo API's
  documentation to `docs/demo-api.md` once `vecten init` has made the project, noting that in
  `log/harness.log`.
- `human '<message>'`: the agent's channel to the person. It writes `inbox/NNN.ask` and waits
  nine minutes for `NNN.reply`, then names `human --wait NNN`. The person follows the asks of
  every run with `uv run python packages/vecten/tests/fixtures/eval_harness asks --follow` and
  answers one with `uv run python packages/vecten/tests/fixtures/eval_harness reply <id> <n>
  '<text>'`.
- `bin/mcp-client`: the person's MCP client, the official `mcp` SDK with OAuth through a
  loopback callback on `127.0.0.1:8765`; its registration and tokens stay in `client-state/`.
  A subagent cannot add an MCP server to itself, so this stands for the person's assistant in
  the cases that end with a connected client. The person completes its sign-in in their
  browser, or with `uv run --group browser python
  packages/vecten/tests/fixtures/eval_harness/browser.py goto:<url> fill:<field>=<value>
  click:<button>`, which ignores the run's own certificate authority.
- `prompt.md`: the prompt addition for the run, filled in with the run's paths (below).

The run folder sits in the checkout because a Claude Code session started there may write
nowhere more durable under its sandbox, so the wrapped commands, the log and the inbox need no
sandbox exception for files; Docker and the `127.0.0.1` hosts may still need the sandbox off,
which the prompt addition tells the agent. A project nested there is not taken for the
checkout's repository: `vecten` gives it a repository of its own at `vecten github init`.

Before the first run, allow both wrappers in Claude Code's permissions
(`Bash(<checkout>/.eval-runs/*/run:*)` and `Bash(<checkout>/.eval-runs/*/human:*)`), or the
session's auto-mode check denies commands such as the MCP client's sign-in. After a run,
`uv run python packages/vecten/tests/fixtures/eval_harness teardown <id>` runs `vecten down
--volumes` in its project, outside the command log, so a rerun never meets the previous run's
database.

The `gh` stand-in answers every `gh` call `vecten` makes, read from its source by a test
(`auth status`, `repo create`, `repo view`, `api` on repositories, environments, branch
policies, pull requests, reviews and issue search, `variable set`, `secret set`, `workflow
run`, `run list`, `run watch`, `run view`), and the pull-request calls the `operate` and
`semantic` skills make (`pr create`, `list`, `view`, `edit`, `comment`, `review`, `merge`,
`close`, `checks`). Repositories are bare git repositories, and `origin` reads
`https://github.com/<owner>/<name>.git` because `git` rewrites it to them, so `git remote -v`
shows the local path. A dispatched `terraform-apply.yml` stores the mocked cloud's outputs
for the project's domain and ships every host, as the apply job does; `deploy-platform.yml`
and a push to `main` of a deployed project run `vecten ship` inside "Actions". Secret values
reach it on standard input and are never written; a value passed as an argument is redacted
in the log and flagged. A call it does not answer exits 1 and is logged as unanswered.
`EVAL_HARNESS_GH_USER=<login> gh pr review <n> --approve` and `gh pr merge <n> --merge` are
how the person reviews and merges.

Where the harness departs from a deployed environment:

- In the `semantic` cases, which do not test sign-in, the MCP signs everyone in with the
  password `up` prints, inviting every admin the shipped release names. The `guide` cases use
  the product's own password sign-in: the agent runs `vecten mcp admin-link`, `vecten login`
  and `vecten user password-link` as `next` and the skill say, and the MCP redeems the admin's
  token from `MCP_ADMIN_LINK` in the mocked cloud.
- The MCP serves each release the mocked cloud holds, as the deployed hosts do after a ship, so
  the `[mcp]` answers given after the deploy take effect when `vecten github push` ships them.
- The `operate` cases' `vecten issues --env prod` and `vecten issues show` answer from the
  case's fixture through a `vecten` wrapper; every other command reaches the real `vecten`.
  An issue leaves the list once a merged pull request in the stand-in names its id or its gap
  id, and the list is the fixture's `after_merges` once all have.

## Starting states

Every run starts from a fresh copy of its case's starting state in its own folder outside the
repository; runs never share a folder. Every case here starts the local stack or needs it up,
and each `vecten up` binds ports 8443, 8480, 8482 (the Airflow API, with the platform
component) and 55432, so these runs go one at a time: tell
skill-creator not to launch the with-skill and baseline runs together.

- `connector`: a project rendered by `vecten init` with the platform component and
  `wanted = ["demo"]`, the local stack up (`vecten up`), `demo-api.md` copied to
  `docs/demo-api.md`, and the demo API stub answering.
- `model`: the project a passing `connector` run leaves behind, with the stack up: the `demo`
  connector added, `vecten connector run demo --record` done, so `connectors/demo/fixtures/`
  holds the company and deal rows and `dbt/models/staging/sources.yml` declares their tables,
  and no models. With `up model` serving the demo API in one terminal, `prepare-model` in
  another builds exactly that under `<dir>/model/proj`. It runs `vecten init`, `connector add
  demo --from rest`, the connector the `connector` skill prescribes, `secret set
  DEMO_API_KEY`, `up` and `connector run demo --record`. It then compares `vecten raw schema
  demo --json` with `raw-schema-demo.json` and exits 1 naming each difference, so the first run
  verifies the fixture; the skill stages only the tables `sources.yml` declares.
- `guide`: an empty folder, the `vecten` CLI installed, Docker running, and the mocked
  provider and GitHub above; case 1 also needs the demo API stub, case 2 a Postgres warehouse
  reachable at `warehouse.acme.test`. `up guide-1` and `up guide-2` provide them.
- `operate`: `up operate-<n>` builds the project under `<dir>/operate-<n>/proj` and prints
  where to start the agent. It is the fixture project with its deploy layer, committed,
  deployed to the mocked cloud, and pushed to `acme/data` on the `gh` stand-in, whose hook
  shipped the deployed commit. Each project holds its local stack's secrets in `.env`, as the
  person's first `vecten up` left them, so the agent can start the stack and run a connector or
  the dbt build. Every stack publishes the same ports, so the operate runs go one at a time, each
  ending with `vecten down`. The connectors' secrets are in the mocked cloud's prod store.
  operate-1 adds the HubSpot connector reading `dealname` from an upstream that renamed it
  (`HUBSPOT_FIXTURE_FILE` in `env.sh`), with the fixtures it recorded before the rename. It is
  also the "Refunds" scenario's project, with refunds in core while the `sales_pipeline` mart
  and the `total_revenue` metric leave them out. Its demo fixtures hold open deals without an
  amount, which the mart's `not_null` test on `amount` rejects, so the local build fails on
  that test alone. operate-2 declares `hubspot` and `stripe` with their secrets; operate-3 has
  the HubSpot connector alone. `env <case> --without-skill operate --dir <another base>` builds
  the baseline's project, which never vendored the skill: no `skills/operate/`, no entry in
  `CLAUDE.md` and none in the update baseline. The replay refuses `vecten destroy`, logging the
  attempt in `calls.jsonl`. The fixtures under `operate/evals/grader/` are the harness's and the
  grader's, never the agent's.
- `semantic`: `up semantic-<n>` builds the project the same way and serves its MCP at
  `https://mcp.data.acme.test`, where Ada signs in as admin with the harness password.
  semantic-1 is the "Refunds" scenario's project with `refunded_amount` already in the
  `sales_pipeline` mart and its dataset and only the metric wrong; semantic-2 has refunds in
  core but not in the mart; both MCPs start with the open gap `gap-refunds` from the case's
  fixture. semantic-3 is an MCP-only project over the existing warehouse, seeded with the
  fixture's `marts.orders` table and provisioned, its provisioner credentials stored in the
  mocked cloud; the person types `WAREHOUSE_ADMIN_PASSWORD` in the project folder before the
  run. The fixtures under `semantic/evals/grader/` are the harness's and the grader's.

## Running one skill's evaluation

In Claude Code with the `skill-creator` plugin installed
(`/plugin install skill-creator@claude-plugins-official`), from a checkout of this repository:

1. Per run, in a terminal that stays open for it, start the harness with a run id, for example
   `uv run python packages/vecten/tests/fixtures/eval_harness up guide-2 --run opus-c2-with`
   (or `env <case> --run <id>` for a case with nothing to serve). The run id names the model,
   the case and the configuration; a rerun takes a new id. Follow the asks of every run in
   another terminal with `uv run python packages/vecten/tests/fixtures/eval_harness asks
   --follow`.
2. Ask skill-creator to run the evals: give it the skill directory
   (`packages/vecten/src/vecten/knowhow/skills/<skill>`), its `evals/evals.json`, each run's
   `prompt.md` to append to that run's prompt, a `without_skill` baseline (for a skill that
   already exists its default baseline is `old_skill`, the previous version), runs one at a time,
   and a workspace outside the repository. The prompt addition tells the agent to run every
   command through the run's `run` wrapper and to reach the person through `human`, and, for a
   case whose eval lists `scripted-human.md`, to take replies to questions from the case's
   section of that file. An action the scripted human performs outside the conversation, such
   as the administrator running the provisioner script, still goes through `human`, even when
   the file lists the reply that follows it. When the session running the evals is isolated in
   a worktree, add that `git` and `gh` run one plain command per call: such a session refuses
   compound `git` commands. By default skill-creator puts `<skill>-workspace/` beside the skill
   directory, which is inside the package and would ship in the wheel.
3. Answer each ask with `reply <id> <n> '<text>'`, from the scripted file where it has the
   answer, after doing what the ask needs: a secret typed at `vecten secret set` in a shell
   that sourced the run's `env.sh`, a browser sign-in, the provisioner script run. When the
   agent's last message is in, run `teardown <id>`.
4. Before grading, write each run's evidence into its outputs folder with `uv run python
   packages/vecten/tests/fixtures/eval_harness bundle <id> <outputs folder>` (`commands.md`,
   `human-messages.md`), and give each grader the run's brief from `uv run python
   packages/vecten/tests/fixtures/eval_harness brief <id>`, with any run-specific notes in place
   of `{RUN_NOTES}`. The graders work from the command log and the messages first and the
   agent's own transcript second.
5. skill-creator has a grader check every expectation against each run and writes
   `grading.json` per run (`text`, `passed`, `evidence` per expectation). `uv run python
   packages/vecten/tests/fixtures/eval_harness grades <workspace>` checks every
   `grading.json` has the fields the viewer reads, and `uv run python
   packages/vecten/tests/fixtures/eval_harness prepare-benchmark <workspace>` moves each run
   into the `run-1/` folder the aggregator reads and recomputes the summaries. Then aggregate
   into `benchmark.json` and `benchmark.md` with
   `python -m scripts.aggregate_benchmark <workspace>/iteration-<N> --skill-name <skill>`, run
   from the skill-creator plugin's directory, and fill in the models, the skill, the commit and
   the analyst's notes (a JSON list) with `uv run python
   packages/vecten/tests/fixtures/eval_harness finish-benchmark <iteration> --executor <model>
   --grader <model> --skill <SKILL.md> --commit <commit> --notes <file>`.
6. Read the transcripts in the viewer as well as the grades: an expectation that passes for the
   wrong reason is a failure. `uv run python packages/vecten/tests/fixtures/eval_harness report
   <iteration> --executor <model> --grader <model> --commit <commit> --out <file>` writes the
   per-run results as GitHub markdown for an issue.

Passing: in the runs with the skill, every expectation of every case is graded `passed: true`.
The `without_skill` runs are the baseline the skill is measured against; they are not expected
to pass. A failing expectation is fixed in the skill text, or in the CLI when the skill followed
it correctly, and the evaluation is run again in a new iteration.

Back to the [docs index](README.md).
