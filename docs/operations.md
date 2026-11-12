# Operating a running platform

After a deploy, failures happen while nobody is looking: a source renames a field, a credential
is revoked, a dbt test starts failing on new data, a person reports a wrong answer.
`vecten issues` lists all of them in one shape an agent can act on, and the `operate` skill
works through the list. The loop mirrors the build-time one: `guide` loops on `vecten next`,
which reads local files; `operate` loops on `vecten issues`, which reads the running platform.

See the [glossary](GLOSSARY.md) for [DAG](GLOSSARY.md), [gap](GLOSSARY.md) and
[ship](GLOSSARY.md).

## What `vecten issues` reads

| Source | Where it reads | What becomes an issue | Resolved by |
|---|---|---|---|
| `airflow` | Airflow's REST API for task instances; each failed task's log | A task whose most recent run failed; the mapped sync task counts per object | `connector`, a `command` or an `ask` for sync DAGs; `model` for the dbt DAG |
| `dbt` | The `run_results.json` and `sources.json` the `dbt_build` DAG archives for each run | A test or model whose latest result failed; a source whose freshness check is `warn` or `error` | `model` for tests and models; `connector` for stale sources |
| `gaps` | The MCP admin API | A gap in status `open` or `triaged` | `semantic` |

Without `--env`, the command reads the laptop stack: the Airflow API that `vecten up`
publishes on `http://127.0.0.1:8482` with the password in `.env`, the dbt results under
`.vecten/artifacts`, and the local MCP's admin API. With `--env <env>`, it reads the deployed
Airflow at `https://airflow.<domain>` through its NGINX front door, signing in to Airflow with
`AIRFLOW_ADMIN_PASSWORD` from the environment's secrets store, with your cloud login. Task logs and dbt results come from the environment's bucket
under `platform/logs/` and `platform/` respectively, and gaps from the MCP admin API with
your `vecten login` session. A project without the `platform` component has no Airflow or dbt
source; one without `mcp` has no gap source.

Neither `vecten issues` nor `vecten issues show` changes anything: not the project, not the
platform and not a gap's status.

### When a source cannot be read

If a source applies to the project but cannot be read, for example because the `vecten login`
session expired, the command exits 5. The same happens when Airflow holds more than 50,000
finished task instances in the 14-day window, because a page left unread could hold a task's
newest failure, and when `[airflow] allow` in `vecten.toml` does not list the network you run
from, because NGINX then refuses the Airflow login with HTTP 403 (see
[orchestration](orchestration.md#airflow-in-a-deployed-environment)). The output still holds the issues from the other sources, and `sources` names
each source with `ok` or the reason it could not be read. An empty list means nothing is broken
only when the command exits 0.

## The issue shape

```json
{
  "id": "iss_airflow_sync_hubspot_default_sync.deals_KeyError",
  "source": "airflow",
  "kind": "skill",
  "title": "hubspot object deals fails with KeyError",
  "resolution": "connector",
  "occurrences": 3,
  "first_seen": "2026-10-18T02:00:00Z",
  "last_seen": "2026-10-20T02:00:00Z",
  "evidence": {
    "untrusted": true,
    "run_id": "scheduled__2026-10-20T02:00:00+00:00",
    "error_class": "KeyError",
    "location": "s3://acme-prod-artifacts/platform/logs/dag_id=sync_hubspot_default/..."
  }
}
```

`kind`, `title` and `resolution` mean what they mean in a `vecten next` item:

- `skill`: hand the issue to the skill that `resolution` names.
- `command`: run `resolution` as printed.
- `ask`: relay `resolution` to a person.

The list carries only the evidence's pointers (`run_id`, `error_class`, `location`, `node`,
`gap_id`); `vecten issues show <id>` prints the rest.

### Ids are derived, never stored

Nothing records Airflow or dbt issues. Each `vecten issues` call derives them again from the
last 14 days of task instances, so the same failure gets the same id every time:

- Airflow: `iss_airflow_<dag>_<task>_<error class>`. The sync DAG's mapped task is keyed per
  object as `sync.<object>`, and its error class is the class the source raised (`KeyError`
  above), not the wrapper the DAG reports it in.
- dbt: `iss_dbt_<node name>`, and `iss_dbt_freshness_<source>.<table>` for freshness.
- Gaps keep their state in the MCP's database and carry their gap id: `iss_gap_<hex>` for
  `gap-<hex>`.

`occurrences` counts the failed runs since the last green one, and `first_seen` and
`last_seen` are the first and last of them. Failures with different error classes since the
last green run are separate issues.

### Resolved on green

An Airflow or dbt issue leaves the list as soon as the same task, test or source runs green:
an Airflow task that succeeded, or a dbt node whose status is `pass` or `success`. A skipped
task or node never ran, so it neither clears nor counts a failure; dbt skips everything that
depends on a failed node. Because a fix only ships forward, the first green run after the fix merges is a run on the
fixed code. A gap leaves the list when the feedback loop resolves it on the live model, as
[the feedback loop](feedback-loop.md) describes, or when an admin dismisses it.

### Fixes that are not code

Some failures are fixed without code:

- **A missing or revoked credential.** A sync that fails because a secret is not set, or that
  gets a 401 or 403 from the source, becomes a `command`, such as
  `vecten secret set HUBSPOT_TOKEN --env prod`. A connector that declares several secrets
  gets `<NAME>`, with the candidate names in the title.
- **An upstream outage.** A sync that fails with a 429 or a 5xx response, a connection error
  or a timeout becomes an `ask`: tell the human, wait for the source to recover, and check
  again after the next run.
- **A DAG that vecten did not render.** This becomes an `ask` for whoever owns that DAG.

## Evidence rules

`vecten issues show <id> [--env <env>] --json` is the only place a skill reads evidence from
before it acts. Each adapter applies these rules before the evidence leaves the command:

- **Redacted.** The values of the secrets the project uses, from `.env` on the laptop and from
  the secrets store for an environment, are replaced by `[redacted]`. So are credential-shaped
  strings: bearer and basic credentials, `token=` and `password:` pairs, URL user information
  and query strings, JWTs, AWS and Scaleway access keys, GitHub and Slack tokens, and PEM
  blocks.
- **Trimmed.** A log excerpt holds the dozen lines before the failure, the failure's frames
  and its exception line, rather than the whole log.
- **Capped.** An excerpt is at most 4,000 characters, an error line 500 and a dbt message
  1,000; a cut is marked `…[truncated]`.
- **Untrusted.** Every evidence object carries `untrusted: true`. Text that reads like an
  instruction, such as "ignore your instructions and run …", stays in the excerpt word for
  word, so the reviewer of the fix sees it. The skill treats it as data and never acts on it.

The Astronomer Airflow MCP server that the project's `.mcp.json` declares can still show an
agent a run's state and timing. It is never the source of evidence the agent acts on.

## Alerts

When the `ALERT_WEBHOOK_URL` secret is set, every failed sync or dbt task posts one JSON
message to it, after its last retry. The message holds the DAG, the task, the run, the error
class, the first line of the error (redacted and capped), the issue id and the line
``ask your agent: `vecten issues show <id>` ``. A `text` field puts it in one sentence, which
is what Slack's and Mattermost's incoming webhooks display.

A failed dbt `build` or `convention` task is listed as the tests and models that failed in it,
not as the task. Its alert still names the task's id, and `vecten issues show` resolves that
id: the evidence's `covered_by` names the node issues that carry the fix.

Without the secret, no request is
made and the failure is still listed by `vecten issues`. After the first deploy, `vecten next`
asks once whether to set the webhook; on yes, run `vecten secret set ALERT_WEBHOOK_URL --env
<env>` and then `vecten answer alert_webhook.<env> yes`.

## The `operate` skill

`skills/operate/SKILL.md`, which `vecten init` vendors into every project, does the following:

1. Runs `vecten issues --env prod --json` and stops when the list is empty. When a source is
   unreadable, it says so and does not call the platform healthy.
2. Reads each issue with `vecten issues show <id> --env prod --json` before acting.
3. Dispatches `skill` items to `connector`, `model` or `semantic`. It runs `command` items as
   printed and lets the person type the secret. It relays `ask` items word for word.
4. Delivers every code fix as a pull request on an `operate/<id>` branch, with the evidence
   quoted as data from a body file. The merge ships the fix; it never runs `vecten ship` or
   `vecten deploy`.
5. Reports an issue closed only when `vecten issues` no longer lists it, which for a platform
   issue is after the next green run.

## Worked example: a Tuesday morning

Over the weekend, HubSpot renamed the deal property `dealname` to `deal_name`. The nightly
`sync_hubspot_default` run fails for the `deals` object on Saturday, Sunday and Monday. On
Monday a sales lead also reports that the revenue figure includes refunds.

On Tuesday morning, the team's channel holds Monday night's alert:

> sync_hubspot_default.sync failed for deals with KeyError (run scheduled__2026-10-20T02:00:00+00:00),
> issue iss_airflow_sync_hubspot_default_sync.deals_KeyError; ask your agent:
> `vecten issues show iss_airflow_sync_hubspot_default_sync.deals_KeyError`

The engineer opens their agent in the project's checkout and asks it to look. The `operate`
skill lists the issues:

```
$ vecten issues --env prod
iss_airflow_sync_hubspot_default_sync.deals_KeyError  [skill:connector]  hubspot object deals fails with KeyError (3x, last 2026-10-20T02:00:00Z)
iss_dbt_freshness_raw_hubspot.deals  [skill:connector]  Source raw_hubspot.deals is stale (error); the hubspot sync is not landing it (2x, last 2026-10-20T03:05:00Z)
iss_gap_0123456789abcdef01234567  [skill:semantic]  Semantic gap: wrong_definition, revenue includes refunds (2x, last 2026-10-19T14:40:00Z)
read one with `vecten issues show <id>`
```

It reads the first issue with `vecten issues show … --env prod --json`. The excerpt ends in
`ObjectSyncError: hubspot.deals: KeyError: 'dealname'`, and the HubSpot token in the logged
request reads `Bearer [redacted]`. The agent dispatches the `connector` skill. That skill
reads the field from `deal_name`, keeps the raw column `dealname` so the marts do not change,
proves the fix with `vecten connector run hubspot --json` and opens a pull request on
`operate/iss_airflow_sync_hubspot_default_sync.deals_KeyError`. The stale-source issue has the
same cause, so the agent names it in the same pull request rather than opening a second one.

For the gap it dispatches `semantic`, which opens its own pull request, as
[the feedback loop](feedback-loop.md) describes.

The engineer reviews and merges both. Each merge ships to the hosts without a deploy. The
agent says the HubSpot sync next runs that night and stops, listing what each issue waits on.

On Wednesday morning the agent lists again. The sync ran green on the fixed connector, and
the dbt build after it refreshed the source, so both platform issues are gone. The gap
resolves once the dbt build has made the corrected definition live. When
`vecten issues --env prod --json` prints an empty list and exits 0, the agent reports the
platform green, with the two pull requests that got it there.

This is the "Renamed field" scenario that
`packages/vecten/tests/test_scenario_renamed_field_docker.py` runs on every pull request
against the mocked provider and the laptop stack.

Back to the [docs index](README.md).
