Starts after `extension-services` (#38) is merged. Items marked **[deploy]** need a real
deployment with DNS and certificates; they are listed again in section 6.

Pull requests (rough, one engineer): F the app declaration and container apps (3 days),
G data scoping, admission and audit in the MCP (4 days), H listing and `app connect` (3 days),
I docs and the end-to-end proof (1 to 2 days plus deploy time). The `snowflake` kind's
publishing is a later change with #40 (section 5).

## 1. App declaration and container apps (PR F)

- [x] 1.1 `vecten_shared.extensions`: the `vecten.apps` group; declaration parsing per kind; #50's `vecten lock` records it
- [x] 1.2 `vecten_shared.config`: `[apps.<extension>.<app>]` with its refusals (undeclared app, no `mcp` component, empty groups); `extension add` writes bindings and keeps existing ones
- [x] 1.3 Render a bound container app as the #38 service `svc-<extension>-app-<app>` with one `login` route at `/`, `websocket = true`, its credential names and `VECTEN_MCP_URL`
- [x] 1.4 `vecten_shared.launch`: standard library only; resolves `entry` inside the installed package, refuses a path outside it, and execs Streamlit with the flags in the spec
- [x] 1.5 Tests: declaration and binding validation; rendered Compose and NGINX for a container app; byte-identical output without apps; the launcher's argument list and path checks; the stub extension's twelve groups; a Docker-tier test that a minimal Streamlit app from a fixture extension starts as its generated service from the platform image, answers `/_stcore/health`, keeps its WebSocket (directly), and that its page makes no request to Streamlit's usage-statistics endpoint
- [ ] 1.6 Docker-tier test of the app under `up` with its WebSocket kept through NGINX: needs the gate (extension-services PR D), since a `login` route answers 503 until the gate serves it

## 2. Data scoping, admission and audit in the MCP (PR G)

- [x] 2.1 The server reads bound apps from the shipped `vecten.toml` and `extensions.lock.json`; app principals are marked by `vecten app connect` (the stored marker, a `records` row of kind `app_principals` per principal naming its extension and app, is defined here; `vecten app connect` sets it in 3.4)
- [x] 2.2 Scope app principals to declared datasets in `sql-exec` (from the parsed table list), `query_metrics` (home dataset), `describe` and `search`; declared datasets missing from the model in model health
- [x] 2.3 Admitted groups per app (dataset, schema and, with #51, restricted-area grants), recomputed at reconciliation, model swap and ship; access-health entries (the gate's part is 2.7)
- [x] 2.4 `X-Vecten-On-Behalf-Of` recorded in the audit for app principals only (state migration for the audit column)
- [x] 2.5 Check that no per-address limit applies to app principals' tool calls (design.md, Risks)
- [x] 2.6 Tests: refusals for undeclared relations (including a CTE, a subquery and a quoted name), an undeclared metric, filtered `describe` and `search`; admission with a group lacking a grant and after the grant is added; header recorded for an app principal and ignored for a person; Postgres and Snowflake warehouse fixtures where the repository's test tiers allow
- [ ] 2.7 The gate's verify admits a person to an app's route only through the app's admitted groups (`AppScopes.admitted`), and the gate reads app routes from `[apps]`: needs the gate (extension-services PR D), which this branch does not carry yet

## 3. Listing and app connect (PR H)

- [x] 3.1 Admin API read operation and admin tool for the app list
- [x] 3.2 Panel "Analytics" screen, read-only, naming the binding
- [ ] 3.3 `/apps` page for signed-in people, filtered by admitted groups
- [ ] 3.4 `vecten app connect`: create or rotate the principal, set groups, store credentials (cloud scope by host, `.env` for local), never print the secret; `next` items
- [ ] 3.5 Tests: admin API and tool parity; panel rendering with a container app and an unpublished Snowflake app; `/apps` filtering; `app connect` against a fake admin API and fake secrets store (creation, rotation, refusals, no secret in output)

## 4. Docs and end-to-end proof (PR I)

- [ ] 4.1 New `docs/analytics-apps.md`: the group, the kinds, the binding, the data model (one view per admitted group, the row cap, the consistency check), the Streamlit recipe and an MCP client sketch, `app connect`
- [ ] 4.2 `docs/extensions.md` table row; `docs/admin-panel.md` Analytics screen; `docs/GLOSSARY.md` ("Analytics app", "App principal"); `CHANGELOG.md`
- [ ] 4.3 **[deploy]** On AWS with a Snowflake warehouse and on Scaleway with Postgres: a Streamlit template app is shipped, `vecten deploy` creates its hostname, a member of its group opens it through the gate and sees data read as `svc_app_<app>`, a person outside it is refused, and the audit names the viewer
- [ ] 4.4 **[deploy]** With #51's restricted area: a finance dashboard bound to `["finance", "default"]` admits finance members only and access health names `default`

## 5. Streamlit in Snowflake (later change, with #40)

- [ ] 5.1 Specify with #40: `apps` schema, stage, app role and warehouse; the provisioner procedure that uploads files and re-creates the Streamlit object owned by the app role
- [ ] 5.2 Verify current Snowflake behaviour: owner's versus caller's rights for Streamlit, the `CREATE STREAMLIT` source syntax, and `USAGE` grants to account roles
- [ ] 5.3 Reconcile `USAGE` on the Streamlit object for admitted groups and `SELECT` for the app role (requirement already in this change's delta)

## 6. Proven only by a real deploy

- [ ] 6.1 Task 4.3: end-to-end Streamlit template on both clouds
- [ ] 6.2 Task 4.4: restricted area and admission
- [ ] 6.3 The `snowflake_live` run of `packages/vecten-mcp/tests/test_app_sql_snowflake_live.py`: whether Snowflake's grammar itself refuses `LATERAL TABLE('<relation>')`, which the app SQL check refuses (written, not run: it needs the snowflake-live workflow's account)
