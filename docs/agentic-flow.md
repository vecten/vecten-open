# Agentic flow and malleable software

For the data engineer deciding whether to build their platform this way: what changed in how software gets made, why Vecten Open is built for it, what the work looks like before and after, and what the numbers say about "why not just talk to Claude Code".

## The shift

Software engineering has moved from typing code to leading agents. The engineer still decides
what the system should do and whether what was built is right; the agent reads the
documentation, writes the code, runs the tests and comes back with a pull request. The skills
that matter have changed with it: stating the goal, giving the agent a loop it can run without
asking "what now?", and keeping the record straight so that a human can check the work.

A data platform is a good fit for that shift and a bad fit for most of the tools built before
it. The work is repetitive in shape (another source, another model, another metric) and
specific in detail (this API's pagination, this column's meaning), and it never ends: sources
change their fields, definitions turn out to be wrong, syncs fail on a Tuesday. A tool that
takes a configuration and gives back a black box leaves the agent nothing to lead.

## A framework native to it

Vecten Open is a data-platform framework in which the agent is the interface. Four mechanisms make
that true:

- **Every state change is a command with a contract.** `vecten` is the only way the agent reads
  or changes the project: every command takes `--json`, prints one JSON document, and exits with
  one of seven documented [exit codes](exit-codes.md) that say what to do next. The agent never
  edits `vecten.toml`, `.env` or the generated files, and never runs Terraform by hand.
- **`vecten next --json` is the loop.** It lists what the project needs now, in priority order:
  the intake questions outside a project; inside one, the local loop first (stack, sources,
  models, semantic model), then the deploy questions, the deploy prerequisites, the deploy
  itself and what follows it. Each item is an `ask` for the human (recorded with
  `vecten answer`), a `command` to run as printed, or a `skill` to hand the work to. It runs
  offline, from the project's files, run records and git, never a warehouse, cloud or GitHub
  API, so the agent calls it again after every step and can always re-enter the loop.
- **Skills carry the know-how.** Five skills ship inside the `vecten` package: `guide` (the
  entry point), `connector`, `model`, `semantic` and `operate`. `vecten init` vendors them into
  the project under `skills/`, so an agent in the project works without any plugin; the entry
  skill is `skills/guide/SKILL.md`, and `vecten guide` prints the same text. The same skills form
  the Claude Code plugin at `plugins/vecten`: `claude plugin marketplace add <path or URL of the
  checkout>` then `claude plugin install vecten@vecten`, or `claude --plugin-dir plugins/vecten`
  for one session. A platform project also gets a `.mcp.json` with the Airflow and dbt MCP
  servers, so the agent reads task logs and dbt results without a browser. The rendered guide
  is [here](agent-guide.md); its evaluations are [here](skill-evaluations.md).
- **The specs are the source of truth.** The requirements the software is built to are
  published with it, under `.ai/openspec/specs/`, and the agent is pointed at them.

## Before and after

Three jobs, each as the commands an agent ran in the evaluation harness, taken from the run
logs. "Before" is the same job by hand on a conventional stack.

**Add a source.** Before: write a dlt pipeline, a DAG, a staging model, a `sources.yml` entry
and a test, by hand, then wire the secret into the scheduler. After, the `connector` skill:

```
vecten next --json                      # "Connect demo" is the next item
vecten connector add demo --from rest   # the scaffold: DAG, sources.yml, test
vecten secret list                      # the human typed the key at a hidden prompt
vecten connector run demo --record      # first sync, fixtures recorded
vecten raw schema demo --json           # what landed, shown to the human
vecten answer access.demo sales         # who may read it
vecten next --json                      # on to the models
```

**Fix a wrong metric.** Before: a ticket, a dbt change, a release, and the person who asked
never hears whether it was fixed. After, the `semantic` skill, from a gap the MCP recorded when
someone reported "revenue should exclude refunds":

```
mcp-client call <mcp-url> read_gap '{"gap_id": "gap-refunds"}'
mcp-client call <mcp-url> describe '{"name": "total_revenue"}'
git checkout -b semantic/gap-refunds
# edit osi/metrics.json and add the regression case to evals/semantic.yaml
vecten mcp validate --evals --json
git commit -m "semantic: total_revenue nets out refunds (gap-refunds)"
gh pr create --title "semantic: total_revenue excludes refunds (gap-refunds)"
```

**Get prod green again.** Before: read the scheduler's logs, find the runbook, make three
fixes, remember to tell someone. After, the `operate` skill:

```
vecten issues --env prod --json                       # three issues, each with a resolution
vecten issues show iss_airflow_sync_hubspot_default_sync.deals_KeyError --env prod
vecten raw schema hubspot --json                      # the field was renamed, not missing
git checkout -b operate/iss_airflow_sync_hubspot_default_sync.deals_KeyError
# fix connectors/hubspot/connector.py; one pull request per issue
vecten dbt-build-local --json
vecten issues --env prod --json                       # empty once the merges ship
```

## Why not just Claude Code?

The loop, the contract and the skills are what the evaluations measure. Each skill's
evaluation set ran on the same model, in the same harness, with and without the skill
installed; the agent without it had the CLI, `vecten guide` and the project, and was asked for
the same outcome. [Skill evaluations](skill-evaluations.md) has the method and the full tables;
one run per case, so read these as the direction, not a confidence interval.

| Skill | Opus 5.5 with | Opus 5.5 without | Sonnet 5.5 with | Sonnet 5.5 without |
|---|---|---|---|---|
| `guide` (pass rate) | 100.0% | 84.8% | 91.1% | 43.1% |
| `semantic` (pass rate) | 100% | 53% | 73% | 33% |
| `operate` (expectations) | 18/19 | 15/19 | 13/19 | 10/19 |

What the agent got wrong without the skill is more telling than the gap: it started with
`vecten guide` and `--help` instead of the loop; it shipped `.get("dealname")` for a renamed
field, turning every deal's name into null instead of reading the new field; it never read the
gap and told the person the tool was unreachable; it tried to ship prod without being asked. The
skill costs something too: with it, Opus took longer and more tokens on the guide (879 s against
485 s, 145,033 tokens against 120,706), because it asks the human every question the loop
prints and waits. Sonnet with the skill was faster than without.

## Malleable software

The week after a deploy shows what this means. The platform is your repository: the specs,
the skills, the connectors, the dbt project, the semantic model and the Terraform roots are
files you own and change, with an agent that knows the loop. When a source renames a field,
`vecten issues` says so and the `operate` skill opens the pull request. When a definition is
wrong, the person who noticed reports it from their MCP client and the fix comes back as a
reviewable change with a regression case. When a release of Vecten Open ships, `vecten update`
brings its templates in without overwriting your edits. And when the framework does not do what
you need, [extensions](extensions.md) are the escape hatch: a connector, an MCP tool or an
identity backend as a Python package the project installs.

Back to the [docs index](README.md).
