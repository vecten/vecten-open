"""NGINX front door: one server per hostname, nothing else answered."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic

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


def _upstream(url: str, uri: str = "") -> str:
    return f"        set $upstream {url};\n        proxy_pass $upstream{uri};\n"


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


def generate_nginx_conf(
    host_components: list[str],
    hosts: dict[str, str],
    mode: Literal["local", "vm"],
    *,
    admin_panel: str = "public",
    admin_allow: list[str] | tuple[str, ...] = (),
    airflow_allow: list[str] | tuple[str, ...] = (),
) -> str:
    unknown = [c for c in host_components if c not in HOST_KEYS]
    if unknown:
        raise Failure(ExitCode.INTERNAL, f"NGINX has no route for component(s) {unknown}")
    served = [hosts[HOST_KEYS[c]] for c in host_components]
    if not served:
        raise Failure(ExitCode.INTERNAL, "NGINX needs at least one served component")
    out = [
        CONNECTION_UPGRADE_MAP + RESOLVER,
        "server {\n    listen 80 default_server;\n    listen 443 ssl default_server;\n"
        "    server_name _;\n    ssl_reject_handshake on;\n    return 444;\n}\n",
    ]
    for host in served:
        # $host drops the published port, so a host value carrying one names the target itself.
        target = f"https://{host}" if ":" in host else "https://$host"
        out.append(
            f"server {{\n    listen 80;\n    server_name {_bare(host)};\n"
            "    location /.well-known/acme-challenge/ { root /var/www/certbot; }\n"
            f"    location / {{ return 301 {target}$request_uri; }}\n}}\n"
        )
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
    )
    write_text_atomic(conf_dir / "default.conf", conf)
