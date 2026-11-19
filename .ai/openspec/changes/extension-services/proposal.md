## Why

Issue: [#38](https://github.com/vecten/vecten-open/issues/38).

An extension cannot run a process that stays up, and nothing outside the two built-in hostnames
is reachable. Two common needs hit this: a chat bot (Teams, Slack) that receives webhooks and
posts notifications, and a small web portal with documentation and links. Analytics apps (#39)
need the same thing.

What blocks it today, as the code on `release/0.1.1` shows:

- `orchestration/docker-compose.override.yml` can add a service, and the VM agent applies it, but
  an override service without a profile starts on every host, and it cannot bring a build
  context, static files or an NGINX fragment: the bundle carries a fixed list of paths
  (`CONTENT` in `packages/vecten/src/vecten/deploy/bundle.py`).
- NGINX is generated on every ship (`packages/vecten/src/vecten/stack/nginx.py`). Its default
  server rejects the TLS handshake and answers 444, so no other hostname or path is reachable.
- DNS records and certificates exist only for `airflow.<domain>` and `mcp.<domain>`: the two
  Terraform modules derive the records from `components`, and the VM agent runs certbot for the
  hostnames `host.json` lists, which come from the same outputs.
- The `shared/extension-points` spec says hooks for HTTP routes "SHALL be added with their first
  consumer". This is that consumer.

Teams run such services on a separate host today, with their own reverse proxy and certificate.

## What Changes

- **A new entry point group, `vecten.services`.** Each entry declares one long-running process:
  its command, its HTTP port, a health check, an optional persistent volume, default resource
  limits, the components whose image it can run from, and its routes. `vecten lock` records the
  declaration in `extensions.lock.json` (#50), so `ship` renders from committed files without
  importing the extension.
- **The project decides how a service is deployed**, in `[services.<extension>.<service>]` of
  `vecten.toml`: its hostname label, the host it runs on, each route's access, groups and
  allow-list, its limits, and whether it starts on the laptop. `extension add` writes this table
  from the extension's suggestions; a service or route runs only while `vecten.toml` lists it.
- **A service runs from the project image of the component it is placed on**, which already has
  the extension installed with that component's extra. It runs in that component's Compose
  profile, so the VM agent starts it with no new profile, on the platform host by default.
- **Each routed service gets its own hostname**, `<label>.<domain>`, or the apex of `<domain>`.
  No extension route is ever served on `airflow.<domain>` or `mcp.<domain>`. `vecten deploy`
  passes the labels to Terraform, which creates the DNS records on both clouds beside the
  built-in ones and lists them in the outputs. `ship` adds them to the hostnames the VM agent
  issues certificates for, so certbot and the renewal timer cover them unchanged. A route whose
  record does not exist yet is not served, and `next` asks for `vecten deploy`.
- **NGINX serves each route** with WebSocket upgrade, the route's timeout and body size, and one
  of two access modes, `public` or `login`, either narrowed by an optional `allow` list of
  networks. Paths a service does not declare answer 404.
- **`login` is the MCP's login, through a gate.** NGINX asks the MCP server, for every request,
  whether the request's gate cookie belongs to an active person in one of the route's groups
  (`auth_request`). A person without a session is sent through the MCP's own sign-in and back.
  NGINX then passes `X-Vecten-User`, `X-Vecten-Name`, `X-Vecten-Groups` and `X-Vecten-Admin`
  to the service, and removes those headers from every request it did not authenticate. The
  same gate is what #5 (Airflow sign-in) can reuse.
- **Services are isolated from the rest of the stack.** They sit on a network of their own that
  only NGINX joins, publish no port, hold no cloud identity (the agent blocks the metadata
  address for that network and passes no cloud key) and receive only the secrets their
  extension declares, injected by the agent.
- **Health is reported, not gating.** Each service has a Compose health check. A routed service's
  health path is also served openly at `https://<label>.<domain>/_vecten/health`, forwarded as
  exactly that path and only for `GET` and `HEAD`, the pattern PR #58 uses for Airflow. `ship`
  polls it and reports each service's state; a failing service never rolls back a bundle.
- **`vecten up` starts services too**, at `https://<label>.localhost:8443`, unless the project
  sets `local = false` for one.

Nothing changes for a project without services: its Compose file, NGINX file, bundle, host.json
and Terraform plan are byte-identical.

## Capabilities

### Modified Capabilities

- `shared/extension-points`: the `vecten.services` group, what a service declaration holds, and
  the project's binding in `vecten.toml`.
- `platform/platform-stack`: services in the Compose profiles, NGINX routes per service hostname,
  the gate and its headers, isolation, the laptop.
- `shared/deploy`: service hostnames in DNS, outputs and certificates; services in the ship
  report; the VM agent's isolation step and service secrets.
- `mcp/mcp-auth`: the gate's sign-in, verification and sessions.
- `cli/cli-lifecycle`: `up`, `extension add` and `ship` with services.

## Impact

- Code: `vecten_shared.config` (`[services]`), `vecten_shared.extensions` (the group),
  `vecten.extensions` and #50's `vecten lock` (recording), `vecten.stack.compose`,
  `vecten.stack.nginx`, `vecten.stack.certs`, `vecten.stack.up`, `vecten.deploy.bundle`,
  `vecten.deploy.shipping`, `vecten.deploy.control` (the Terraform variable), `vecten.next`;
  `vecten_mcp` (gate endpoints, gate sessions in `mcp_state`); a small launcher in
  `vecten_shared` that services may use to resolve package resources.
- VM agent: `packages/vecten/src/vecten/deploy/vm/apply.py` and its byte-identical copy
  `terraform/scaleway/modules/host/agent/apply.py`: two new `host.json` keys
  (`service_secrets`, `service_network`) with legacy defaults, and an isolation step. Python 3.9,
  standard library only.
- Terraform: `terraform/aws` and `terraform/scaleway` gain the `service_hostnames` variable,
  records and output field; the scaffolded thin root declares and passes it.
- Docs: `docs/extensions.md` (the group and the binding), a new `docs/services.md`,
  `docs/GLOSSARY.md` ("Service", "Route", "Gate"), the runbook template, `docs/operations.md`.
- Depends on: #50's `vecten lock` and `extensions.lock.json`, #49's secret declarations and read
  API (`spec/extension-declarations`), and the in-repo extensions change
  (`project-extensions`). PR #58's allow-list helpers and health-path pattern are reused.
