# Admin panel and usage analytics

The admin panel is a set of web pages served by the MCP server under `/admin`. It is for
people whose [principal](GLOSSARY.md) holds the admin flag. They use it to manage people and
groups, grant data, read the semantic model's health and usage, triage [gaps](GLOSSARY.md), browse the audit
and change deployment settings.

The panel adds no capability of its own. Every page calls the same admin service as the
[admin API](mcp-identity.md) (used by `vecten user`, `vecten group` and `vecten grant`) and the
[admin MCP tools](mcp-tools.md#human-admin-tools). An action has the same effect whichever of
these surfaces runs it, and each one writes an access event to `mcp_state`, the server's own
database. The [access-event storage](mcp-audit.md#delivery-and-access-events) section describes
those events. Panel actions are recorded as `panel/<operation>`, for example
`panel/grant`. Admin tool calls are recorded as `mcp/<operation>`, and admin API calls by the
operation name alone.

## Signing in

Open `https://<MCP host>/admin`. The panel redirects to the server's sign-in page, which uses
a password or your identity provider, then asks you to approve the "Vecten admin panel" client.
Afterwards you land on the overview. The first admins are the addresses listed under
`[mcp] admins` in `vecten.toml`; see
[first administrator and login](mcp-identity.md#first-administrator-and-login).

- A person without the admin flag can complete sign-in, but the panel then refuses them.
- The panel re-reads the flag and the principal on every request. Clearing someone's flag or
  disabling them takes effect on their next click.
- A panel session lasts up to eight hours, or until the underlying sign-in session ends.
  **Sign out** ends both.
- The session cookie is `HttpOnly`, `SameSite=Strict` and `Secure`, and is scoped to `/admin`.
  Every form carries a token tied to the session, and a submission without it is refused.
  The pages load no scripts.

## Screens

| Screen | What you can do there | Same action elsewhere |
| --- | --- | --- |
| Overview | Model status, questions and refusals in the last seven days, open gaps | `model_health`, `usage` tools |
| Users and groups | Create, adopt, disable and delete people and services; set or clear the admin flag; create and delete groups; add and remove members | `vecten user`, `vecten service`, `vecten group` |
| Grants and deny lists | Grant or revoke a dataset, convention schema or restricted area per group; edit each group's column deny list; reconcile grants now | `vecten grant`, `list_grants` tool |
| Model | Read the loaded model, its health, usage per field and metric, and open gaps | `describe`, `model_health`, `usage_view` tools |
| Usage | The analytics views below, over a chosen number of days | `usage_view` tool |
| Gaps | List gaps by status, read their evidence, set a status, confirm an unreviewed fix | `list_gaps`, `read_gap`, `set_gap_status` tools; confirming has no tool |
| Audit | Filter audit rows by person, tool, resolution and date; open a row and its request chain | `search_audit`, `read_audit_row` tools |
| Connected clients | Active client sessions across people | `connected_clients` tool |
| Settings | Current value, default and range of each setting; change one | `read_settings` tool, `POST /api/admin/settings/set` |

### Users and groups

**Create a person** takes an email address and a display name. The server derives the
warehouse user name from the display name and creates that account. If an account of that
name already exists and is not bound to anyone, nothing changes yet. Instead, a confirmation
page shows the account's roles and last login. On Postgres, an account the [provisioner](GLOSSARY.md) did not
create first needs one `GRANT ... WITH ADMIN OPTION` statement, which the page prints for an
administrator to run. **Adopt existing account** binds it without touching its credentials.
Deleting an adopted principal later removes the server's grants and keeps the account.

**Create service principal** creates `svc_<name>` and shows its client id and secret once.
Copy the secret immediately: it is never stored in readable form and cannot be shown again.

### Grants and deny lists

A grant gives a group read access to one dataset, translated to `GRANT SELECT` on its source,
or to one convention schema (`core`, `marts`, `staging`, `snapshots` or `raw_<source>`), or to
a restricted area `vecten.toml` declares, which covers both `core_<area>` and `marts_<area>`.
Grants are desired state. The server reconciles them every fifteen minutes and whenever the
model changes, and **Reconcile now** runs a reconciliation immediately.

A deny list holds `dataset.field` entries, one per line. Denied fields vanish from `describe`,
`search` and compiled metrics for members of the group. A deny list hides definitions; it does
not stop SQL from reading the column. Warehouse permissions and policies enforce access to data;
see [access and enforcement](mcp-identity.md#access-and-enforcement).

### Model

The model screen is read-only. It shows:

- **Health:** `ready`, `error` with the validation errors, or `shipped, waiting for build`.
  The last status means a bundle (the project content `vecten ship` delivers) reached the server but the dbt build that creates its
  tables has not finished, so the previous model stays live.
- **Datasets, fields and metrics:** each with its description and use count. Open a field to
  see where its description lives.

On a project with the `platform` component, field descriptions live in the dbt `schema.yml`
under `dbt/models/marts/` that declares the model. On an existing warehouse they live in
`osi/datasets.json`. Metrics live in `osi/metrics.json`. The panel never edits a definition. To
change one, run the `semantic` skill (one of the shipped agent skills) in a checkout of the project repository. It drafts the
change, validates it and opens a pull request; the new definition goes live after the merge is
shipped and built. See [the feedback loop](feedback-loop.md) and
[model authoring](semantic-model.md).

### Gaps and the audit

A gap's evidence is text reported by people and agents. The panel displays it escaped, in a
block marked untrusted, and nothing in it is followed as an instruction. Setting a gap's status
only changes its tracking state; see [the feedback loop](feedback-loop.md) for the lifecycle.

A gap whose fix merged without an approving review shows `merged without an approving review;
the gap stays open`, with the pull request and its author, and a **Confirm this fix** button.
Confirming stands in for the review a one-person team cannot give: the confirmation names you
and that merge, the access events record it, and the gap then resolves once the fix is live.
A later unreviewed merge for the same gap needs its own confirmation.

The audit browser lists rows newest first, 50 to a page, with an **Older rows** link.
Opening a row shows its receipt, SQL, arguments and request chain. A request chain links a
refused `query_metrics` call to the `sql-exec` call the agent made next in the same session.
The fields are described in [the audit reference](mcp-audit.md).

## Usage analytics

The views are computed from `mcp_state` only: the audit rows, the gaps and the loaded model.
They never query the warehouse, and the server never writes audit, access-event or gap
tables into your warehouse.

| View (`usage_view` name) | Contents |
| --- | --- |
| Questions per day and person (`questions`) | Calls to `query_metrics` and `sql-exec` per UTC day and per person, including refused and failed calls |
| Top metrics and datasets (`top`) | The 20 metrics most often answered by `query_metrics`, and the 20 datasets most often queried. Refused calls are not counted |
| Fields never used (`unused_fields`) | Model fields that appear in no audit row's column list during the window. The window defaults to the audit retention period |
| Fields with no description (`undescribed_fields`) | Model fields whose description is empty |
| Refusal reasons over time (`refusals`) | Refusals per reason code per day, and the 50 most recent refused questions |
| Open gaps (`open_gaps`) | Gaps with status `open` or `triaged`, most recently seen first |
| Usage per field and metric (`field_usage`) | Every model field and metric with its use count |

A field counts as used when an audit row's column list names it. If the row also records
datasets, only fields of those datasets count. Columns are matched by name, ignoring table
aliases, quoting and case. The window is 1 to 3650 days and defaults to 30.

While the **redact** setting is on, questions read `[redacted]` in every view, in the audit
browser and in the admin tools. This applies even to rows stored before the setting was
turned on.

## Settings

| Setting | Default | Range | Meaning |
| --- | --- | --- | --- |
| `audit_retention_days` | 90 | 1–3650 | Days an audit row and its access events are kept; the daily maintenance drops older rows |
| `redact` | off | on, off | Hide questions, arguments and identifiers in stored and displayed audit |
| `session_idle_seconds` | 900 | 30–3600 | Seconds an idle per-person warehouse connection stays open |
| `row_cap` | 1000 | 1–10000 | Rows a query tool returns at most |
| `statement_timeout_seconds` | 30 | 1–3600 | Seconds a warehouse statement may run |
| `reconcile_drift` | revoke (report on a state an earlier release created) | revoke, report | What reconciliation does with a read grant a managed role holds and the state does not: revoke it, or only list it in access health |

## Restricting who reaches the panel

`/admin` is public by default, protected only by admin sign-in. Two settings in `vecten.toml`
narrow it. Both are enforced by the NGINX front door on the MCP host, and the MCP endpoint
`/mcp` and the admin API `/api/admin` stay public either way:

```toml
[mcp]
# Only these networks may open /admin; everyone else gets 403.
admin_allow = ["203.0.113.0/24", "2001:db8::/32"]
```

```toml
[mcp]
# /admin answers 404 publicly; reach it through a tunnel instead.
admin_panel = "off"
```

`admin_allow` takes IPv4 or IPv6 networks; a bare address means that one address. `allow`
under `[airflow]` does the same for the whole Airflow hostname; see
[orchestration](orchestration.md#airflow-in-a-deployed-environment).
`admin_allow` cannot be combined with `admin_panel = "off"`. Either change takes effect the next
time the bundle is rendered and applied, which `vecten ship --env <env>` does.

With `admin_panel = "off"`, the MCP container also listens on port 8001 of the host's loopback
interface. On AWS, open a Session Manager tunnel to the MCP host and sign in through it:

    aws ssm start-session --region <region> --target <MCP host instance id> \
      --document-name AWS-StartPortForwardingSession \
      --parameters portNumber=8001,localPortNumber=8001

Then open `http://127.0.0.1:8001/admin`. Sign-in through the tunnel works as it does publicly.
With OIDC, your identity provider still returns to the public `/oidc/callback`. That route stays
reachable when the panel is off, and it hands the sign-in back to the tunnel's address, where
the sign-in check runs against the tunnel's cookies.
Scaleway offers no SSH-free tunnel to an instance, so on Scaleway use `admin_allow` instead.

Back to the [docs index](README.md).
