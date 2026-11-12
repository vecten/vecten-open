# Extension services

An extension service is a long-running process that an [extension](GLOSSARY.md) contributes and
your Vecten project (the repository `vecten init` generated) runs on its deployed hosts and on
the laptop, in the local stack `vecten up` starts: a Teams or Slack bot that receives
webhooks and posts notifications, a portal of documentation and links, a dashboard. This page is
for the person who deploys and runs one, and for the author who writes one. How an extension
declares a service and how the project binds it in `vecten.toml` are in
[extensions](extensions.md#services); this page starts once a service is bound.

Terms used below:

- A **host** is a virtual machine (VM) of a deployed environment. Under the `standard`
  [profile](GLOSSARY.md) each [component](GLOSSARY.md) the project lists under `components` in
  `vecten.toml` has its own: the platform host runs Airflow, the MCP
  host runs the [MCP](GLOSSARY.md) server. Under `minimal` one VM runs both.
- **NGINX** is the web server in front of every host and of the laptop stack. It holds the HTTPS
  certificates and forwards each request to the container that serves it.
- **Deploy and ship.** `vecten deploy --env <env>` changes infrastructure, such as VMs and DNS
  records, by running Terraform in GitHub Actions, and then ships. A [ship](GLOSSARY.md) delivers
  the project's code and configuration to the existing hosts without Terraform; every push to
  the default branch runs one.
- A [route](GLOSSARY.md) is a path a service serves over HTTP, and the [gate](GLOSSARY.md) is
  the sign-in that protects `login` routes.

## What runs where

Each bound service runs as one container from the project image of its `host` component: the
Airflow image on the platform host, by default, or the MCP image on the MCP host when the
binding sets `host = "mcp"`. Under `minimal` both are on the one VM. Its Compose service, which
is also the name NGINX reaches it by, is `svc-<extension>-<service>`; `docker ps` shows the
container as `<Compose project>-svc-<extension>-<service>-1`. On a VM the container starts in
the same `docker compose up` as the rest of the stack, and under `vecten up` in a second one
once the stack is up. It restarts whenever it stops, and nothing waits for it: a service that crashes in a loop leaves Airflow,
the MCP server and NGINX serving, and only its own hostname answers 502. Removing a binding, or
the extension, stops and removes the container at the next ship.

A service on the MCP host holds no more access than one on the platform host (see
[isolation](#isolation-what-a-service-can-reach)), but it shares the VM's memory and CPU with
the MCP server, which every person's question goes through.

## Hostnames and `vecten deploy`

Each service with a bound route gets a hostname of its own: `<label>.<domain>`, or `<domain>`
itself when the binding sets `hostname = "@"`, where `<domain>` is the `domain` in that
environment's `deploy/envs/<env>.tfvars`. No extension route is ever served on
`airflow.<domain>` or `mcp.<domain>`. A browser treats each hostname as a separate origin, so
script on a service's page cannot read the [admin panel](GLOSSARY.md)'s pages or Airflow's UI
with the person's cookies.

A new hostname needs a DNS record, which only Terraform creates, so it takes one deploy:

1. **Bind and merge.** `extension add` writes the binding and names the new hostname. The pull
   request that adds it gets a comment from the generated `terraform-plan` workflow, which plans
   Terraform for every pull request that changes `vecten.toml`, showing the new DNS record.
   After the merge, the ship starts the service but serves none of its routes yet, and the
   `ship` workflow's log reports them by `<extension>.<service>.<route>`:

   ```text
   service acme-bot.bot: waiting for deploy
   routes waiting for `vecten deploy --env prod`: acme-bot.bot.admin, acme-bot.bot.webhook
   ```

   `vecten next`, which lists the project's next steps, offers `vecten deploy --env prod` while
   the environment's outputs on your machine (`.vecten/environments/prod.json`, written by the last deploy) lack the hostname.
2. **Deploy.** `vecten deploy --env prod` passes every bound label to Terraform, which creates one A record
   per label, pointing at the VM that runs the service, on AWS and on Scaleway alike, and lists
   it in the environment's outputs.
3. **Certificate.** The ship that the deploy runs adds the hostname to the host's list of names
   to certify. The host waits up to five minutes for the record to point at it, then gets a
   Let's Encrypt certificate; the daily renewal timer renews it with the others. While
   Let's Encrypt checks the name, NGINX on that host stops for a few seconds, so Airflow or the
   MCP on the same VM are briefly unreachable: add hostnames outside busy hours.

Renaming a label is a new hostname: it waits for the next deploy, which also removes the old
record. Moving a service to the other host with `host` works the same way: until the deploy
points the record at the new host, the service runs there and its routes wait.

`vecten deploy` refuses a project whose Terraform root (under `deploy/terraform/<cloud>`) does
not declare the `service_hostnames` variable, as roots generated before Vecten had services do
not, naming `vecten update`, which re-renders the root from the installed Vecten. `vecten.toml` is the only place labels are
set: a `service_hostnames` line in a tfvars file is refused. `@` is refused, on both clouds,
in an environment whose tfvars set `domain` equal to `dns_zone` (the DNS zone, which usually
also holds your organization's website). The reason is Scaleway: left alone, it would add a
second record at the zone's apex, the bare zone name itself, beside the website's instead of
refusing it; the rule holds on
AWS too, so that a binding means the same on both clouds.

## Access: `public`, `login` and `allow`

On a service's hostname NGINX answers each request this way:

| Path | Answer |
|---|---|
| `/_vecten/health` | The service's health check, to every address, for `GET` and `HEAD` (see [health](#health-and-the-ship-report)). |
| `/_vecten/gate/...` | The gate's sign-in, on a hostname with a `login` route. |
| Any other path under `/_vecten` | 404. |
| A path a bound route covers | The route's access mode, below. |
| Anything else | 404; nothing reaches the service. |

A request whose raw path holds an encoded slash or backslash (`%2F`, `%5C`), a backslash, a `;`
(or `%3B`), `//`, or a `.` or `..` segment, encoded or not, is answered 400 on every path a
route covers, before the service sees it; the health path and the 404 answers above are not
affected. NGINX picks the route from the cleaned-up path but forwards the path as sent, so such a
path could make the service read a different route than the one NGINX checked. An application
that needs these forms in its own paths cannot be routed.

- **`public`**: anyone. The service authenticates callers itself, as a webhook receiver must
  (see [webhooks](#webhooks-and-signature-checks)).
- **`login`**: only people signed in to the MCP who belong to one of the route's `groups`,
  through [the gate](#the-gate-signing-in-for-login-routes). It needs the `mcp` component.
- **`allow`** narrows either mode to the listed networks: on a deployed host NGINX answers 403 to
  every other address before any sign-in. `vecten up` ignores it, because the laptop's NGINX
  sees every request as coming from Docker's own address.

A forwarded request carries `X-Forwarded-For` and `X-Real-IP` set to the address NGINX saw
(whatever the client sent there is replaced), `X-Forwarded-Proto`, and the `Host` the browser
sent. Its body may be at most the route's `max_body_mb`, else NGINX answers 413. WebSocket
upgrades pass on every route; NGINX closes a connection on which the service sends nothing for
300 seconds, or for a day with `websocket = true`.

## The gate: signing in for `login` routes

The gate puts a service behind the MCP's own sign-in with no sign-in code in the service. One
identity provider, one list of people and groups (managed with `vecten user` and
`vecten group`, see [MCP identity](mcp-identity.md)), and no new redirect address to register at
the identity provider: it only ever returns to the MCP.

The sign-in flow:

1. A browser asks for a `login` route. For every such request NGINX first asks the MCP server
   whether the request's gate cookie belongs to an active person in one of the route's groups.
   Without a valid gate session, NGINX redirects the browser to
   `https://<hostname>/_vecten/gate/start` on the service's own hostname, carrying the path the
   person asked for.
2. `start`, answered by the MCP server through NGINX, sets the state cookie
   `vecten-gate-state` on the service's hostname, holding a random value for ten minutes. The
   cookie is limited to the path `/_vecten/gate`, which NGINX forwards only to the MCP, so the
   service never sees it. `start` then redirects to the MCP's
   `https://mcp.<domain>/gate/authorize` with only a hash of that value. The MCP signs the
   person in through its configured identity backend (OpenID Connect, password, or one an
   extension provides). A person already signed
   in to the MCP's browser pages passes straight through, so one sign-in covers every service.
3. The MCP redirects back to `https://<hostname>/_vecten/gate/callback` with a one-time code,
   valid for at most 60 seconds and only for that hostname and that hash. The callback, again
   answered by the MCP, accepts the code only together with the state cookie's value, so a
   callback address leaked through a log is useless without the browser that started the
   sign-in. It creates a gate session and sets the cookie `__Host-vecten-gate` (HTTPS only,
   hidden from scripts, sent to that hostname only), then returns the browser to the path it
   asked for.
4. Every later request is checked again, against the person's current state and groups: adding
   someone to a group, or disabling them, takes effect on their next request.

A signed-in person outside the route's groups gets a 403 "No access" page from NGINX, with a
link to sign out and use another account; a person with no gate session is redirected to sign
in, as above. The MCP starts a sign-in only for a
hostname that a `login` route binds in the `vecten.toml` it was shipped with, and returns only to
a path on that hostname, so the gate cannot send a browser to another site. Disabled people and
people not provisioned in the MCP are refused, and services that sign in to the MCP with client
credentials never get a gate session.

A gate session ends at the MCP's maximum session lifetime, when the person is disabled or
deleted, when the person sets a new password through a link, and at
`https://<hostname>/_vecten/gate/logout`. A WebSocket opened before that stays open until it
reconnects.

The gate depends on the MCP server: while it is down, `login` routes are down too, and `public`
routes keep working. Under `standard` the platform host's NGINX asks the MCP over HTTPS at
`mcp.<domain>`, with its certificate checked, which adds a round trip between the two VMs to
every request on a `login` route there. On the laptop, `login` routes use the laptop's MCP at
`https://mcp.localhost:8443`, its password sign-in, and the laptop's own people and groups.

### Identity headers: which to trust and why

On a `login` route NGINX passes the service who is asking:

| Header | Value |
|---|---|
| `X-Vecten-User` | The person's sign-in email. |
| `X-Vecten-Principal` | The person's stable id in the MCP. |
| `X-Vecten-Name` | The display name, percent-encoded (below). |
| `X-Vecten-Groups` | The person's current groups, comma-separated and sorted. |
| `X-Vecten-Admin` | `true` or `false`: the person's [admin flag](GLOSSARY.md). |

A header carries plain ASCII text only, so `X-Vecten-User`, `X-Vecten-Principal` and
`X-Vecten-Name` are percent-encoded as UTF-8: every character outside printable ASCII (space
to `~`), and `%` itself, becomes `%XX`, while a name in ASCII without `%` arrives unchanged. A
display name holding `ë` arrives with `%C3%AB` in its place. Decode the values before showing
or storing them, for example with Python's `urllib.parse.unquote`.

Trust these five headers only on a request that arrived on a `login` route. Three things make
them trustworthy there:

- **NGINX sets them on every path of a service's hostname.** On a `login` route it sets them
  from the MCP's answer; everywhere else, on `public` routes, the health path, the gate and the
  404 answers, it sets them empty, which forwards none. A header of these names sent by the
  client never reaches the service, in any letter case; spellings with `_` for `-` are dropped.
- **Only NGINX can reach a service.** A service publishes no port and sits on a Docker network
  that, of the stack's own containers, only NGINX joins, so no DAG, connector or other part of
  the stack can send it a request. Every bound service on a host shares that network, though,
  so another service on the same host can reach its port directly: trust the headers only as
  far as you trust every service bound on that host.
- **The service never sees the gate cookie.** NGINX removes `__Host-vecten-gate` from the
  `Cookie` header it forwards, so a service holds no credential it could replay to the gate.
  A service must not set a cookie of that name itself: NGINX answers 400 to a request that
  still carries one after the removal.

In practice: key what you store about a person by `X-Vecten-Principal`, since an email can
change; read the groups from each request rather than remembering them; and on a service with
both `public` and `login` routes, treat a request without `X-Vecten-User` as anonymous.

## Webhooks and signature checks

A bot receives its provider's webhooks on a `public` route, and `public` means NGINX lets
anyone through: the bot must check that each request comes from the provider, and refuse it
otherwise.

- **Microsoft Teams.** Set the Azure Bot Service messaging endpoint to the route, for example
  `https://bot.<domain>/api/messages`. Each request carries a Bot Framework token in its
  `Authorization` header, which the Bot Framework SDK checks once it is configured with the
  bot's app id.
- **Slack Events API.** Set the request URL to the route. Each request carries
  `X-Slack-Signature`, an HMAC-SHA256 of `v0:<X-Slack-Request-Timestamp>:<raw body>` under the
  app's signing secret; compare it, and refuse a timestamp more than five minutes old, which
  stops a recorded request being replayed.

Tokens and signing secrets are secrets the extension's manifest declares (see
[secrets and settings](extensions.md#secrets-and-settings)): set each with
`vecten secret set <NAME> --env <env>` for a deployed environment, and in `.env` for the
laptop. A provider that publishes the addresses it sends from can also go in the route's
`allow`. A provider that sends bodies over 1 MB needs a larger `max_body_mb`. A settings page
for the bot is a second route on the same hostname with `access = "login"`.

## Slack Socket Mode

A Slack bot in Socket Mode opens a WebSocket to Slack and receives events through it, so it
needs no route, no hostname and no deploy: bind the service without routes. It works on the
laptop without a tunnel. `ship` reports it as `not routed`; its health is checked only by Docker
(see [health](#health-and-the-ship-report)).

## Calling out, and data through the MCP

Outbound internet is open from the VMs on both clouds and from the services' network, so a bot
calls the Teams or Slack API directly, with tokens its manifest declares.

To send notifications built from warehouse data, either:

- an Airflow DAG posts the notification after a run, since DAG code already reads the warehouse;
  or
- the service asks the MCP server as a service principal, an MCP identity for a program rather
  than a person: `vecten service add <name> --env <env>` creates one and prints its OAuth client
  credentials once (see [MCP identity](mcp-identity.md)). Store them as secrets the extension
  declares, and call `https://mcp.<domain>` like any client. The principal's groups govern what
  it may read, and the [audit](mcp-audit.md) records every call.

A service never receives a warehouse credential: `LOADER_PASSWORD` and the MCP server's own
credentials cannot be declared as an extension's secrets (the full list is in
[secrets and settings](extensions.md#secrets-and-settings)). Requests from every service on a VM
reach the MCP from that VM's public address, and the MCP limits sign-in and token requests per
address, so the services on one VM share those limits.

## Isolation: what a service can reach

A public route is the code in a deployment most exposed to the internet, so a service holds as
little as possible:

- **Its own network.** Services join the Docker network `vecten-services`, which, of the
  stack's containers, only NGINX joins, and publish no port. Airflow, DAG code and the MCP
  server cannot resolve or reach a service, and a service reaches them only through their
  public hostnames, as any client would.
- **No cloud identity.** A service's environment holds no `AWS_*` or `SCW_*` key and no
  `AIRFLOW_CONN_*` connection. On a VM, the VM agent, the program that applies each ship, adds
  a firewall rule
  (`iptables`, in Docker's `DOCKER-USER` chain) that drops traffic from that network to the
  cloud's instance metadata address (169.254.169.254 on AWS, 169.254.42.42 on Scaleway), where a
  container would otherwise obtain the VM's own cloud credentials. The rule is added on every
  ship before any container starts, and at boot by the unit `vecten-services-guard.service`
  before Docker starts.
- **Only its declared secrets.** The agent reads the names the extension's manifest declares
  from the secrets the host may read, passes them to that service's container only, and masks
  them in every line `ship` prints. The extension's other secrets, the project's own, and the
  stack's are not passed.

When the firewall rule cannot be added, the host fails closed. A ship fails, naming its
`service isolation` step, and the version of the project already running on the host stays. At boot a failed guard unit
keeps Docker, and with it the host's whole stack, Airflow and the MCP included, down until an
operator fixes the cause. On Scaleway, whose VMs accept no SSH and no run command, that means a new VM. Outbound internet stays open, and a service shares the host's kernel
with the rest of the stack: install only extensions you trust.

## Limits and sizing

Every service runs with a memory limit and a CPU limit: the declaration's, 256 MB and half a
CPU unless it says otherwise, or the binding's `memory_mb` and `cpus`. A service that exceeds its
memory is killed and restarted; one that wants more CPU is slowed down.

Nothing compares the limits with the VM's size or refuses them; Vecten prints the sums for you
to compare. `vecten up` prints what the laptop's services reserve, and
`vecten ship --env <env> --check` what each host's do (`vm` stands for the one VM under
`minimal`; `--json` puts them under `service_limits`):

```text
services on platform reserve 768 MB and 1.5 CPUs
```

The platform host also runs Airflow, which uses most of the default VM size's memory. To grow a
host, set `platform_size` or `instance_type_overrides` in `deploy/envs/<env>.tfvars` and deploy;
the generated project's `docs/runbook.md` lists them under "Choose before the first deploy". On
Scaleway a change of server type replaces the VM, which loses the services' volumes.

## Volumes, and what replaces a VM

A service that declares `volume` gets a Docker volume named `svc-<extension>-<service>-data`
on its host, mounted at that path. When the image has no such path, the image build creates it
owned by the image's user, so the service can write there; a path the image already holds, such
as `/etc`, keeps its owner, so declare a path of the service's own. Docker lists it with the
Compose project's name in front, such as `vecten-acme-prod_svc-acme-bot-bot-data`.

The volume survives ships, restarts and reboots. It lives on the VM's disk and is not backed
up, so it is lost whenever the VM is replaced: a `vecten destroy` followed by a deploy, a change
of `profile`, on Scaleway a change of server type, or any `terraform-plan` comment that shows a
host's instance being replaced or destroyed. Moving a service to the other host with `host`
starts it with an empty volume there. Keep in a volume only what the service can rebuild, such
as a bot's conversation references, which it gets back when people message it again. Data that
must last belongs in a store outside the VM.

Removing a service's binding stops its container but keeps its volume, and the next ship, and
every ship after it, names the volume:

```text
volumes of services no longer bound on platform: vecten-acme-prod_svc-acme-old-data; they keep their data until removed on the VM
```

The line is in the `ship` workflow's log, and `--json` there lists them under
`orphaned_volumes`, by host. Vecten never removes one. On AWS you can remove it with
`docker volume rm <name>` through Systems Manager Run Command, the AWS service `vecten ship`
itself uses to run commands on a VM; a Scaleway VM accepts no SSH and no
run command, so there it stays until the VM is replaced. On Scaleway the first ship after
upgrading Vecten does not list volumes yet, because the VM still runs the previous release's
copy of the agent.

## On the laptop

`vecten up` starts each bound service whose binding does not set `local = false`, after the rest
of the stack is up, and does not wait for it to become healthy. It prints each service's state
as Docker sees it (its health check's `starting`, `healthy` or `unhealthy`, its container state
while it has no verdict yet, `not running`, `not started:` with Docker's reason, or
`unknown: docker compose ps failed`), the address of each bound
route, and what the services reserve:

```text
service acme-bot.bot: starting
route acme-bot.bot.admin: https://bot.localhost:8443/settings
route acme-bot.bot.webhook: https://bot.localhost:8443/api/messages
services reserve 256 MB and 0.5 CPUs
```

`--json` gives them under `services`, `routes` and `limits`. A service's address is
`https://<label>.localhost:8443`, or `https://localhost:8443` for `@`, with a self-signed
certificate that the browser warns about, as it does for `airflow.localhost`. `allow` does not
apply on the laptop; `login` routes use the laptop's MCP. A service reads its declared secrets
from `.env`; a name `.env` lacks is empty.

Vecten provides no tunnel, and the laptop's NGINX listens on `127.0.0.1` only, so a provider
cannot send webhooks to the laptop. Set `local = false` for such a bot and try it in a deployed
environment, or run a tunnel of your own to `https://127.0.0.1:8443` that connects with
`<label>.localhost` as its TLS server name (SNI), since the laptop's NGINX refuses a TLS
handshake for any other name, and sends `<label>.localhost` as the `Host` header.

## Health and the ship report

Docker runs each service's declared health check (see
[declaring a service](extensions.md#services)). For a routed service that declares a health
path, NGINX also serves `https://<hostname>/_vecten/health` to every address, outside `allow`
and the gate, so that `ship` can reach it from a GitHub Actions runner. It admits `GET` and
`HEAD` only, answering 403 to anything else, and forwards exactly the declared path. Anyone can
read the answer, so put nothing private in it.

After it has checked Airflow and the MCP, `ship` polls each served service's health address,
one after another, for up to 60 seconds each, and reports one state per bound service, under
`services` with `--json`. These lines are printed by the ship that runs inside GitHub Actions: in the log of the `ship` workflow, and of the `terraform-apply` workflow that `vecten deploy` dispatches. `vecten ship --env <env>` or `vecten deploy --env <env>` run from a laptop dispatch the workflow and print only its run and address.



| State | Meaning |
|---|---|
| `healthy` | The health address answered with a status from 200 to 399. |
| `unhealthy` | It did not, within 60 seconds. |
| `waiting for deploy` | The hostname has no DNS record yet; its routes are listed under `waiting_routes` (see [hostnames](#hostnames-and-vecten-deploy)). |
| `not routed` | The binding sets no hostname; only Docker checks its health. |
| `served` | The hostname is served and the service declares a health command, which only Docker runs. |

A service's state never fails a ship and never puts back the version the previous ship delivered: a broken bot does
not undo a dbt fix. Each unhealthy service only makes the ship up to a minute longer.
`vecten issues` does not read services; [operations](operations.md#services-in-the-ship-output)
says where their state shows.

## Streamlit and other web frameworks

A Streamlit app needs `websocket = true` on its route, `/_stcore/health` as its health path,
and the options `--server.address=0.0.0.0`, `--server.port=<port>` and
`--browser.gatherUsageStats=false`, the last turning off the usage statistics Streamlit sends by
default. The app's file is package data, and the installed package sits in a different place in
each image, so start Streamlit from a module of the package that finds the file next to itself,
for example `["python", "-m", "acme_dash.serve"]`. Any other framework works the same way: listen
on `0.0.0.0` and the declared port, and serve the declared paths as they are, from `/`, since
NGINX forwards each request's path unchanged and each service has its hostname to itself.

## The override file

`orchestration/docker-compose.override.yml` is still merged over the generated Compose file on
the laptop and on every VM. A service added there without a Compose profile starts on every
host, cannot bring files of its own, and gets none of the above: no hostname, route, gate,
isolation or report. Use `vecten.services` instead.

Back to the [docs index](README.md).
