"""NGINX front door: one server per hostname, nothing else answered."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
from vecten_shared.services import Bound, route_prefix

# NGINX is the edge on every cloud, so a forwarded chain could only hold what the client wrote.
PROXY_HEADERS = """        proxy_http_version 1.1;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
"""
# NGINX resolves a literal proxy_pass host once, at start. Compose recreates containers on new
# addresses that another container may take, so the name is looked up per request through
# Docker's embedded DNS, which a variable in proxy_pass forces.
RESOLVER = "resolver 127.0.0.11 valid=10s ipv6=off;\n"


def _upstream(url: str, uri: str = "", variable: str = "$upstream") -> str:
    return f"        set {variable} {url};\n        proxy_pass {variable}{uri};\n"


AIRFLOW_UPSTREAM = "http://airflow-api-server:8080"
AIRFLOW_PROXY = (
    _upstream(AIRFLOW_UPSTREAM) + PROXY_HEADERS + "        proxy_set_header Host $http_host;\n"
    "        proxy_set_header Upgrade $http_upgrade;\n"
    "        proxy_set_header Connection $connection_upgrade;\n"
    "        proxy_read_timeout 300s;\n"
)
AIRFLOW_HEALTH_PATH = "/api/v2/monitor/health"
# NGINX matches a decoded, normalised URI but forwards the one the client sent, so a request
# that normalises onto the open health path could name another Airflow route. Forwarding the
# path itself, and reads only, keeps the opening to the health answer.
AIRFLOW_HEALTH_PROXY = (
    "        limit_except GET {\n            deny all;\n        }\n"
    + _upstream(AIRFLOW_UPSTREAM, AIRFLOW_HEALTH_PATH)
    + PROXY_HEADERS
    + "        proxy_set_header Host $http_host;\n"
)
HOST_KEYS = {"platform": "airflow", "mcp": "mcp"}
CONNECTION_UPGRADE_MAP = (
    "map $http_upgrade $connection_upgrade {\n    default upgrade;\n    '' close;\n}\n"
)


def _bare(host: str) -> str:
    # Local hostnames carry the published port; server_name and file names take none.
    return host.split(":", 1)[0]


def _tls(host: str, mode: str) -> str:
    if mode == "local":
        return (
            f"    ssl_certificate /etc/nginx/certs/{host}.crt;\n"
            f"    ssl_certificate_key /etc/nginx/certs/{host}.key;\n"
        )
    return (
        f"    ssl_certificate /etc/letsencrypt/live/{host}/fullchain.pem;\n"
        f"    ssl_certificate_key /etc/letsencrypt/live/{host}/privkey.pem;\n"
    )


MCP_PROXY = (
    _upstream("http://mcp-server:8001") + PROXY_HEADERS + "        proxy_set_header Host $host;\n"
    "        proxy_set_header Connection '';\n        proxy_buffering off;\n"
    "        proxy_cache off;\n"
    "        proxy_read_timeout 86400s;\n"
)


def _allow_rules(networks: list[str]) -> str:
    return "".join(f"        allow {network};\n" for network in networks) + "        deny all;\n"


def _admin_location(admin_panel: str, admin_allow: list[str]) -> str:
    # The panel is behind admin login either way; these narrow who reaches the login page.
    if admin_panel == "off":
        return "    location /admin {\n        return 404;\n    }\n"
    if not admin_allow:
        return ""
    return "    location /admin {\n" + _allow_rules(admin_allow) + MCP_PROXY + "    }\n"


def _airflow_locations(airflow_allow: list[str]) -> str:
    # Airflow's login guards every path either way; the list narrows who reaches the login.
    if not airflow_allow:
        return "    location / {\n" + AIRFLOW_PROXY + "    }\n"
    # `ship` polls the health path from wherever it runs, a CI runner whose address no list
    # can name included, and the path answers component states only, without a login.
    return (
        f"    location = {AIRFLOW_HEALTH_PATH} {{\n"
        + AIRFLOW_HEALTH_PROXY
        + "    }\n    location / {\n"
        + _allow_rules(airflow_allow)
        + AIRFLOW_PROXY
        + "    }\n"
    )


@dataclass(frozen=True)
class ServiceRoute:
    path: str
    access: str
    groups: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    websocket: bool = False
    max_body_mb: int = 1
    # `<extension>.<service>.<route>`: the name the gate checks a login request against.
    key: str = ""


@dataclass(frozen=True)
class ServiceSite:
    """One served service hostname: on the laptop it carries the published port."""

    host: str
    upstream: str
    health_path: str | None
    routes: tuple[ServiceRoute, ...]


IDENTITY_HEADERS = (
    "X-Vecten-User",
    "X-Vecten-Principal",
    "X-Vecten-Name",
    "X-Vecten-Groups",
    "X-Vecten-Admin",
)
SERVICE_HEALTH_PATH = "/_vecten/health"
GATE_COOKIE = "__Host-vecten-gate"
# A header NGINX sets itself is never copied from the client, whatever its letter case, and
# an empty value sends none; the default ignore_invalid_headers drops underscore spellings.
IDENTITY_CLEARED = "".join(f'        proxy_set_header {h} "";\n' for h in IDENTITY_HEADERS)
# The browser sends the gate's host-only cookie to every path of the service's hostname; a
# service holding it could replay it to the gate. Cookie names compare case-insensitively
# for the __Host- prefix, and the second map drops a separator the first leaves leading.
SERVICE_MAPS = (
    "map $http_cookie $vecten_cookie_stripped {\n"
    "    default $http_cookie;\n"
    f'    "~*^(?<vecten_cookie_before>.*?)(?:^\\s*|;\\s*){GATE_COOKIE}=[^;]*'
    '(?<vecten_cookie_after>.*)$" "$vecten_cookie_before$vecten_cookie_after";\n'
    "}\n"
    "map $vecten_cookie_stripped $vecten_service_cookie {\n"
    "    default $vecten_cookie_stripped;\n"
    '    "~^;\\s*(?<vecten_cookie_rest>.*)$" $vecten_cookie_rest;\n'
    "}\n"
    # A service may set a second cookie of that name in another letter case; the request
    # that still holds one after the strip is refused.
    "map $vecten_service_cookie $vecten_cookie_repeated {\n"
    "    default 0;\n"
    f'    "~*(?:^|;\\s*){GATE_COOKIE}=" 1;\n'
    "}\n"
    # The location is picked by the normalised path, the client's own is forwarded: these
    # forms would let the service read another route than the one NGINX checked.
    "map $request_uri $vecten_ambiguous_path {\n"
    "    default 0;\n"
    r'    "~^[^?]*(?:%2[fF]|%5[cC]|\\\\|;|%3[bB]|//|/(?:\.|%2[eE]){1,2}(?:/|\?|$))" 1;' + "\n"
    "}\n"
)


# The gate's cookies, read by their exact names, are the only cookies the MCP is sent.
GATE_STATE_COOKIE = "vecten-gate-state"
GATE_MAPS = (
    "map $http_cookie $vecten_gate_session {\n"
    '    default "";\n'
    f'    "~(?:^|;\\s*){GATE_COOKIE}=(?<vecten_gate_session_value>[^;]*)" '
    "$vecten_gate_session_value;\n"
    "}\n"
    "map $http_cookie $vecten_gate_state {\n"
    '    default "";\n'
    f'    "~(?:^|;\\s*){GATE_STATE_COOKIE}=(?<vecten_gate_state_value>[^;]*)" '
    "$vecten_gate_state_value;\n"
    "}\n"
)
GATE_PATH = "/_vecten/gate"
GATE_PATHS = ("start", "callback", "logout")
GATE_IDENTITY = {h: "$vecten_gate_" + h.removeprefix("X-Vecten-").lower() for h in IDENTITY_HEADERS}
# The verify answer's headers, copied into the request forwarded to the service; NGINX sends
# no client header of a name it sets itself.
IDENTITY_FROM_GATE = "".join(
    f"        proxy_set_header {header} {variable};\n" for header, variable in GATE_IDENTITY.items()
)
GATE_DENIED_PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>No access</title>'
    "</head><body><h1>No access</h1><p>Your account does not have access to this page. "
    f'<a href="{GATE_PATH}/logout">Sign out</a> to use another account.</p></body></html>'
)
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
GATE_UPSTREAM = "$vecten_gate_upstream"


@dataclass(frozen=True)
class GateUpstream:
    """Where a host's NGINX reaches the MCP's gate: on the Compose network beside it, or over
    HTTPS at mcp.<domain> with the certificate checked when the MCP runs on another VM."""

    url: str
    host: str
    tls: bool


def gate_upstream(
    host_components: list[str], hosts: dict[str, str], mode: str
) -> GateUpstream | None:
    if "mcp" in host_components:
        return GateUpstream("http://mcp-server:8001", _bare(hosts["mcp"]), False)
    if mode == "vm" and hosts.get("mcp"):
        name = _bare(hosts["mcp"])
        return GateUpstream(f"https://{name}", name, True)
    return None


def service_site(bound: Bound, host: str) -> ServiceSite:
    """A bound service served at `host`: paths and proxy needs from the declaration, access
    and networks from vecten.toml."""
    declared = bound.service
    return ServiceSite(
        host=host,
        upstream=f"http://{bound.compose_name}:{declared.port}" if declared.port else "",
        health_path=declared.health_path,
        routes=tuple(
            ServiceRoute(
                path=route.path,
                access=bound.route_bindings[name].access,
                groups=tuple(bound.route_bindings[name].groups),
                allow=tuple(bound.route_bindings[name].allow),
                websocket=route.websocket,
                max_body_mb=route.max_body_mb,
                key=f"{bound.key}.{name}",
            )
            for name, route in bound.routes.items()
        ),
    )


REFUSE_REPEATED_COOKIE = (
    "        if ($vecten_cookie_repeated) {\n            return 400;\n        }\n"
)


def _service_proxy(upstream: str, uri: str = "", identity: str = IDENTITY_CLEARED) -> str:
    return (
        REFUSE_REPEATED_COOKIE
        + identity
        + "        proxy_set_header Cookie $vecten_service_cookie;\n"
        + _upstream(upstream, uri)
        + PROXY_HEADERS
        + "        proxy_set_header Host $http_host;\n"
    )


def _gate_check(route: ServiceRoute) -> str:
    return (
        f"        auth_request {GATE_PATH}/verify/{route.key};\n"
        + "".join(
            f"        auth_request_set {variable} $upstream_http_"
            + header.lower().replace("-", "_")
            + ";\n"
            for header, variable in GATE_IDENTITY.items()
        )
        + "        error_page 401 = @vecten_gate_sign_in;\n"
        + "        error_page 403 = @vecten_gate_denied;\n"
    )


def _route_body(
    site: ServiceSite, route: ServiceRoute, mode: str, gate: GateUpstream | None
) -> str:
    if route.access != "public" and gate is None:
        # Only the gate may forward a login route; where no MCP can admit a request the path
        # is held here, so a public route covering it never forwards it unchecked.
        return IDENTITY_CLEARED + "        return 503;\n"
    login = route.access != "public"
    allow = _allow_rules(list(route.allow)) if route.allow and mode == "vm" else ""
    timeout = (
        "        proxy_read_timeout 86400s;\n        proxy_buffering off;\n"
        if route.websocket
        else "        proxy_read_timeout 300s;\n"
    )
    return (
        "        if ($vecten_ambiguous_path) {\n            return 400;\n        }\n"
        # The access phase runs allow before auth_request, so a refused address never
        # reaches the gate.
        + allow
        + (_gate_check(route) if login else "")
        + f"        client_max_body_size {route.max_body_mb}m;\n"
        + _service_proxy(site.upstream, identity=IDENTITY_FROM_GATE if login else IDENTITY_CLEARED)
        + "        proxy_set_header Upgrade $http_upgrade;\n"
        + "        proxy_set_header Connection $connection_upgrade;\n"
        + timeout
    )


def _location(match: str, body: str) -> str:
    return f"    location {match} {{\n{body}    }}\n"


def _gate_proxy(gate: GateUpstream, site: ServiceSite, uri: str, cookie: str) -> str:
    body = (
        IDENTITY_CLEARED
        + f'        proxy_set_header Cookie "{cookie}";\n'
        + f"        proxy_set_header X-Vecten-Gate-Host {_bare(site.host)};\n"
        # A subrequest shares the request's variables: the gate's address in $upstream would
        # replace the service's before the request is forwarded.
        + _upstream(gate.url, uri, GATE_UPSTREAM)
        + PROXY_HEADERS
        + f"        proxy_set_header Host {gate.host};\n"
    )
    if gate.tls:
        body += (
            "        proxy_ssl_server_name on;\n"
            f"        proxy_ssl_name {gate.host};\n"
            "        proxy_ssl_verify on;\n"
            f"        proxy_ssl_trusted_certificate {CA_BUNDLE};\n"
        )
    return body


def _gate_locations(site: ServiceSite, gate: GateUpstream) -> list[str]:
    logins = [route for route in site.routes if route.access != "public"]
    if not logins:
        return []
    if not all(route.key for route in logins):
        raise Failure(ExitCode.INTERNAL, f"a login route on {site.host} has no name")
    both = "__Host-vecten-gate=$vecten_gate_session; vecten-gate-state=$vecten_gate_state"
    out = [
        _location(
            f"= {GATE_PATH}/{path}",
            REFUSE_REPEATED_COOKIE + _gate_proxy(gate, site, f"/gate/{path}$is_args$args", both),
        )
        for path in GATE_PATHS
    ]
    for route in sorted(logins, key=lambda r: r.key):
        out.append(
            _location(
                f"= {GATE_PATH}/verify/{route.key}",
                "        internal;\n"
                + REFUSE_REPEATED_COOKIE
                + "        proxy_method GET;\n"
                + "        proxy_pass_request_body off;\n"
                + '        proxy_set_header Content-Length "";\n'
                + f"        proxy_set_header X-Vecten-Gate-Route {route.key};\n"
                + _gate_proxy(
                    gate, site, "/gate/verify", "__Host-vecten-gate=$vecten_gate_session"
                ),
            )
        )
    out += [
        _location(
            "@vecten_gate_sign_in",
            IDENTITY_CLEARED
            + "        absolute_redirect off;\n"
            + f"        return 302 {GATE_PATH}/start?rd=$request_uri;\n",
        ),
        _location(
            "@vecten_gate_denied",
            IDENTITY_CLEARED
            + "        default_type text/html;\n"
            + f"        return 403 '{GATE_DENIED_PAGE}';\n",
        ),
    ]
    return out


def _service_locations(site: ServiceSite, mode: str, gate: GateUpstream | None) -> str:
    out = []
    if site.health_path is not None:
        # Open to every address, as `ship` polls it from CI; the declared path is forwarded
        # whatever form of the URI matched.
        out.append(
            _location(
                f"= {SERVICE_HEALTH_PATH}",
                "        limit_except GET {\n            deny all;\n        }\n"
                + _service_proxy(site.upstream, site.health_path),
            )
        )
    reserved = IDENTITY_CLEARED + "        return 404;\n"
    out += [_location("= /_vecten", reserved), _location("/_vecten/", reserved)]
    if gate is not None:
        out += _gate_locations(site, gate)
    covered = False
    for route in sorted(site.routes, key=lambda r: route_prefix(r.path)):
        prefix = route_prefix(route.path)
        body = _route_body(site, route, mode, gate)
        if prefix == "/":
            covered = True
            out.append(_location("/", body))
        else:
            out += [_location(f"= {prefix}", body), _location(f"{prefix}/", body)]
    if not covered:
        out.append(_location("/", reserved))
    return "".join(out)


def _service_servers(site: ServiceSite, mode: str, gate: GateUpstream | None) -> list[str]:
    host = _bare(site.host)
    return [
        _redirect_server(site.host),
        f"server {{\n    listen 443 ssl;\n    server_name {host};\n"
        + _tls(host, mode)
        + _service_locations(site, mode, gate)
        + "}\n",
    ]


def _redirect_server(host: str) -> str:
    # $host drops the published port, so a host value carrying one names the target itself.
    target = f"https://{host}" if ":" in host else "https://$host"
    return (
        f"server {{\n    listen 80;\n    server_name {_bare(host)};\n"
        "    location /.well-known/acme-challenge/ { root /var/www/certbot; }\n"
        f"    location / {{ return 301 {target}$request_uri; }}\n}}\n"
    )


def generate_nginx_conf(
    host_components: list[str],
    hosts: dict[str, str],
    mode: Literal["local", "vm"],
    *,
    admin_panel: str = "public",
    admin_allow: list[str] | tuple[str, ...] = (),
    airflow_allow: list[str] | tuple[str, ...] = (),
    services: Sequence[ServiceSite] = (),
) -> str:
    unknown = [c for c in host_components if c not in HOST_KEYS]
    if unknown:
        raise Failure(ExitCode.INTERNAL, f"NGINX has no route for component(s) {unknown}")
    served = [hosts[HOST_KEYS[c]] for c in host_components]
    if not served:
        raise Failure(ExitCode.INTERNAL, "NGINX needs at least one served component")
    gate = gate_upstream(host_components, hosts, mode)
    gated = gate is not None and any(r.access != "public" for s in services for r in s.routes)
    out = [
        CONNECTION_UPGRADE_MAP
        + (SERVICE_MAPS if services else "")
        + (GATE_MAPS if gated else "")
        + RESOLVER,
        "server {\n    listen 80 default_server;\n    listen 443 ssl default_server;\n"
        "    server_name _;\n    ssl_reject_handshake on;\n    return 444;\n}\n",
    ]
    for host in served:
        out.append(_redirect_server(host))
    if "platform" in host_components:
        host = _bare(hosts["airflow"])
        out.append(
            f"server {{\n    listen 443 ssl;\n    server_name {host};\n"
            + _tls(host, mode)
            # Airflow's own login guards it: its UI sends its token as `Authorization: Bearer`,
            # which NGINX basic auth would take for missing credentials and challenge again.
            + _airflow_locations(list(airflow_allow))
            + "}\n"
        )
    if "mcp" in host_components:
        host = _bare(hosts["mcp"])
        out.append(
            f"server {{\n    listen 443 ssl;\n    server_name {host};\n"
            + _tls(host, mode)
            + _admin_location(admin_panel, list(admin_allow))
            + "    location / {\n"
            + MCP_PROXY
            + "    }\n}\n"
        )
    for site in services:
        out += _service_servers(site, mode, gate)
    return "\n".join(out)


def write_nginx(
    stack_dir: Path,
    host_components: list[str],
    hosts: dict[str, str],
    mode: Literal["local", "vm"],
    *,
    admin_panel: str = "public",
    admin_allow: list[str] | tuple[str, ...] = (),
    airflow_allow: list[str] | tuple[str, ...] = (),
    services: Sequence[ServiceSite] = (),
) -> None:
    conf_dir = stack_dir / "nginx" / "conf.d"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf = generate_nginx_conf(
        host_components,
        hosts,
        mode,
        admin_panel=admin_panel,
        admin_allow=admin_allow,
        airflow_allow=airflow_allow,
        services=services,
    )
    write_text_atomic(conf_dir / "default.conf", conf)
