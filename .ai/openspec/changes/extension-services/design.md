## Context

This change answers #38: an extension runs a long-running service and exposes HTTP routes. #39
(analytics apps) builds on it and is the separate change `extension-analytics-apps`, which lands
after this one.

**The product model it serves.** A generic template, such as a Teams or Slack bot, a portal of
documentation and links, or a Streamlit dashboard, is copied once into a client's Vecten project
under `extensions/<name>/` and owned by the client from then on (change `project-extensions`).
`vecten extension add ./extensions/<name>` registers it, the bundle carries `extensions/`, and the
images install it from that path on the laptop and on every VM. An extension from a package
index is installed the same way, by name. Everything below has to work for both, so nothing in
this design reads an extension's source tree: it reads the installed package, or the record
`vecten lock` wrote.

**What the code does today**, which every decision starts from:

- **Compose.** `stack/compose.py` generates the whole stack from `vecten.toml` on every `up` and
  `ship`. Services carry the profile of their component (`platform`, `mcp`) or `local`. The VM
  agent runs `docker compose ... --profile <component> up -d --build --remove-orphans` for each
  component its host serves (`compose_up` in `deploy/vm/apply.py`), and merges
  `orchestration/docker-compose.override.yml` over the generated file. An override service with
  no profile therefore starts on every host. The images are built on the target from a thin
  project layer (`stack/images.py`) over the published base images.
- **NGINX.** `stack/nginx.py` writes one server per built-in hostname and a default server that
  rejects the TLS handshake and answers 444. Upstreams are resolved per request through Docker's
  DNS (`RESOLVER`), so NGINX starts even while an upstream container is missing. #58 adds a
  shared `allow … deny all` helper (`_allow_rules`, validated by `allow_networks`) and an open
  exact-match health location that forwards only that path and only reads.
- **DNS and certificates.** `terraform/{aws,scaleway}/locals.tf` map `platform` to
  `airflow.<domain>` and `mcp` to `mcp.<domain>`; `dns.tf` creates one A record per component
  pointing at the host that serves it; `outputs.hosts[h].hostnames` pairs one hostname with each
  component, and `vm_hostnames` refuses any other shape. `bundle.py` copies that list into
  `host.json`'s `hostnames`, and the agent runs certbot (`--standalone`) for every listed name
  that has no certificate yet, after waiting for its DNS record to point at the host. The renewal
  timer in user data renews every certificate under `/etc/letsencrypt`.
- **Cloud identity.** On AWS every container reaches the instance metadata service: the host
  module sets `http_put_response_hop_limit = 2` so that Airflow and the MCP server can use the
  instance role. On Scaleway, containers get the VM's key only when the generated Compose file
  references `SCW_*`. A container on the MCP host can therefore read the `mcp` scope, which holds
  `MCP_PROVISIONER_PASSWORD` and the signing key; under `minimal` one VM reads both scopes.
- **Secrets to containers.** The agent reads the names in `host.json`'s `connector_secrets` from
  the platform scope and hands them to Compose; #49 (`spec/extension-declarations`) extends that
  list to the platform names every extension's manifest declares, with no agent change, and has
  the MCP server read `MCP_*` names through its provider.
- **CI ships without the project's extensions.** The rendered `ship.yml` installs `vecten` only.
  #50 therefore records what extensions declare in a committed `extensions.lock.json` that `ship`
  reads without importing anything. This change records services the same way.
- **The MCP server** is its own OAuth 2.1 authorization server, signs people in through an
  identity backend (`oidc` or `password`), keeps principals, groups and the admin flag in
  `mcp_state`, and serves browser pages (login, consent, `/admin`) with `SameSite=Strict`
  cookies on `mcp.<domain>`.

## Goals / Non-Goals

**Goals:**

- An extension, installed from an index or kept in the project, can run a long-running process
  on a deployed environment and on the laptop.
- The process can be reached on its own hostname, with DNS and a certificate handled like the
  built-in hostnames, on AWS and on Scaleway.
- Each route is public, behind the MCP's login with group checks, or either of those narrowed to
  listed networks.
- A public-facing service is contained: a compromise of it does not hand over the host's cloud
  identity, other secrets or other containers.
- No change for projects without services.

**Non-Goals:**

- A service built from its own Dockerfile or image. Services run from the project images.
- A durable data store for services beyond a host-local volume.
- Analytics apps, their listing and their data access (#39, the next change).
- Airflow's sign-in through the MCP (#5). This change designs the gate so #5 can reuse it.
- Cloud or warehouse resources a service needs (#40).
- Scaling a service beyond one container, or running one on a host of its own.

## Decisions

All decisions were accepted by the maintainer on 2026-11-12, each as recommended. They are
ordered with the most consequential and least reversible
first: Decisions 1 to 4 are the contract extension authors and templates code against, and the
DNS shape every deployed environment keeps.

### Decision 1: One `vecten.services` group; the extension declares the process, the project binds the deployment — *Accepted*

**Context.** Two things need saying about a service. Some are facts about the code: the command,
the port it listens on, its health path, the paths it serves, where it keeps files. Others are
choices about one deployment: which hostname, who may reach it, how much memory, whether it runs
on the laptop. The issue asks for "two entry point groups, or one". Whatever is declared has to
reach `ship` in CI, which cannot import the extension (#50, Decision 5).

**Options.**

1. *Two groups, `vecten.services` and `vecten.routes`*, a route naming its service. Routes could
   in principle come from another extension than the service, which no use case needs, and every
   route then needs a cross-reference check.
2. *One group, `vecten.services`, with routes nested in each service.* A route is meaningless
   without the process behind it, so it lives inside it. One object to validate, one name space.
3. *Keys in #49's `vecten.manifest`* (`services = {...}`). One fewer group, but the manifest is
   "what the extension needs", not a layer it contributes, and a package whose only entry point
   is the manifest is refused as contributing nothing (#49). A service is a layer.

For where the deployment choices live:

- (a) *All in the extension's declaration.* The extension author would choose the client's
  hostname, its groups and whether a route is public. A package upgrade could make a route
  public with no change in the project's diff.
- (b) *Code facts in the declaration, deployment choices in `vecten.toml`*, written by
  `extension add` from the declaration's suggestions, as `connector add` copies a connector's
  `secrets` and `dependencies` into the project's connector declaration today.
- (c) *All in `vecten.toml`*, the extension contributing nothing but a command. The command and
  port would then be duplicated by hand in every project.

**Accepted: option 2 with (b).** The declaration (a dictionary, or a zero-argument factory
returning one, as for `vecten.connectors`) holds, per service name:

```python
{
    "command": ["python", "-m", "acme_bot.server"],  # exec form, run in the project image
    "port": 8000,  # required when the service has routes
    "health": {"path": "/healthz"},  # or {"command": [...]}
    "volume": "/var/lib/acme-bot",  # optional, see Decision 10
    "components": ["platform"],  # images it can run from, see Decision 5
    "memory_mb": 256,
    "cpus": 0.5,  # defaults, see Decision 9
    "routes": {
        "webhook": {"path": "/api/messages", "access": "public", "max_body_mb": 1},
        "admin": {"path": "/", "access": "login", "groups": ["default"], "websocket": True},
    },
}
```

`vecten lock` records it in `extensions.lock.json` beside the manifest. The project's binding is:

```toml
[services.acme-bot.bot]           # [services.<extension>.<service>]
hostname = "bot"                  # bot.<domain>; "@" for the apex of <domain>
host = "platform"                 # one of the declaration's components the project enables
local = true                      # start it under `vecten up`
memory_mb = 256

[services.acme-bot.bot.routes.webhook]
access = "public"

[services.acme-bot.bot.routes.admin]
access = "login"
groups = ["default"]
allow = ["203.0.113.0/24"]        # optional, narrows either access mode
```

Consequences:

- `ship` and `up` render from `vecten.toml` and `extensions.lock.json` alone. A service or route
  that the record declares but `vecten.toml` does not list does not run; `next` reports it as
  unwired. A table in `vecten.toml` naming a service or route the record does not declare is
  refused when the configuration loads.
- A package upgrade that adds a public route changes nothing until a person adds it to
  `vecten.toml`, and that addition shows in the project's diff.
- An in-repo extension whose service declaration changes in code needs `vecten lock` before the
  change ships, the same staleness #49's manifest has. `up` re-reads the declaration from the
  editable install and refuses a record that differs ("run `vecten lock`"), so the developer sees
  it on the laptop first. A CI check that would catch it without a laptop is left to the
  in-repo change, which owns how in-repo records go stale.
- Names: a service name and a route name match `^[a-z][a-z0-9-]{0,30}$`. The Compose service is
  `svc-<extension>-<service>`, truncated with a hash suffix past 63 characters.

### Decision 2: Every routed service gets its own hostname, never a path on `airflow.` or `mcp.` — *Accepted*

**Context.** This decides the DNS shape of every deployed environment and the browser's security
boundary between extension code and the product's own pages. Origins are the browser's unit of
isolation: script on a page can read any response from its own origin with the person's cookies.

**Options.**

1. *A path on an existing hostname* (`mcp.<domain>/portal/`). No DNS or certificate work, and a
   new route ships without Terraform. But a page the extension serves would share the origin of
   the admin panel and the OAuth consent page: one cross-site scripting flaw in a portal would
   let script read `/admin` pages, CSRF tokens included, with an admin's cookies. On
   `airflow.<domain>` it would share the origin of Airflow's UI and its token. Every extension
   path would also have to be kept clear of the MCP's and Airflow's own paths forever.
2. *One shared hostname for all extensions* (`apps.<domain>/<service>/`). One DNS record and one
   certificate, created once. Extensions still share an origin with each other, and apps that
   assume they own `/` (most Streamlit apps, many bot frameworks' callback URLs) need a base-path
   setting each.
3. *A hostname per routed service*, `<label>.<domain>`, with the apex of `<domain>` available as
   `@`. Each service is its own origin. A service may serve several paths on its hostname, each
   with its own access mode (a bot's public webhook and its signed-in settings page).
4. *A wildcard record* `*.<domain>` pointing at the services' host, with certificates still per
   name (HTTP-01 works under a wildcard record). New hostnames then need no Terraform, but every
   unknown name under `<domain>` resolves to the VM, a wildcard can point at one host only, and a
   project whose `<domain>` is its company's main domain would capture every undefined subdomain.

**Accepted: option 3.** Labels are lowercase DNS labels; `airflow`, `mcp`, `www` and labels
starting with `_` are reserved; a label maps to exactly one service in the project. `@` is refused
when `<domain>` equals the DNS zone, because on Scaleway a second A record at a zone apex is added
beside the client's own website rather than refused.

The cost is that a new hostname needs `vecten deploy` (Terraform) once before `ship` can serve
it. Hostnames change rarely: the template is added once. Decision 3 makes the wait explicit.

### Decision 3: Hostnames flow from `vecten.toml` to Terraform at deploy, and to certbot through `host.json` — *Accepted*

**Context.** Terraform reads tfvars, not `vecten.toml`. Both the plan workflow and the apply
workflow run Terraform through `vecten deploy`, which already passes extra variables
(`terraform.py`, `extra_vars`). The VM agent issues certificates for exactly `host.json`'s
`hostnames`, and uses that list for nothing else.

**Options.**

1. *The person lists labels in each environment's tfvars* (`service_hostnames = {...}`), and
   `ship` checks they agree with `vecten.toml`. Two places to edit, per environment, for a fact
   that does not differ between environments.
2. *`vecten deploy` derives them from `vecten.toml`* and passes
   `-var service_hostnames=<json map of label to component>`. The thin root declares the variable
   (default `{}`) and passes it to the module. One source, and the pull-request plan shows the
   new record.

**Accepted: option 2.** In each cloud module, `service_hostnames` (a map of label to
component, validated: label shape, reserved labels, component among `components`) adds one A
record per label pointing at `host_of[component]`, beside the built-in records, with the same
TTL, and the outputs gain `hosts[h].service_hostnames`, the list of names served by host `h`. The
built-in `hostnames` list keeps its pairing with `components`, so `vm_hostnames` is unchanged.
Both modules keep identical outputs.

At ship, `bundle.py` appends the host's `service_hostnames` from the outputs to `host.json`'s
`hostnames`. The agent then waits for each record and runs certbot for it exactly as for the
built-in names, and the renewal timer covers it: no agent change for certificates. Only names
present in the outputs are appended, because the agent waits for DNS and would otherwise fail
the ship; a route whose hostname is not in the outputs yet is not rendered, `ship` reports it as
"waiting for `vecten deploy --env <env>`", and `next` offers that command.

A project whose thin root predates the variable gets a refusal from `vecten deploy` naming
`vecten update` when `vecten.toml` binds any hostname; without hostnames nothing is passed.

Adding a hostname to a running host stops NGINX for the seconds certbot's standalone challenge
takes, as on the first ship. The runbook says so.

### Decision 4: `login` is the MCP's login through a gate: `auth_request`, a host-only cookie, live groups — *Accepted*

**Context.** "Behind the MCP's login" must work for any service without code in it, must use the
person's current groups (`mcp/mcp-auth`, "Session resolves principal and groups"), must not let a
client forge who it is, and must work when the MCP runs on another VM (`standard`). NGINX open
source has `auth_request` but no JWT validation. #5 wants Airflow behind the same login.

**Options for verifying the person.**

1. *`auth_request` against the MCP.* For every request on a `login` route, NGINX makes an internal
   subrequest to the MCP's verify endpoint with the request's cookie and the route's name. The
   MCP looks up the gate session, checks the principal is active and in one of the route's
   groups, and answers 200 with identity headers, 401 or 403. Group changes and disabled people
   take effect on the next request. Cost: one round trip to the MCP per request; under
   `standard` that is HTTPS to `mcp.<domain>` from the platform VM.
2. *A signed short-lived token in the cookie, verified next to the service* by a small verifier
   process. No per-request round trip, but group membership is frozen for the token's lifetime,
   revocation waits for expiry, and a verifier process is one more moving part per host.
3. *Each service does OpenID Connect against the MCP itself.* Standard for apps that support it
   (Streamlit's `st.login`, Grafana, Airflow's FAB), but every template must implement it, a
   service that forgets is open, and the MCP must become a full OpenID provider with ID tokens
   and a groups claim.

**Accepted: option 1.** The flow:

1. A request without a valid gate cookie gets 401 from the subrequest; NGINX answers with a
   redirect to `https://<label>.<domain>/_vecten/gate/start?rd=<path>` on the same host.
2. `/_vecten/gate/*` on every host with a `login` route is proxied to the MCP, with the service's
   hostname passed by NGINX. `start` sets a short-lived, HttpOnly state cookie on the service's
   host holding a random verifier, and redirects to `https://mcp.<domain>/gate/authorize` with
   only the verifier's hash, PKCE's pattern. `authorize` signs the person in with the configured
   backend; a person already signed in to the MCP's browser pages passes straight through, so
   one sign-in covers every service.
3. The MCP redirects back to `https://<label>.<domain>/_vecten/gate/callback` with a one-time
   code bound to that hostname and to the verifier's hash. The callback, again proxied to the
   MCP, redeems the code only with the verifier from the state cookie, so a callback URL leaked
   through a log or a referrer is useless without the browser that started the flow. It then
   creates a gate session in `mcp_state` and sets the cookie
   `__Host-vecten-gate` (Secure, HttpOnly, SameSite=Lax, host-only) holding an opaque session id,
   then redirects to `rd`, which must be a path.
4. Every later request is verified by the subrequest as above.

The MCP accepts `authorize` only for hostnames that a `login` route binds in its own copy of
`vecten.toml` (the bundle's), so it is not an open redirector. Gate sessions end at the MCP's
maximum session lifetime, when the person is disabled or deleted, on `/_vecten/gate/logout`, and
whenever "Setting a password ends the person's other sessions" applies. A route's `groups` must
name at least one group; there is no "anyone signed in" shorthand, because an empty list means
nobody for MCP tools already.

`login` requires the `mcp` component; a platform-only project may bind `public` routes only.

**How group membership and identity reach the service.** On a `login` route NGINX copies these
headers from the verify answer (`auth_request_set`) into the request it forwards:

| Header | Value |
|---|---|
| `X-Vecten-User` | the principal's login email |
| `X-Vecten-Principal` | the principal's stable id |
| `X-Vecten-Name` | the display name |
| `X-Vecten-Groups` | the person's current group names, comma-separated, sorted |
| `X-Vecten-Admin` | `true` or `false`, the admin flag |

**Which headers a service may trust, and how spoofing is prevented.** A service may trust exactly
these five headers, and only on a request that arrived on a `login` route. Three layers make that
hold:

- NGINX sets every one of the five on every location it renders, for every route: from the
  verify answer on `login` routes, and to the empty string on `public` routes and on the gate,
  health and fallback locations. NGINX does not forward a client header it redefines, and an
  empty value drops it, so a client-sent `X-Vecten-User` never reaches a service under any
  spelling. NGINX's default `ignore_invalid_headers` drops underscore variants.
- A service is reachable only through NGINX: it publishes no port and sits on a network only NGINX
  joins (Decision 6). No DAG, connector or other service can send it a forged header.
- The gate cookie is removed from the `Cookie` header forwarded to the service, so the service
  never holds a credential it could replay to the gate.

A signed identity assertion (a JWT the service verifies) is not part of this change. If one is
added later it MUST NOT be signed with the MCP's signing key: Snowflake's External OAuth
integration trusts that key, so a token signed with it and handed to extension code could be
replayed to Snowflake as the person.

**How #5 reuses it.** Airflow can sit behind the same gate: the platform host's NGINX protects
`airflow.<domain>` with the same subrequest (groups: the admin flag or a named group), keeps PR
#58's open health location outside it, and forwards the same headers. #5 then needs only an
Airflow auth manager that trusts `X-Vecten-*` from NGINX and maps `X-Vecten-Admin` to Airflow's
admin role, and a decision on how API clients such as `vecten issues --env` authenticate (they
cannot follow a browser redirect). One login, one admin list (`[mcp] admins`), and no second
OpenID client registration at the identity provider. That decision stays in #5's change.

### Decision 5: A service runs from the project image of the component it is placed on — *Accepted*

**Context.** The issue asks for "the image that already has the extension installed". Two project
images exist: `<project>-airflow` (Airflow base, user `airflow`) and `<project>-mcp`
(`python:3.14-slim` base, user `app`). #50 installs an extension's `platform` extra in the first
and its `mcp` extra in the second. In-repo extensions are built into both from `extensions/`.

**Options.**

1. *The image of the host component*: a service placed on `platform` runs from the Airflow
   project image, on `mcp` from the MCP project image. Nothing new is built. The declaration's
   `components` lists the images whose extra holds the service's dependencies; the project picks
   one in `host`.
2. *A third project image for services*, `<project>-services`, built from the MCP base image with
   a new `services` extra and a fourth lock. Light, and free of Airflow's constraints, but #50's
   lock machinery, the bundle and both clouds' image builds all grow by one.
3. *An extension-provided Dockerfile or image.* Any stack (Node, Go). It needs a build context
   in the bundle, a second supply chain to review and lock, and per-architecture builds.

**Accepted: option 1.** The cost is that a service's dependencies resolve against the
Airflow base image's installed set when it runs on `platform`. Streamlit and its dependencies
(`tornado`, `pyarrow`, `protobuf`) may conflict with Airflow's pins; the first task is a spike
that resolves the Streamlit template's `platform` extra against the Airflow image. If it
conflicts, option 2 is the fallback, and it is additive: a new `components` value.

**Spike result (task 1.1, 2026-11-12): no conflict; option 1 stands.** An in-repo extension
whose `platform` and `mcp` extras list `streamlit`, `tornado`, `pyarrow` and `protobuf` was added
with `vecten extension add` and locked by `vecten lock` against the local `vecten-airflow:0.1.0`
(Airflow 3.3.2, Python 3.14) and `vecten-mcp:0.1.0` base images. Both locks hold Streamlit
1.67.0, the newest release, so Airflow's installed set does not hold Streamlit back. It holds
Streamlit's dependencies at the versions already in the image:

| Package    | Airflow lock | MCP lock and free resolution |
|------------|--------------|------------------------------|
| protobuf   | 6.33.6       | 7.36.2                       |
| pyarrow    | 25.0.1       | 26.0.1                       |
| tornado    | 6.5.8        | 6.6.1                        |
| pandas     | 3.0.5        | 3.1.1                        |
| numpy      | 2.5.3        | 2.5.4                        |

Both project images built from the generated Dockerfiles, including the `--no-deps` install and
`pip check`. In each, `streamlit run` as the image's own user (`airflow`, `app`) answered
`/_stcore/health` with `200 ok` and served its page, and `airflow version` still reports 3.3.2.
The extension adds about 170 MB to the Airflow project image (3.62 to 3.79 GB) and about 600 MB
to the MCP project image (0.49 to 1.09 GB), since the MCP base has no numpy, pandas or pyarrow.
The remaining risk is a future Streamlit release that requires protobuf 7 or a newer pyarrow
than Airflow ships: `vecten lock` then picks an older Streamlit or fails to resolve, and option 2
becomes the answer for that template.

### Decision 6: A service holds no cloud identity, sits on its own network, and gets only its declared secrets — *Accepted*

**Context.** A webhook receiver is the code in a deployment most exposed to the internet. On the
platform host today, any container can take the instance role through the metadata service and
read every secret in the `platform` scope: connector credentials and `LOADER_PASSWORD`, which can
write the warehouse. On the MCP host, or on the one VM under `minimal`, that includes
`MCP_PROVISIONER_PASSWORD` and the signing key, which together control every person's warehouse
access. Containers on the default Compose network can also reach each other's ports.

**Options.**

1. *Same as Airflow and the MCP*: the host's network and identity. Simplest; a compromised public
   service reads the host's whole secret scope.
2. *No cloud identity, isolated network, injected secrets.* Services join a Compose network
   `vecten-services` (bridge name pinned) that only NGINX also joins; they publish no port. The VM
   agent, on each ship and at boot, inserts `DOCKER-USER` rules dropping traffic from that bridge
   to the metadata addresses (169.254.169.254 on AWS, 169.254.42.42 on Scaleway). The generated
   Compose file passes no `SCW_*` or `AWS_*` key to a service. The agent reads the secret names in
   a new `host.json` key, `service_secrets`, from the scopes its host may read and hands them to
   Compose, which passes each service only the names its extension declares. The read API (#49)
   reads them from the environment when `VECTEN_COMPONENT=service`. Outbound internet stays
   open, which a bot needs.
3. *A cloud identity per service*: an IAM role or Scaleway application per service, with its own
   scope. The cleanest boundary, but it is Terraform, IAM and boundary work per service, which is
   #40's ground.

**Accepted: option 2.** It changes the VM agent: two `HOST_KEYS` (`service_secrets`, a
list, default `[]`; `service_network`, the bridge name, default `""` meaning no step), the
iptables step, and the byte-identical Scaleway copy, all Python 3.9 standard library. Every
bundle carries its own copy of the agent (`.vecten/ship/apply.py`), so a bundle with services is
always shipped by an agent that knows the keys; `load_host` already ignores keys it does not
know and fills legacy defaults, so rolling back to an older bundle keeps working. A rollback is
run by the agent of the release that failed, so it knows the keys too, unless that release was
built by a Vecten older than this change: downgrading Vecten on an environment whose previous
release runs services is unsupported. After a reboot dockerd restarts running services as it
starts, so a ship that runs services also installs `vecten-services-guard.service`, ordered
before `docker.service` and required by it, which runs the shipping agent's own copy
(`/opt/vecten/guard/apply.py guard`, since the current release may predate the command) to
create `DOCKER-USER` and add the rule with no secret read; dockerd keeps a chain it finds
(proven in the Docker tier on engines 25.0 and 28.5). A guard that fails keeps Docker, and
with it the whole stack, down until an operator fixes it: fail-closed, proven under systemd. The boot step still adds the rule first, before any
secret read; at boot and on rollback a failure stops the services rather than leave them
running without the rule. On ship it runs before the swap and fails the ship, because a
service without the rule would hold the host's identity.

Consequences: a service reads secrets only as names its extension declares (the same names #49
delivers); it cannot call the provider for anything else; it reaches the MCP through
`https://mcp.<domain>` like any client (Decision 13). DAG code and the MCP server keep their
identity.

### Decision 7: Placement: the platform host by default, the MCP host only by choice — *Accepted*

**Context.** `minimal` puts everything on one VM. Under `standard`, the platform VM runs DAG code
(already the least trusted host) and the MCP VM holds the provisioner and signing key. A project
may have only one component.

**Options.** (1) Any component host, by the declaration. (2) The platform host only. (3) Platform
by default, `mcp` when the project sets `host = "mcp"` and the declaration's `components`
allows it.

**Accepted: option 3.** With Decision 6 a service on the MCP host holds no identity, so the
remaining exposure is resource contention with the MCP server and a shared kernel. MCP-only
projects, which have no platform host, need option 3 to run a portal at all. `vecten lock` and
`ship --check` print a note when a service is placed on `mcp`. Under `minimal` `host` still
decides the image and profile, and the one VM runs both.

### Decision 8: Two access modes, `public` and `login`, each optionally narrowed by `allow` — *Accepted*

**Context.** The issue names public (for example a webhook receiver with its own signature check)
and behind the MCP's login; the brief adds an IP allow-list. An allow-list is useful with both: a
webhook from a provider with published address ranges, a portal for office networks only.

**Accepted.** `access` is `public` or `login`, and `allow` is an optional list of networks,
validated and normalised by #58's `allow_networks` and rendered by its `_allow_rules`. A
`public` route is served to anyone and the service authenticates the caller itself, which the
docs make explicit for webhooks (Teams: Bot Framework's JWT; Slack: the `X-Slack-Signature`
HMAC). `extension add` writes the declaration's suggested `access` into `vecten.toml`, so a public
route is a line in the project's diff, never a default the project did not see. As with #58's
Airflow list, `up` ignores `allow`: the laptop's NGINX sees Docker's gateway address, so a real
list would lock the developer out of their own stack.

### Decision 9: Required resource limits, with defaults — *Accepted*

**Context.** The platform VM's default tier is 2 vCPU and 4 GB (`platform/platform-stack`,
"Resource target"), most of which Airflow uses. A Streamlit app can take hundreds of megabytes.
An unbounded service would starve the scheduler.

**Accepted.** Every service runs with a memory limit and a CPU limit in Compose
(`mem_limit`, `cpus`): the declaration's defaults (256 MB and 0.5 when absent), overridden in
`vecten.toml`. `ship --check` and `up` print the sum of service limits per host against the
host's memory, and the docs name `platform_size` and `instance_type_overrides` as the way to
grow. Refusing over a budget is not proposed: the headroom per VM class has not been measured.

### Decision 10: A persistent volume is host-local and survives ships, not VM replacement — *Accepted*

**Context.** The issue asks for a persistent volume. The platform stack's rule is that "a VM holds
no state and can be rebuilt". A bot that sends proactive notifications keeps conversation
references, which it can re-acquire when people message it again.

**Options.** (1) A named Docker volume per service on the host. (2) A database per service on the
state instance, created by the setup script. (3) A prefix of the environment's bucket, through a
new provider operation.

**Accepted: option 1.** `volume` names the mount path; Compose mounts the named volume
`svc-<extension>-<service>-data`, owned by the image's runtime user. It survives ships, restarts
and reboots, and is lost when the VM is replaced, which the docs say plainly with the cases that
replace a VM. Removing a service leaves its volume until a person removes it; `ship` reports
orphaned service volumes. Options 2 and 3 are durable but change the setup script or the
provider interface's runtime operations (`shared/deploy`, "Runtime code uses only get_secret,
connection and log_target"), and belong with #40.

### Decision 11: Health is checked by Compose and reported by `ship`; it never rolls back a bundle — *Accepted*

**Context.** `ship` rolls back the whole bundle when a component's URL fails its poll. A broken
bot should not undo a dbt fix, and a service must never keep NGINX from starting.

**Options.** (1) Gating: a failing service fails the ship. (2) Reported: the ship succeeds and
lists each service's state. (3) Gating per service, by a `required` flag.

**Accepted: option 2.** Each service gets a Compose `healthcheck` from its declaration
(`curl --fail http://localhost:<port><path>`, which both project images can run, or the
declared command). NGINX does not depend on any service; upstreams are resolved per request, so
a missing service answers 502 on its own hostname only. A routed service's health path is also
served at `https://<host>/_vecten/health` through an exact-match location open to every address,
admitting `GET` and `HEAD` only and forwarding exactly the declared health path, #58's pattern
and for the same reason (NGINX matches the normalised URI but forwards the one sent). After the
component polls, `ship` polls each routed service's health URL and reports `healthy`,
`unhealthy` or `waiting for deploy`; a service without routes is reported by its container state
only through the agent's log excerpt on failure. Option 3 can be added later without changing
the declaration.

### Decision 12: Compose profile, the laptop, WebSockets and proxy settings — *Accepted*

- **Profile.** A service carries the profile of its `host` component, so the agent's existing
  `--profile <component>` starts it on the right host and `--remove-orphans` stops a removed one.
  No new profile, no agent change for starting.
- **The laptop.** `up` starts every service whose binding does not set `local = false`, at
  `https://<label>.localhost:8443` with a self-signed certificate from `stack/certs.py`, and prints
  each route's URL. `login` routes use the laptop's MCP and its password login. Secrets come from
  `.env` through the same declared names. A bot that needs a public webhook URL on the laptop
  needs a tunnel the developer runs; the docs say so and suggest `local = false` otherwise.
- **WebSockets.** Every route forwards `Upgrade` and `Connection` through the existing
  `$connection_upgrade` map, so a WebSocket needs no flag. `websocket = true` raises the read
  timeout to one day and turns proxy buffering off, which Streamlit needs. Without it the read
  timeout is 300 seconds. An open WebSocket survives a gate session's end until it reconnects; the
  docs say so.
- **Body size.** `max_body_mb` per route, 1 by default (NGINX's own default), at most 100.
- **Forwarded address.** NGINX replaces `X-Forwarded-For` and `X-Real-IP` with the address it
  saw, as for the built-in hosts.

### Decision 13: How a webhook-receiving bot is reachable, and how it calls out — *Accepted*

- **Inbound.** The bot binds a hostname and a `public` route for its webhook path, for example
  `https://bot.<domain>/api/messages` for the Azure Bot Service messaging endpoint, or Slack's
  Events API request URL. The bot verifies each request itself. A settings page, if the template
  has one, is a second route on the same hostname with `access = "login"`. A Slack bot in Socket
  Mode needs no route at all: it opens an outbound WebSocket.
- **Outbound.** Egress from the VMs is open on both clouds (security group egress rule on AWS,
  `outbound_default_policy = "accept"` on Scaleway), and the services network routes it, so the
  bot calls Teams or Slack directly. Its tokens are secrets its manifest declares.
- **Data for notifications.** Either an Airflow DAG posts the notification after a run (DAG code
  already reads the warehouse), or the bot queries through the MCP as a service principal
  (`svc_<name>`, client credentials stored as declared secrets) over `https://mcp.<domain>`, so
  its reads are governed by its groups and audited. A service never receives a warehouse
  credential: `LOADER_PASSWORD` and the MCP's credentials are refused as declared secrets by #49.
  Requests from services reach the MCP from the VM's own public address, so the MCP's
  per-address rate limits count all of them together; the docs say so.

### Decision 14: The override file stays an escape hatch; the bundle content is unchanged — *Accepted*

The issue lists `CONTENT` as a blocker because a service could not bring files. With Decision 5 a
service's code and static files are package data inside the installed extension, in the image, so
the bundle needs nothing new beyond the `extensions/` entry the in-repo change adds and #50's
`extensions.lock.json`. `orchestration/docker-compose.override.yml` keeps working; the docs point
service authors to `vecten.services` instead, and say that an override service without a profile
starts on every host.

## Risks / Trade-offs

- [Streamlit and other web stacks may not resolve against the Airflow image] → Spike first
  (task 1.1). Fallback: Decision 5's option 2, additive.
- [A new hostname needs a Terraform apply before it serves] → `ship` reports the route as waiting
  and `next` offers `vecten deploy`; the PR plan shows the record.
- [Certbot stops NGINX briefly when a hostname is added] → Same as the first ship; the runbook
  says to add hostnames outside busy hours.
- [The gate adds a round trip per request, across VMs under `standard`] → Measured in the deploy
  tasks. If it matters, NGINX can cache verify answers for a few seconds keyed on the cookie,
  which bounds how late a group change applies. Task 5.6 measures it as rendered: the verify
  upstream is a per-request variable with no `upstream` block, so NGINX keeps no connection to
  `mcp.<domain>` open and each gated request across VMs opens a TLS connection. An `upstream`
  block with `keepalive` (resolved at start, re-resolved on reload) is the first remedy if the
  handshake dominates.
- [The MCP being down takes `login` routes down] → It is the identity provider; `public` routes
  are unaffected. The docs say so.
- [After a reboot Docker restarts services before the boot unit runs] → The guard unit adds
  the rule before `docker.service`, which requires it, so a failed guard keeps Docker down. Task 3.5 probes the metadata address from a service while a VM reboots.
- [iptables rules on two operating systems] → Amazon Linux 2023 and Ubuntu both run Docker with
  the iptables backend it manages, and `DOCKER-USER` is Docker's documented place for user
  rules. A Docker-tier test proves the block on the agent's own commands; a real deploy proves it
  on each cloud.
- [In-repo service declarations can go stale against `extensions.lock.json`] → `up` refuses a
  differing record; the gap for edits never run locally is noted for the in-repo change.
- [The memory budget is advisory] → Printed by `ship --check` and `up`; the docs name the sizing
  overrides.
- [Host-local volumes are lost with the VM] → Stated in the docs, the declaration's docstring
  and the runbook; durable stores are #40's.

## Migration Plan

- Additive. A project without `[services]` renders byte-identical Compose and NGINX, its
  `host.json` carries `service_secrets = []` and `service_network = ""`, and `vecten deploy`
  passes no new variable; tests assert each.
- Projects that bind a hostname run `vecten update` (thin root gains `service_hostnames`), then
  `vecten deploy --env <env>`, then ship as usual.
- `host.json` gains two keys with legacy defaults in the agent, so a bundle built before this
  change still applies on a host whose agent copy is newer.
- Landing order: #50's locking PR, then #49's secrets and read API, then the in-repo extensions
  change, then this change's pull requests (tasks.md), then `extension-analytics-apps`. This
  change's deltas to `shared/extension-points` and `cli/cli-lifecycle` are written over #50's
  text of the same requirements and are rebased if #50 changes.

## Security considerations

- **Origin isolation.** No extension content shares an origin with the admin panel, OAuth pages
  or Airflow's UI (Decision 2).
- **Identity headers.** Set or cleared by NGINX on every location; trusted only on `login` routes;
  services unreachable except through NGINX (Decision 4).
- **Gate.** Opaque random session ids hashed in `mcp_state`; `__Host-` cookie; one-time codes
  bound to a hostname the bundle's `vecten.toml` declares and to a verifier only the starting
  browser's state cookie holds; `rd` restricted to a path; sessions end with the MCP's lifetime, on disable and on password reset. The verify
  endpoint returns identity only for a valid session and is exempt from per-address limits,
  because all of one host's subrequests come from one address.
- **No cloud identity in services**, enforced by the agent's rules and the Compose file; only
  declared secrets injected (Decision 6).
- **Public routes** are explicit lines in `vecten.toml`; the docs require signature checks.
- **Allow-lists** reuse #58's validation, so every entry is a parsed network.
- **No telemetry.** The launcher and the docs turn off usage statistics of frameworks that send
  them by default (Streamlit's `browser.gatherUsageStats`), in keeping with the edition boundary.
- **Supply chain.** A service runs the extension's code with the network reach above; the docs
  repeat that only trusted extensions should be installed.
