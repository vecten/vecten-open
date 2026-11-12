## Context

This change answers #39: a generic place for analytics apps. It is built on `extension-services`
(#38) and lands after it. The reasons it is a separate change rather than part of #38: #38 is
needed by the bot and portal templates, which need none of what is below, and the data-access
decisions here touch the MCP's access control, which #38 does not.

**The product model.** A dashboard template, typically Streamlit, is copied into the client's
project under `extensions/<name>/` and customised there (change `project-extensions`), or an
extension from an index ships one. Either way the app's source is package data of an installed
extension, present in both project images.

**What exists to build on:**

- #38 gives a service: a process from a project image, on a hostname of its own, with a `login`
  route behind the MCP's gate that admits named groups and passes `X-Vecten-User`,
  `X-Vecten-Groups` and the other identity headers. Services hold no cloud identity and receive
  only declared secrets.
- `mcp/access-control`: groups are the unit of authorization; each maps to one warehouse role;
  dataset and schema grants are desired state in `mcp_state`, reconciled; the warehouse is the
  only enforcement point. Restricted marts (#51, `spec/restricted-marts`) add restricted areas
  (`core_<area>`, `marts_<area>`) that no group reads until granted, declared in `vecten.toml`
  under `[areas.<name>]`.
- `mcp/warehouse-identity`: every query runs as the principal's own warehouse user. A service
  principal `svc_<name>` authenticates with client credentials and has its own warehouse user,
  impersonated on Postgres and token-authenticated on Snowflake; no principal holds a warehouse
  secret of its own. The identity contract already names a service principal for a "dashboard
  host" as an example.
- `mcp/mcp-tools`: `sql-exec` parses each statement and records referenced tables and columns;
  `query_metrics` compiles metrics against home datasets; results are capped by a row cap.
- `mcp/admin-panel`: server-rendered screens under `/admin`; every panel action is also an admin
  API call; read operations are also admin tools. The model view is read-only and names where a
  change belongs.
- Edition boundary: everything open; no licence checks; no telemetry in open code.

## Goals / Non-Goals

**Goals:**

- An extension declares an analytics app once; `ship` publishes a self-hosted one with the
  project.
- Who may open an app, and what data it shows them, both follow the MCP's groups, including
  restricted marts.
- Admins and people can see what apps are installed and open the ones they may.
- A Streamlit template works end to end with no BI tool in the product.

**Non-Goals:**

- A BI tool, a chart library, or a dashboard editor in the product.
- Creating warehouse objects for Streamlit in Snowflake (#40). This change defines the kind and
  its access model; publishing waits for #40.
- Per-person row-level data in self-hosted apps (Decision 3 explains the deferred option).
- Embedding apps in the admin panel.

## Decisions

All decisions were accepted by the maintainer on 2026-11-12, each as recommended, and are ordered
with the most consequential and least reversible first.

### Decision 1: Build on #38: a container app is a service with one `login` route, declared once as an app — *Accepted*

**Context.** A self-hosted Streamlit app needs exactly what a portal needs from #38. The question
is whether an app is a kind of service, or a separate thing that may produce one.

**Options.**

1. *No new group*: an extension declares a #38 service and flags one route `app = {...}` with a
   title and datasets. The smallest surface, but a Snowflake app has no process and no route,
   so it would need a second mechanism anyway.
2. *A `vecten.apps` group whose `container` kind generates the service.* The app declares its
   title, description, datasets, `kind` and either a `runtime = "streamlit"` shorthand with an
   `entry` file or a generic `command` and `port`. Vecten renders the #38 service
   `svc-<extension>-app-<app>` with one `login` route at `/`, `websocket = true`, the declared
   health path. A Snowflake app is the same declaration with `kind = "snowflake"`.
3. *Apps as metadata only* (a listing), deployment left to the extension's own services. The
   listing then cannot enforce access or data scope.

**Accepted: option 2.** The declaration:

```python
{
    "title": "Sales overview",
    "description": "Weekly bookings by region",
    "kind": "container",                     # or "snowflake"
    "runtime": "streamlit",                  # container only; or give "command" and "port"
    "entry": "acme_sales/app.py",            # a file in the installed package
    "datasets": ["orders", "customers"],     # Ossie dataset names it reads
    "groups": ["default"],                   # suggested viewers
    "components": ["platform"],
    "memory_mb": 512,
}
```

and the project's binding:

```toml
[apps.acme-sales.overview]
groups = ["sales", "finance"]
hostname = "sales"        # container only; defaults to the app's name
local = true              # container only
```

`vecten lock` records the declaration in `extensions.lock.json`; `extension add` writes the
binding. A container app's generated service follows every #38 rule (image, isolation, limits,
health, laptop) and needs no `[services]` table: the app binding is its binding. An app's
`groups` must be non-empty.

### Decision 2: Data access: the app's own principal through the MCP, restricted to its declared datasets — *Accepted*

**Context.** "Analytics apps must read data through the same group-based access as the MCP."
Three identities could run an app's queries. The warehouse is the only enforcement point, and
neither a person's nor a service principal's warehouse user has a credential anyone but the MCP
holds (Postgres: impersonated; Snowflake: MCP-minted tokens).

**Options.**

1. *Through the MCP as the viewing person.* The gate would hand the app a short-lived MCP access
   token for the viewer, and every query runs as the viewer's warehouse user, so row policies,
   masks and restricted marts apply per person. Costs: the app holds each viewer's token while
   they use it; results cannot be cached across viewers, which most dashboards rely on; the row
   cap applies per query; the MCP must mint tokens for an audience it does not serve today.
2. *As the viewer's warehouse identity directly.* The MCP would hand the app a warehouse token
   (Snowflake) or impersonate for it (Postgres). It moves warehouse credentials out of the MCP,
   which `mcp/warehouse-identity` rules out, and does not work on Postgres at all.
3. *A service identity per app, through the MCP.* The app authenticates as `svc_app_<app>` with
   client credentials and calls `query_metrics` or `sql-exec`. The principal is a member of the
   app's bound groups, so its warehouse user holds their roles. The MCP restricts it to the
   app's declared datasets: `sql-exec` refuses a statement referencing any other relation, using
   the table list it already parses; `query_metrics` refuses a metric whose home dataset is not
   declared; `describe` and `search` show only declared datasets. The gate admits a viewer only
   through a bound group that may itself read every declared dataset (by dataset grant, schema
   grant or restricted-area grant), so no viewer sees data their groups could not read.
   Results can be cached across viewers.

**Accepted: option 3**, with two additions:

- **The viewer is audited.** The app may pass the viewer's `X-Vecten-User` to the MCP in an
  `X-Vecten-On-Behalf-Of` header; the MCP accepts it only from an app principal, records it in
  the audit row beside the principal, and never uses it to authorize.
- **Breaches are visible.** The MCP computes, at every reconciliation and every model swap, the
  bound groups of each app that cannot read all its datasets, leaves them out of the gate's
  admission for that app, and lists each as an access-health failure ("group `default` may open
  `acme-sales.overview` but cannot read `marts_finance.revenue`"). With #51 this is what keeps a
  dashboard from showing restricted marts to the default group.

Consequences: an app sees the same rows for every viewer it admits, so per-person row policies
do not apply inside an app. The docs say so, and option 1 stays open as a later
`data_access = "viewer"` mode for apps that need it. The row cap applies to app queries; an app
that needs more aggregates in SQL or reads metrics.

### Decision 3: Two runtime kinds; the first template targets `container` — *Accepted*

**Context.** The issue names Streamlit in Snowflake and a hosted Streamlit app.

**Options for the first template.**

1. *`container` (self-hosted Streamlit through #38).* Works on Postgres and Snowflake projects
   alike; access through the gate with no warehouse change; data through the MCP (Decision 2);
   runs on the laptop under `up`; needs nothing from #40. Cost: memory on the host, and
   Streamlit's dependencies must resolve against the platform image (#38's spike).
2. *`snowflake` (Streamlit in Snowflake).* No host resources, and Snowflake's own UI. But it
   needs a schema or stage, a warehouse, the Streamlit object and an owning role (#40); access is
   a Snowflake grant, so viewers need Snowflake sign-in (the MCP provisions person-type users
   without a credential, so this works only where the client has Snowflake single sign-on); and
   it is Snowflake-only.

**Accepted: option 1 for the first template.** `snowflake` is declared and listed now, so
templates can carry it, and its publishing is specified with #40 (Decision 5).

### Decision 4: Who may open an app is its bound `groups`, in `vecten.toml` — *Accepted*

**Context.** Dataset grants live in `mcp_state` and admins edit them in the panel; connector and
restricted-area access is declared in `vecten.toml` and seeded. #38 reads route groups from the
bundle's `vecten.toml`.

**Options.** (1) `vecten.toml` is authoritative, read by the MCP from the bundle; a change is a
pull request and a ship. (2) `vecten.toml` seeds desired state in `mcp_state` that admins then
edit in the panel, like connector access.

**Accepted: option 1**, for the same reason as #38's routes: the gate reads route groups
from the bundle, and an app's viewers and its datasets change together in one reviewed commit.
The panel shows them read-only and names the file, as the model view does. If clients ask for
panel edits, option 2 can be added for apps and routes together.

For a `snowflake` app, the MCP grants `USAGE` on the Streamlit object to each admitted group's
role as reconciled desired state, and revokes it from any other role it manages.

### Decision 5: Publishing: `ship` for `container`, a #40-backed step for `snowflake` — *Accepted*

- **`container`.** `ship` renders and starts the app's service like any #38 service; a new
  hostname needs `vecten deploy` once (#38, Decision 3). `up` runs it on the laptop.
- **`snowflake`**, specified here, implemented after #40. Direction for that follow-up:
  `deploy` creates, through #40's warehouse resources, an `apps` schema with a stage per app and
  an app role `app_<app>` the MCP's provisioner manages; the MCP grants `app_<app>` `SELECT` on
  exactly the declared datasets (reconciled like a group role) and owns nothing else. On each
  ship, the MCP host uploads the app's files from the installed package to its stage and
  re-creates the Streamlit object owned by `app_<app>`, then grants `USAGE` to the admitted
  groups' roles. Because Streamlit in Snowflake runs with its owner's rights, the app role is
  its service identity, the counterpart of Decision 2. Until #40 lands, `vecten lock` accepts the
  kind, the panel lists the app as "not published (needs #40)", and `ship` reports it.

### Decision 6: Where apps are listed — *Accepted*

- **Admin panel, "Analytics".** Every bound app: title, extension and version, kind, URL, bound
  and admitted groups, declared datasets, the service's last reported health, and access-health
  failures. Read-only, naming `vecten.toml` for changes.
- **Admin API and admin tool.** `GET` of the same list, so an agent can answer "which dashboards
  exist and who can open them".
- **`/apps` on the MCP host.** For any signed-in person, the apps they may open, with links. A
  portal template can link to it instead of keeping its own list.
- **Docs.** `docs/analytics-apps.md`: the group, the two kinds, the data model, the Streamlit
  recipe.

### Decision 7: The minimum for a Streamlit template, and the launcher — *Accepted*

**Context.** No BI tool is built in, so the product's part must be small.

**Accepted.** The product provides:

1. #38: services, a hostname, the gate.
2. The `container` kind with `runtime = "streamlit"`: Vecten renders the command
   `python -m vecten_shared.launch streamlit <entry>`. The launcher, standard library only and
   importing no Streamlit, resolves `<entry>` inside the installed package and replaces itself
   with `python -m streamlit run <path>` with `--server.port $PORT`, `--server.address 0.0.0.0`,
   `--server.headless true`, `--browser.gatherUsageStats false`, `--server.enableXsrfProtection
   true` and `--server.baseUrlPath ""`. Health is `/_stcore/health`; the route has
   `websocket = true`.
3. The app principal: `vecten app connect <extension>.<app> --env <env>` creates
   `svc_app_<app>` through the admin API, sets its groups to the app's bound groups, and stores
   its client id and secret under names the product reserves for the app and delivers to its
   service with no manifest entry: `APP_<APP>_CLIENT_ID` and `APP_<APP>_CLIENT_SECRET` on the
   platform host, `MCP_APP_<APP>_CLIENT_ID` and `MCP_APP_<APP>_CLIENT_SECRET` on the MCP host, so
   #49's name-to-scope rule puts them where the app's host reads. The service also gets
   `VECTEN_MCP_URL`. Run again, the command rotates the secret. `next` offers it for every
   environment where the app is bound and the principal is missing.
4. Dataset scoping and the consistency check in the MCP (Decision 2).
5. The listing (Decision 6).

The template provides the app, its `platform` extra (`streamlit` and an MCP client), and a small
module that obtains a token with client credentials and calls `query_metrics` or `sql-exec` over
the MCP's streamable HTTP endpoint at `https://mcp.<domain>/mcp`, caching results with
Streamlit's cache. A shared Python client in the product is not proposed now: the MCP's
protocol is the contract, and the template can carry its own.

## Risks / Trade-offs

- [Streamlit's dependencies against the Airflow image] → #38's spike 1.1; fallback is #38's
  third image, which apps would use unchanged.
- [Apps show one view to every admitted viewer] → Documented; the gate admits only groups that
  may read all declared datasets; a viewer mode is a later option.
- [Row cap limits app queries] → Documented; aggregate in SQL or use metrics.
- [Rate limits] → App queries reach the MCP from the host's public address (#38, Decision 13).
  The limits `mcp/mcp-auth` keys by address cover login, password and registration, which app
  principals do not use, and the client credentials token request, which they use once an hour
  each. A task checks that no other per-address limit applies to them.
- [Streamlit in Snowflake details are unverified] → Whether Snowflake now offers caller's rights
  for Streamlit, which would allow a viewer mode there, and the exact `CREATE STREAMLIT` source
  syntax for the pinned provider, are to be checked when #40 is designed.
- [`vecten.toml` as the only place to change viewers] → Admins need a pull request; the panel
  says so. Option 2 of Decision 4 is additive.

## Migration Plan

- Additive: projects without `[apps]` see no change in any rendered file or in the MCP's
  behaviour; tests assert it.
- Lands after `extension-services`. Its delta to "Extensions are Python packages with per-layer
  entry points" and to the stub requirement is written over #38's text.
- Existing service principals are unaffected: dataset scoping applies only to principals
  `vecten app connect` creates, which the MCP marks as app principals.

## Security considerations

- **Data scope.** An app principal is restricted by the MCP to declared datasets, on every tool,
  and the warehouse still enforces its group roles underneath.
- **Viewer admission.** Only through a bound group that may read every declared dataset, so an
  app is never a way around a dataset or restricted-area grant.
- **Audit.** Every app query is audited under the app principal, with the viewer recorded but
  never trusted for authorization.
- **Credentials.** The app's client secret is a declared secret delivered only to the app's
  service (#38, Decision 6); `vecten app connect` never prints it.
- **No telemetry.** The launcher turns off Streamlit's usage statistics, in keeping with the
  edition boundary.
- **Snowflake apps** run with owner's rights; the owner is an app role holding exactly the
  declared datasets, never `loader` or a group role.
