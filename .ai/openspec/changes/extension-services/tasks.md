Items marked **[deploy]** can be proven only on a real deployment with DNS and certificates; they
are listed again in section 8. Everything else is proven by unit, Docker-tier or Terraform tests.

Pull requests, in order (sizes are rough, one engineer): A services core (3 days), B isolation
in the VM agent (2 days), C hostnames and public routes (4 days), D the gate (4 to 5 days),
E docs and proofs (1 to 2 days plus deploy time). A and C together are the smallest slice a bot
template can run on; B must land in the same release, before any public route reaches a client.

## 1. Spikes (before A)

- [x] 1.1 Resolve a Streamlit app's dependencies (`streamlit`, `tornado`, `pyarrow`, `protobuf`) as the `platform` extra against the Airflow base image's installed set with #50's `vecten lock`; record the result in design.md (Decision 5 fallback if it conflicts)
- [x] 1.2 Confirm the pinned Compose plugin honours `mem_limit`, `cpus` and a network's `com.docker.network.bridge.name` outside swarm
- [ ] 1.3 Confirm `iptables -I DOCKER-USER -i <bridge> -d 169.254.169.254 -j DROP` blocks a container on that bridge and only it, on Amazon Linux 2023 and on the Scaleway Ubuntu image, and survives a Docker restart

## 2. Services core (PR A)

- [x] 2.1 `vecten_shared.extensions`: the `vecten.services` group; declaration parsing and validation (keys, names, reserved `/_vecten/` prefix, duplicate paths), unknown keys reported
- [x] 2.2 #50's `vecten lock`: record service declarations in `extensions.lock.json`; `up` re-reads an editable in-repo extension's declaration and refuses a differing record
- [x] 2.3 `vecten_shared.config`: `[services.<extension>.<service>]` and its `routes` tables, with every refusal in the "binds each service" requirement
- [x] 2.4 `extension add`: write bindings from suggestions, keep existing ones, print public routes; `next` reports declared-but-unbound services and routes
- [x] 2.5 `stack/compose.py`: one Compose service per binding (name, image, profile, command, environment, mounts, limits, volume, health check), the `vecten-services` network with its fixed bridge name, `nginx` on both networks, no `depends_on` toward services
- [x] 2.6 Declared secrets reach services on the laptop as `${NAME:-}`, filtered by host scope; the read API reads a service's secrets from its environment when `VECTEN_COMPONENT=service`
- [x] 2.7 `up` starts services, honours `local = false`, prints health without waiting for it; `ship --check` and `up` print the per-host sum of limits
- [x] 2.8 Tests: declaration parsing; binding validation (each refusal); byte-identical Compose, NGINX and bundle for a project without services; Compose output per profile (standard, minimal, MCP-only, `host = "mcp"`); the stub extension's service (eleven groups); a Docker-tier test that `up` starts a service from the platform image, its volume survives a recreate, a crash-looping service leaves Airflow, the MCP and `nginx` up, and Airflow cannot resolve the service's name

## 3. Isolation in the VM agent (PR B)

- [x] 3.1 `bundle.py`: `service_secrets` and `service_network` in `host.json`
- [x] 3.2 `deploy/vm/apply.py`: the two `HOST_KEYS` with legacy defaults, the isolation step on ship and at boot (before Compose), service secrets read from the host's scopes and masked; copy byte-identical to `terraform/scaleway/modules/host/agent/apply.py`
- [x] 3.3 Compose: no cloud key, `AIRFLOW_CONN_*` or published port on any service
- [x] 3.4 Tests: agent unit tests with a fake runner (rule order before `compose up`, failure fails the step and recovers, legacy release starts without the step, masking); the existing byte-identity and Python 3.9 syntax checks; a Docker-tier test that a container on the services bridge cannot reach a fake metadata listener bound to the link-local address while a default-network container can
- [ ] 3.5 **[deploy]** On AWS and on Scaleway, a service cannot obtain the instance role or the VM key, also while the VM reboots, and Airflow and the MCP server still read their secrets

## 4. Hostnames and public routes (PR C)

- [ ] 4.1 Terraform, both clouds: `service_hostnames` variable with validation, records beside the built-in ones, `hosts[h].service_hostnames` output; `terraform test` for records per profile, apex refusal where domain equals zone, reserved labels, output parity
- [ ] 4.2 Thin root template declares and passes the variable; `vecten deploy` passes it from `vecten.toml` (including `--plan-only`) and refuses an old root naming `vecten update`; tests of the rendered root and of the passed variable
- [ ] 4.3 `EnvironmentOutputs`: `service_hostnames` per host, empty when absent; `bundle.py` appends them to `host.json`'s `hostnames`
- [ ] 4.4 `stack/nginx.py`: a port-80 server and a TLS server per served service hostname; route locations with upgrade, timeouts, body size and PR #58's allow helper; the five identity headers cleared on every location; the gate cookie stripped; `/_vecten/health` exact location (reads only, fixed upstream path); 404 fallback; no route on built-in hostnames
- [ ] 4.5 Laptop: `<label>.localhost` certificates from `stack/certs.py`, `allow` ignored
- [ ] 4.6 `ship`: poll each service health URL after the component polls, report states (`--json` too), never roll back for a service; report waiting routes and orphaned volumes; `next` offers `vecten deploy --env <env>` for waiting hostnames
- [ ] 4.7 Tests: NGINX rendering (public, allow, websocket, body size, header clearing, health location, fallback); a Docker-tier test with a stub upstream that logs requests: forged `X-Vecten-*` headers never arrive, `POST` and an encoded path on the health location behave as specified, an unbound path is 404, a WebSocket upgrade passes; bundle tests for `host.json` hostnames with and without outputs
- [ ] 4.8 **[deploy]** On AWS (standard) and on Scaleway (minimal): `vecten deploy` creates `portal.<domain>`, the next ship issues its certificate, the renewal timer's dry run (`certbot renew --dry-run`) covers it, and a public route answers over HTTPS
- [ ] 4.9 **[deploy]** A ship from a GitHub-hosted runner reports a routed service's health while its route's `allow` excludes the runner
- [ ] 4.10 **[deploy]** Apex binding (`@`) on a domain below its zone, on both clouds

## 5. The gate (PR D)

- [ ] 5.1 `vecten_mcp`: gate sessions in `mcp_state` (hashed ids, principal, hostname, expiry), with a migration
- [ ] 5.2 Endpoints: `/_vecten/gate/start`, `/gate/authorize`, `/_vecten/gate/callback`, `/_vecten/gate/logout`, verify; hostname and return-path checks against the bundle's `vecten.toml`; one-time codes; reuse of the browser session; exemption of verify from per-address limits
- [ ] 5.3 Session ends: lifetime, disable and delete, logout, password set (extends the existing revocation)
- [ ] 5.4 NGINX: `auth_request` on `login` routes, `auth_request_set` into the five headers, 401 to `start`, 403 page; the verify upstream on the Compose network on the MCP's host and over verified HTTPS at `mcp.<domain>` elsewhere; gate paths proxied to the MCP
- [ ] 5.5 Tests: MCP unit tests for each refusal (unknown hostname, absolute return path, replayed code, code on another host, service principal, disabled person), live group change, password reset ending gate sessions; NGINX rendering of `login` routes; a Docker-tier end-to-end test on the laptop stack: sign in with the password backend, reach a stub service with correct headers, 403 outside groups, forged headers ignored, logout
- [ ] 5.6 **[deploy]** Under `standard` on AWS, the platform VM's NGINX verifies against `https://mcp.<domain>` with the certificate checked; measure the added latency per request and record it in design.md
- [ ] 5.7 **[deploy]** With the `oidc` backend against a real provider, one sign-in covers two service hostnames, and the provider needs no new redirect URI

## 6. Docs

- [ ] 6.1 `docs/extensions.md`: the `vecten.services` group in the table; the declaration and the binding; package data for static files; trusted extensions only
- [ ] 6.2 New `docs/services.md`: hostnames and deploy, access modes, the gate and its headers (which to trust and why), webhooks and signature checks, Slack Socket Mode, outbound calls and data through the MCP, limits and sizing, volumes and what replaces a VM, the laptop and tunnels, health reporting
- [ ] 6.3 `docs/GLOSSARY.md`: Service, Route, Gate; runbook template: adding a hostname and the brief NGINX stop; `docs/operations.md`: service states in `ship` output
- [ ] 6.4 `CHANGELOG.md` under `[Unreleased]`

## 7. Interactions to settle with neighbouring changes

- [ ] 7.1 Rebase the `shared/extension-points` and `cli/cli-lifecycle` deltas onto #50's archived text
- [ ] 7.2 With the in-repo change: how a service declaration edited in code is caught in CI (see design.md, Decision 1)
- [ ] 7.3 With #5: confirm the gate covers Airflow's browser sign-in and record the API-client question in #5's change

## 8. Proven only by a real deploy

- [ ] 8.1 Task 3.5: no cloud identity in services, on both clouds
- [ ] 8.2 Task 4.8: DNS record, certificate issue and renewal for a service hostname, on both clouds
- [ ] 8.3 Task 4.9: service health polled from a runner outside the allow-list
- [ ] 8.4 Task 4.10: apex hostname
- [ ] 8.5 Task 5.6: cross-VM verification and its latency
- [ ] 8.6 Task 5.7: single sign-on across service hostnames with a real OIDC provider
- [ ] 8.7 A Teams bot template receives a Bot Framework message on its public route and posts a proactive message; a Slack bot verifies a signed Events API request
