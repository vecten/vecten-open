# Analytics apps

An analytics app is a dashboard that an extension declares and a project binds to MCP groups.
A Streamlit app is the usual example. Vecten has no dashboards and no BI tool of its own. It
runs the app, decides who may open it, and limits what data it reads. The MCP's groups decide
both of those, just as they do for questions people ask through the MCP.

An app reads data through the MCP as its own service principal, the **app principal**,
`svc_app_<app>`. The MCP lets that principal read only the datasets the app declares, with the
warehouse roles of the groups the app admits. A person opens the app only through one of those
groups. Every person the app admits sees the same data.

Terms used on this page:

- **Record**: `extensions.lock.json`, which `vecten lock` writes; it holds each extension's
  declarations ([dependencies and locks](extensions.md#dependencies-and-locks)).
- **Login route**: a route of an extension service that only signed-in members of its groups
  reach; NGINX asks the gate before forwarding a request to it.
- **Gate**: the MCP's check, at an app's or service's hostname, that a person is signed in and
  in an admitted group. It is part of the extension services change and not merged on this
  branch yet.
- **Access health**: the MCP's report of access problems, read with the admin API's
  `access-health` operation and shown by `vecten issues`.
- **Reconciliation**: the MCP reapplying the desired grants to the warehouse, every fifteen
  minutes and on `vecten reconcile` ([access and enforcement](mcp-identity.md#access-and-enforcement)).
- **Model swap**: the MCP loading a newly shipped semantic model.

## What works today and what does not yet

| Part | State |
| --- | --- |
| Declaring an app, recording it with `vecten lock`, binding it with `extension add` | Built |
| A `container` app rendered as a service from the project image, on its own hostname | Built |
| The app principal, `vecten app connect`, and the app's credentials delivered to its service | Built |
| Data scope: the app principal reads only its declared datasets | Built |
| Admission, access health and `vecten issues` entries | Built |
| The viewer recorded in the audit | Built |
| The Analytics screen, the admin API read, the `list_apps` tool and the `/apps` page | Built |
| The gate in front of an app's hostname | Not merged yet. Until it is, NGINX answers every request to the app's login route with `503`; only the service's health path, `/_vecten/health`, is forwarded |
| Publishing a `snowflake` app (Streamlit in Snowflake) | Specified, not built: it needs warehouse resources that a later change adds ([#40](https://github.com/vecten/vecten-open/issues/40)) |
| `vecten ship` listing a `snowflake` app as not published | Specified, not built; the Analytics screen lists it as not published |
| `USAGE` grants on a Streamlit object for the admitted groups | Specified, not built (with #40) |
| Restricted-area grants ([#51](https://github.com/vecten/vecten-open/issues/51)) counted by admission | Specified, not built; only dataset and schema grants count |
| A service's health shown on the Analytics screen | Not built: `vecten ship` prints it, and nothing passes it to the MCP |
| A laptop app reading data | Not built: see [On the laptop](#on-the-laptop) |
| Snowflake's own grammar refusing `LATERAL TABLE('<relation>')` | The app SQL check refuses it either way; the test that checks Snowflake itself (`test_app_sql_snowflake_live.py`) needs a live account and has not run |
| A viewer mode, where an app reads as the viewing person | Not built; a later option (see [Every viewer sees the same data](#every-viewer-sees-the-same-data)) |

## Declaring an app

An extension declares each app as an entry point in the group `vecten.apps`. The entry point's
name is the app's name. Its object is a dictionary, or a factory with no arguments that
returns one:

```python
{
    "title": "Sales overview",
    "description": "Weekly bookings by region",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme_sales/app.py",
    "datasets": ["orders", "customers"],
    "groups": ["sales"],
}
```

```toml
[project.entry-points."vecten.apps"]
overview = "acme_sales.apps:overview"
```

The app's name follows the service name rule. It starts with a lowercase letter, followed by
up to 30 lowercase letters, digits or hyphens.

| Key | Meaning |
| --- | --- |
| `title`, `description` | What the listings show. The title must not be empty. |
| `kind` | `container` (a web app run by Vecten) or `snowflake` (Streamlit in Snowflake). |
| `datasets` | A non-empty list of distinct Ossie dataset names. These are the only data the app reads. |
| `groups` | A non-empty list of the MCP groups `extension add` suggests when it binds the app. |
| `runtime`, `entry` | `container` only. `runtime = "streamlit"` with `entry`, a `.py` file inside the installed package written as `<package>/<path>`. |
| `command`, `port`, `health` | `container` only, for any other web framework: the command, the port it listens on and its health check. `health` defaults to `{"path": "/"}`. |
| `components`, `memory_mb`, `cpus` | `container` only, the same as for a service. `memory_mb` defaults to 512. |
| `entry` (snowflake) | Required for a `snowflake` app: its main file, as `<package>/<path>.py`. |

`vecten lock` records each declaration in `extensions.lock.json`. It refuses a known key with a
bad value and names the extension, the app and the key. Some keys conflict and are also refused:
container keys on a `snowflake` app, `command`, `port` or `health` beside `runtime`, and `entry`
beside `command`. An extension may not declare a service named `app-<app>` beside its app
`<app>`: both would run as the same service.

**The Streamlit runtime.** Vecten runs
`python -m vecten_shared.launch streamlit <entry>`. The launcher uses the standard library only.
It finds the package without importing it and refuses a path outside the package. Then it
replaces itself with `python -m streamlit run <file>` and these flags:
`--server.port $PORT`, `--server.address 0.0.0.0`, `--server.headless true`,
`--browser.gatherUsageStats false`, `--server.enableXsrfProtection true` and
`--server.baseUrlPath ""`. The app listens on port 8501, and its health path is
`/_stcore/health`. Streamlit and the app's other packages go in the extension's `platform` extra
(or `mcp`, if the app runs on the MCP host); see
[dependencies per component](extensions.md#dependencies-per-component).

A minimal `acme_sales/app.py`, using the client in
[the app principal and its data](#the-app-principal-and-its-data) saved as
`acme_sales/mcp_client.py`:

```python
import asyncio

import pandas as pd
import streamlit as st

from acme_sales.mcp_client import revenue_by_region

st.title("Sales overview")
# The gate passes the viewer's email as X-Vecten-User once it is merged.
viewer = st.context.headers.get("X-Vecten-User", "")


@st.cache_data(ttl=300)
def load():
    # Every admitted viewer sees the same data, so one cached result serves them all.
    result = asyncio.run(revenue_by_region(viewer))
    return pd.DataFrame(result["rows"], columns=result["columns"])


st.bar_chart(load(), x="region", y="revenue")
```

## Binding an app in vecten.toml

A declared app runs only while `vecten.toml` binds it:

```toml
[apps.acme-sales.overview]
groups = ["sales", "finance"]   # who may open it; never empty
hostname = "sales"              # container only; defaults to the app's name
local = true                    # container only; whether `vecten up` runs it
```

`vecten extension add` writes a binding for each declared app. It sets `groups` from the
declaration's suggestions. For a `container` app it also sets `hostname`, taking the first free
label from `<app>`, `<extension>-<app>` and numbered variants. It never changes an existing
binding. It leaves an app unbound, with a note, in these cases: the project has no `mcp`
component, the project enables none of the app's components, or another app already holds
the same principal name.

Loading `vecten.toml` refuses:

- an app binding in a project without the `mcp` component;
- a binding under an extension that `extensions` does not list;
- a hostname bound twice, across services and apps;
- two apps whose principal names fold to the same name. The principal name replaces every run of
  characters other than letters and digits with one `_`, so `a-b` and `a--b` both become
  `svc_app_a_b`, and `overview-` becomes `svc_app_overview`.

The next commands that read the record (`up`, `ship`, the bundle) refuse a binding for an app
the record does not declare. They also refuse `hostname` or `local` on a `snowflake` app.

Who may open an app is decided in `vecten.toml` only: change it in a pull request and ship.
The admin panel shows bindings but does not edit them.

## How a container app runs

A bound `container` app runs as the service `svc-<extension>-app-<app>`, from the project image
of the first of its `components` that the project enables. It follows every rule of an
extension service: isolation, limits and health check. It gets one `login` route at `/` for
its bound groups, with WebSockets kept open. It needs no `[services]` table. A new hostname
needs `vecten deploy` once before `ship` serves it.

The service receives:

- `PORT` and `VECTEN_PUBLIC_URL` (`https://<hostname>.<domain>`), like any service.
- `VECTEN_MCP_URL`, `https://mcp.<domain>/mcp`. It is set on a deployed host only.
- The app principal's client id and secret. On the platform host these are
  `APP_<APP>_CLIENT_ID` and `APP_<APP>_CLIENT_SECRET`. On the MCP host they are
  `MCP_APP_<APP>_CLIENT_ID` and `MCP_APP_<APP>_CLIENT_SECRET`. `<APP>` is the app's name
  upper-cased, with `-` as `_`. The prefix puts each name in the secrets scope of the host
  that runs the app. Only that app's service receives them. No extension or project may
  declare a secret with one of these names, and no connector may either.

## The app principal and its data

The app authenticates to the MCP with client credentials. It sends a `POST` to
`https://mcp.<domain>/token` with `grant_type=client_credentials`, its client id and secret,
and `resource=https://mcp.<domain>/mcp`. The access token lasts an hour.

**Which roles it reads with.** The principal is a member of the app's bound groups. It queries
with the warehouse roles of only those groups that the app admits at that moment (see
[who may open an app](#who-may-open-an-app)). A bound group the app does not admit, and any
other group, never feeds a query. The MCP grants the principal those roles in the warehouse
and revokes the others whenever admission or its membership changes. On Postgres, a query
from an app principal is refused if its warehouse user holds any other role, for example one
granted by hand. The principal cannot be added to a group outside its app's binding. The
column deny lists of its groups apply combined, as they do for anyone in several groups.

A role the MCP fails to revoke stays on its record, so the next sync (every reconciliation)
retries it and deleting the principal revokes it. Access health reports a failed sync, and any
warehouse role the principal holds beyond its admitted groups' roles where the warehouse can
list them.

What the MCP does not check: privileges and row policies that a warehouse administrator grants
directly to the principal's own role (`svc_app_<app>`) or to `PUBLIC`. Those apply to the app
like to anyone. On Postgres, the role check runs in the query's transaction just before the
statement, at `READ COMMITTED`. A role an administrator grants between the check and the
statement can take effect for that one query.

On top of its roles, the MCP holds it to the app's declared datasets:

- **Tools.** An app principal sees only `describe`, `search`, `sql-exec` and `query_metrics`.
- **`describe` and `search`** show only the declared datasets, their metrics and the
  relationships between two declared datasets. An undeclared dataset gets the same answer as
  an unknown one.
- **`query_metrics`** refuses a metric whose home dataset is not declared. The refusal is the
  same whether or not the metric exists.
- **`sql-exec`** is checked before the warehouse sees the statement. The statement must be one
  `SELECT` or `WITH` query, with no comments. Every table it reads must be the table of a
  declared dataset, written with the same parts. An unqualified name counts only as a CTE that
  a `WITH` makes visible at that point. Subqueries, `VALUES`, `UNNEST` and
  `LATERAL (subquery)` are allowed. Table functions, stages, `TABLE(...)`, `ROWS FROM`,
  `OPERATOR(...)`, casts to user-defined or `reg*` types, and any function off the warehouse's
  list are refused.
- **The function list.** Each warehouse has its own list of reporting built-ins, and anything
  else is refused. The list covers common aggregates (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`,
  `MEDIAN`, `STDDEV`, `PERCENTILE_CONT`, `ARRAY_AGG`, `STRING_AGG`), window functions
  (`ROW_NUMBER`, `RANK`, `LAG`, `LEAD`), text functions (`LOWER`, `UPPER`, `TRIM`,
  `SUBSTRING`, `CONCAT`, `REPLACE`, `SPLIT_PART`, `REGEXP_REPLACE`), `CASE`, `CAST`,
  `COALESCE`, `NULLIF`, `ROUND`, `CURRENT_DATE`, `CURRENT_TIMESTAMP`, `DATE_TRUNC` and
  `EXTRACT`. Snowflake's list adds functions such as `DATEDIFF`, `YEAR`, `IFF` and `LISTAGG`.
  Functions such as `AGE`, `TO_TIMESTAMP` and `HASH` are not on either list. The full list,
  as SQLGlot classes, is `FUNCTIONS` in `vecten_mcp/app_sql.py`.
- **A dataset whose source is a query** names no table for `sql-exec`. Read it through
  `query_metrics`, which applies that query.
- **The row cap** (the `row_cap` setting, 1000 by default, at most 10000) applies to every app
  query, as it does to anyone's. An app that needs more rows should aggregate in SQL or read
  metrics.

### Every viewer sees the same data

The app reads as its principal, not as the person viewing it. All viewers of one app share one
view: what the union of the admitted groups' roles may read. At the table level this is safe,
because every admitted group may read every declared dataset. Row access policies, row-level
security and column masks are evaluated against that union, though. If two admitted groups see
different rows or masks of the same table, the app shows each viewer the union, whichever
group admitted them. Bind such groups to separate apps. A viewer mode, where the app reads as
the viewing person, is a later change.

**The viewer is audited.** An app may pass the viewing person's email in an
`X-Vecten-On-Behalf-Of` header; once the gate is merged, the app reads it from the
`X-Vecten-User` header the gate sends. The MCP records it, lower-cased, as `on_behalf_of` in the
audit row, and only for an app principal. The header must appear once, hold at most 762 ASCII
characters, and percent-decode to one visible `local@domain` of at most 254 characters.
Otherwise nothing is recorded. The MCP never uses it to decide anything.

No per-address rate limit applies to an app's tool calls. Only its token request is limited,
like everyone's, to 60 per minute from one address.

A minimal client, for the MCP Python SDK 2.x (`mcp`, which brings `httpx2`) in the app's
extra; with another client library, the protocol is the same:

```python
import json
import os
import time
from urllib.parse import quote

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP = os.environ["VECTEN_MCP_URL"]  # https://mcp.<domain>/mcp
ISSUER = MCP.removesuffix("/mcp")
_token = {"value": "", "expires_at": 0.0}


async def token():
    """The access token, requested again a minute before it expires."""
    if _token["expires_at"] - 60 > time.time():
        return _token["value"]
    async with httpx2.AsyncClient(timeout=30) as http:
        response = await http.post(
            ISSUER + "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ["APP_OVERVIEW_CLIENT_ID"],
                "client_secret": os.environ["APP_OVERVIEW_CLIENT_SECRET"],
                "resource": MCP,
            },
        )
    response.raise_for_status()
    body = response.json()
    _token.update(value=body["access_token"], expires_at=time.time() + body["expires_in"])
    return _token["value"]


async def revenue_by_region(viewer):
    headers = {"Authorization": "Bearer " + await token()}
    if viewer:
        headers["X-Vecten-On-Behalf-Of"] = quote(viewer, safe="@")
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30, read=300)) as http:
        async with streamable_http_client(MCP, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "query_metrics", {"metrics": ["revenue"], "dimensions": ["region"]}
                )
    if result.isError:
        raise RuntimeError(result.content[0].text)
    return json.loads(result.content[0].text)
```

## Who may open an app

A bound group **admits** people to an app only if it may read every dataset the app declares.
A group may read a dataset in one of two ways:

- a dataset grant on it, applied to the table the dataset reads now; or
- a schema grant on the schema of every table its source reads.

A grant counts once it is both desired and applied. A revoked grant or a grant the warehouse
refused admits no one. On Snowflake, a three-part source counts only in the project's own
database. A bound group that does not exist admits no one. A declared dataset that the model
does not hold admits no group: the app cannot be opened until the model holds it.

The MCP recomputes admission at startup (a ship restarts it with the new bundle), after every
reconciliation and model swap, and after every grant or group change.

Each bound group that is left out becomes an **access-health** entry, and `vecten issues`
reports it with a fix. For example: "group `default` may open `acme-sales.overview` but cannot
read `marts_finance.revenue`". The fix is `vecten grant <dataset> --group <group> --env <env>`,
or removing the group from the binding and shipping. Access health also lists a service
principal named `svc_app_<app>` for a bound app that is not marked as that app's principal: it
reads everything its groups can. Review who uses it, then run `vecten app connect` with
`--adopt`, or delete it.

Admission is computed now. Once the gate is merged, it admits a person at an app's hostname
only through these groups.

Access health also lists an app principal whose warehouse roles may exceed its admitted
groups': a role sync that failed (`vecten reconcile --env <env>` retries it) and roles the
warehouse reports beyond them (a warehouse administrator revokes those).

An app principal whose groups differ from its app's binding, as after the binding changed, is
an access-health entry too. `vecten app connect <extension>.<app> --env <env> --groups-only`
sets its groups to the binding without changing its credentials.

## vecten app connect

```sh
vecten app connect acme-sales.overview --env prod
```

The command needs an admin session from `vecten login --env <env>`. `--env` defaults to `prod`.
It works in this order:

1. It refuses, before asking the MCP, an app that `vecten.toml` does not bind, a `snowflake`
   app, an environment that is not configured, and a missing session (naming `vecten login`).
2. It calls the admin API's `apps/connect`. The MCP reads the binding from the bundle it runs,
   so an app bound in your checkout but not shipped yet is refused there.
3. The MCP creates `svc_app_<app>` under exactly that warehouse name, or reuses the existing
   one. It marks the principal as the app's and makes it a member of the bound groups and no
   others: a new principal leaves `default` unless the app is bound to it. In the warehouse it
   holds only the roles of the groups the app admits (see
   [which roles it reads with](#the-app-principal-and-its-data)). Last, it issues new client
   credentials, which replace every earlier client of the principal and end its sessions.
4. The command stores the client id and secret under the app's credential names. With `--env
   <env>` they go to that environment's secrets store, and the name puts them in the scope of
   the app's host. With `--env local` they go to `.env`. The output names both secrets and
   never prints their values.

Refusals from the MCP: a bound group that does not exist (create it first), a disabled
principal (delete it first), a principal marked for another app, and an existing unmarked
`svc_app_<app>` without `--adopt`. With `--adopt`, that principal is taken over: its groups
become the app's, and its credentials are replaced. When a warehouse account named
`svc_app_<app>` exists but no MCP principal does, the command asks for `--adopt` too.
Adoption is refused while the account holds a role other than those of the bound groups, of
the groups the principal is in now (the connection revokes them), of `default`, and
Snowflake's `PUBLIC`.

**After changing an app's groups** in `vecten.toml`, ship, then run
`vecten app connect <extension>.<app> --env <env> --groups-only`. It sets the principal's
groups to the new binding and keeps its credentials, so the running app keeps working. It
needs an existing, marked principal, and it cannot be combined with `--adopt`.

**Running it again rotates the secret.** The previous secret stops working at once, so the
running app cannot authenticate until `vecten ship --env <env>` delivers the new one (on the
laptop, until `vecten up`). If storing fails after the rotation, the command names what it
stored and what it did not. Running it again rotates once more and stores both.

`vecten next` offers the command after the admin sign-in item. Signed in, it asks the MCP and
offers it only for a shipped app with no principal. When it cannot ask, it relies on this
checkout's record of stored secrets and says that the command rotates the secret of an app
that is already connected.

## On the laptop

`vecten up` runs a bound container app whose binding has `local = true`, at
`https://<hostname>.localhost:8443`. `vecten app connect <extension>.<app> --env local` stores its
credentials in `.env`, and the service receives them at the next `vecten up`. **A laptop app
reads no data.** No container on the laptop can reach `https://mcp.localhost:8443`: that name
resolves to the container itself, NGINX listens on another port inside the network, and the
certificate is the laptop's self-signed one. So `VECTEN_MCP_URL` is not set on the laptop.
Develop the app's data access against a deployed environment.

## Where apps are listed

- **The Analytics screen** (`/admin/apps`) lists every bound app. For each it shows the title,
  extension and version, kind, URL, bound and admitted groups, declared datasets, the app
  principal's state, the binding's place (`[apps.<extension>.<app>]` in `vecten.toml`) and the
  app's access-health entries. A `snowflake` app is shown as not published, naming the
  warehouse resources it needs. The screen is read-only. See the
  [admin panel](admin-panel.md#screens).
- **The admin API**, `GET /api/admin/apps`, and the admin tool `list_apps` return the same list.
  An agent can use either to answer "which dashboards exist and who can open them".
- **`/apps` on the MCP host** lists, with links, the container apps that admit one of the
  signed-in person's current groups, and nothing about any other app. A person signs in there
  through the MCP's own sign-in with a client of its own (`vecten-apps-page`). The admin flag
  plays no part.

The `/apps` session cookie, `vecten_apps`, is `HttpOnly`, `Secure` and `SameSite=Strict`. It
is scoped to `/apps`, and it cannot be replayed as an admin panel session or the other way
round. It cannot use the `__Host-` prefix, which requires `Path=/` and would send the cookie
to `/mcp` and `/admin` too. As a result, another hostname under the same domain could plant one.
The only effect would be to sign that browser in to `/apps` as the person who planted it,
who sees only their own list.

Back to the [docs index](README.md).
