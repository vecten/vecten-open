"""NGINX for service hostnames: segment-aware routes, open health, identity headers cleared."""

import re

import pytest

from vecten.stack.nginx import (
    IDENTITY_HEADERS,
    ServiceRoute,
    ServiceSite,
    generate_nginx_conf,
)

VM_HOSTS = {"airflow": "airflow.data.acme.com", "mcp": "mcp.data.acme.com"}
LOCAL_HOSTS = {"airflow": "airflow.localhost:8443", "mcp": "mcp.localhost:8443"}
UPSTREAM = "http://svc-acme-bot-bot:8000"


def site(host="bot.data.acme.com", routes=None, health="/healthz"):
    if routes is None:
        routes = (ServiceRoute("/api/messages", "public"),)
    return ServiceSite(host=host, upstream=UPSTREAM, health_path=health, routes=tuple(routes))


def conf(*sites, mode="vm", components=("platform", "mcp")):
    hosts = VM_HOSTS if mode == "vm" else LOCAL_HOSTS
    return generate_nginx_conf(list(components), hosts, mode, services=list(sites))


def servers(text: str) -> list[str]:
    return re.findall(r"^server\s*{\n(.*?)\n}", text, re.S | re.M)


def tls(text: str, name: str) -> str:
    return next(b for b in servers(text) if f"server_name {name};" in b and "listen 443" in b)


def locations(block: str) -> dict[str, str]:
    return dict(re.findall(r"^    location ([^{]+?) {\n(.*?)\n    }", block, re.S | re.M))


def test_a_project_without_services_renders_the_same_bytes():
    for mode, hosts in (("vm", VM_HOSTS), ("local", LOCAL_HOSTS)):
        before = generate_nginx_conf(["platform", "mcp"], hosts, mode)
        assert generate_nginx_conf(["platform", "mcp"], hosts, mode, services=[]) == before
        assert "vecten" not in before.lower()


def test_a_service_hostname_gets_a_challenge_server_and_a_tls_server():
    text = conf(site())
    plain = next(
        b for b in servers(text) if "server_name bot.data.acme.com;" in b and "listen 80;" in b
    )
    assert "location /.well-known/acme-challenge/ { root /var/www/certbot; }" in plain
    assert "return 301 https://$host$request_uri;" in plain
    block = tls(text, "bot.data.acme.com")
    assert "ssl_certificate /etc/letsencrypt/live/bot.data.acme.com/fullchain.pem;" in block


def test_a_route_covers_its_path_as_whole_segments():
    for path in ("/api/messages", "/api/messages/"):
        found = locations(
            tls(conf(site(routes=[ServiceRoute(path, "public")])), "bot.data.acme.com")
        )
        assert "= /api/messages" in found and "/api/messages/" in found
        # A plain prefix would also cover /api/messagesx.
        assert "/api/messages" not in found


def test_a_public_route_forwards_to_the_service_with_upgrade_and_the_default_limits():
    found = locations(tls(conf(site()), "bot.data.acme.com"))
    for key in ("= /api/messages", "/api/messages/"):
        body = found[key]
        assert f"set $upstream {UPSTREAM};" in body
        assert "proxy_pass $upstream;" in body
        assert "proxy_set_header Upgrade $http_upgrade;" in body
        assert "proxy_set_header Connection $connection_upgrade;" in body
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in body
        assert "proxy_read_timeout 300s;" in body
        assert "proxy_buffering off;" not in body
        assert "client_max_body_size 1m;" in body
        assert "allow " not in body and "deny all;" not in body


def test_a_websocket_route_waits_a_day_and_does_not_buffer():
    route = ServiceRoute("/", "public", websocket=True)
    body = locations(tls(conf(site(routes=[route])), "bot.data.acme.com"))["/"]
    assert "proxy_read_timeout 86400s;" in body
    assert "proxy_buffering off;" in body


def test_a_route_takes_its_body_limit():
    route = ServiceRoute("/upload", "public", max_body_mb=25)
    body = locations(tls(conf(site(routes=[route])), "bot.data.acme.com"))["/upload/"]
    assert "client_max_body_size 25m;" in body


def test_allow_narrows_the_route_on_a_vm_and_leaves_the_health_path_open():
    route = ServiceRoute("/", "public", allow=("10.0.0.0/8", "192.0.2.7/32"))
    found = locations(tls(conf(site(routes=[route])), "bot.data.acme.com"))
    assert "allow 10.0.0.0/8;\n        allow 192.0.2.7/32;\n        deny all;" in found["/"]
    assert "allow " not in found["= /_vecten/health"]
    assert "deny all;" not in found["= /_vecten/health"].replace(
        "limit_except GET {\n            deny all;\n        }", ""
    )


def test_the_laptop_ignores_allow():
    route = ServiceRoute("/", "public", allow=("10.0.0.0/8",))
    text = conf(site(host="bot.localhost:8443", routes=[route]), mode="local")
    body = locations(tls(text, "bot.localhost"))["/"]
    assert "allow " not in body and "deny all;" not in body


def test_every_location_of_a_service_hostname_clears_the_identity_headers_and_the_gate_cookie():
    routes = [
        ServiceRoute("/", "public", websocket=True),
        ServiceRoute("/api", "public", allow=("10.0.0.0/8",)),
        ServiceRoute("/settings", "login", groups=("finance",)),
    ]
    block = tls(conf(site(routes=routes)), "bot.data.acme.com")
    found = locations(block)
    assert len(found) >= 7
    assert len(IDENTITY_HEADERS) == 5
    for key, body in found.items():
        for header in IDENTITY_HEADERS:
            assert f'proxy_set_header {header} "";' in body, (key, header)
        if "proxy_pass" in body:
            assert "proxy_set_header Cookie $vecten_service_cookie;" in body, key


def test_the_gate_cookie_is_stripped_by_a_map_that_keeps_every_other_cookie():
    text = conf(site())
    assert "map $http_cookie $vecten_cookie_stripped {" in text
    assert "map $vecten_cookie_stripped $vecten_service_cookie {" in text
    assert text.index("map $http_cookie") < text.index("server {")
    pattern = re.search(r'"~\*(\^.*?)" "\$vecten_cookie_before\$vecten_cookie_after";', text)
    assert pattern
    regex = pattern.group(1).replace("(?<", "(?P<")

    def strip(cookie):
        match = re.match(regex, cookie, re.I)
        out = cookie if not match else match["vecten_cookie_before"] + match["vecten_cookie_after"]
        return re.sub(r"^;\s*", "", out)

    gate = "__Host-vecten-gate=abc"
    assert strip(gate) == ""
    assert strip(f"{gate}; a=1") == "a=1"
    assert strip(f"a=1; {gate}") == "a=1"
    assert strip(f"a=1; {gate}; b=2") == "a=1; b=2"
    assert strip("__host-VECTEN-gate=abc; a=1") == "a=1"
    assert strip("x__Host-vecten-gate=abc; a=1") == "x__Host-vecten-gate=abc; a=1"
    assert strip("a=1; b=2") == "a=1; b=2"


def test_the_health_path_is_exact_reads_only_and_forwards_the_declared_path():
    body = locations(tls(conf(site()), "bot.data.acme.com"))["= /_vecten/health"]
    assert "limit_except GET {\n            deny all;\n        }" in body
    assert f"set $upstream {UPSTREAM};" in body
    assert "proxy_pass $upstream/healthz;" in body


def test_a_health_command_gives_no_health_location():
    found = locations(tls(conf(site(health=None)), "bot.data.acme.com"))
    assert "= /_vecten/health" not in found


def test_a_path_no_route_covers_answers_404():
    found = locations(tls(conf(site()), "bot.data.acme.com"))
    assert "return 404;" in found["/"]
    assert "proxy_pass" not in found["/"]


def test_a_route_at_the_root_replaces_the_fallback():
    found = locations(tls(conf(site(routes=[ServiceRoute("/", "public")])), "bot.data.acme.com"))
    assert "proxy_pass $upstream;" in found["/"]
    assert "return 404;" not in found["/"]


def test_vectens_own_paths_never_reach_the_service():
    found = locations(tls(conf(site(routes=[ServiceRoute("/", "public")])), "bot.data.acme.com"))
    for key in ("= /_vecten", "/_vecten/"):
        assert "return 404;" in found[key] and "proxy_pass" not in found[key]


def test_a_login_route_is_not_forwarded_until_the_gate_serves_it():
    routes = [ServiceRoute("/", "public"), ServiceRoute("/settings", "login", groups=("finance",))]
    found = locations(tls(conf(site(routes=routes)), "bot.data.acme.com"))
    for key in ("= /settings", "/settings/"):
        assert "return 503;" in found[key]
        assert "proxy_pass" not in found[key]


def test_a_route_refuses_a_path_nginx_would_read_otherwise_than_the_service():
    text = conf(site())
    assert "map $request_uri $vecten_ambiguous_path {" in text
    pattern = re.search(r'map \$request_uri \$vecten_ambiguous_path \{\n.*?"~(.*?)" 1;', text, re.S)
    assert pattern
    regex = pattern.group(1).replace("\\\\", "\\")
    for uri in (
        "/api/%2e%2e/admin",
        "/api/.%2E/admin",
        "/api/../admin",
        "/api/./x",
        "/api/..",
        "/api/..?x=1",
        "/api%2Fadmin",
        "/api%2fadmin",
        "/api%5cadmin",
        "/api\\admin",
        "/api//admin",
        "/admin;/x",
        "/pub/..;/priv",
        "/a/%2E%2e;/b",
        "/a%3Bb",
        "/a%3bb",
    ):
        assert re.search(regex, uri), uri
    for uri in ("/api/messages", "/api/file.js", "/api/a..b", "/api?next=/x/../y", "/api?u=a//b"):
        assert not re.search(regex, uri), uri
    found = locations(tls(text, "bot.data.acme.com"))
    for key, body in found.items():
        refuses = "if ($vecten_ambiguous_path) {\n            return 400;\n        }" in body
        assert refuses == ("proxy_pass $upstream;" in body), key


def test_no_service_route_is_served_on_the_built_in_hostnames():
    text = conf(site(routes=[ServiceRoute("/", "public")]))
    for name in ("airflow.data.acme.com", "mcp.data.acme.com"):
        assert "svc-" not in tls(text, name)


def test_the_laptop_serves_a_label_below_localhost_with_its_own_certificate():
    text = conf(site(host="bot.localhost:8443"), mode="local")
    block = tls(text, "bot.localhost")
    assert "ssl_certificate /etc/nginx/certs/bot.localhost.crt;" in block
    assert "return 301 https://bot.localhost:8443$request_uri;" in text


@pytest.mark.parametrize("mode", ["local", "vm"])
def test_two_service_hostnames_get_their_own_servers(mode):
    suffix = ".localhost:8443" if mode == "local" else ".data.acme.com"
    text = conf(site(host=f"bot{suffix}"), site(host=f"portal{suffix}"), mode=mode)
    bare = ".localhost" if mode == "local" else ".data.acme.com"
    assert tls(text, f"bot{bare}") and tls(text, f"portal{bare}")
    assert text.count("map $http_cookie") == 1


def test_a_gate_cookie_left_after_the_strip_refuses_the_request():
    # A service can set a second cookie whose name differs only in case; the browser then
    # sends both, and one strip pass would leave the real one.
    text = conf(site(routes=[ServiceRoute("/", "public")]))
    assert "map $vecten_service_cookie $vecten_cookie_repeated {" in text
    pattern = re.search(
        r'map \$vecten_service_cookie \$vecten_cookie_repeated \{\n.*?"~\*(.*?)" 1;', text, re.S
    )
    assert pattern
    regex = pattern.group(1)
    assert re.search(regex, "__Host-vecten-gate=REAL", re.I)
    assert re.search(regex, "a=1; __host-vecten-gate=b", re.I)
    assert not re.search(regex, "a=1; b=2", re.I)
    assert not re.search(regex, "x__Host-vecten-gate=1", re.I)
    for key, body in locations(tls(text, "bot.data.acme.com")).items():
        refuses = "if ($vecten_cookie_repeated) {\n            return 400;\n        }" in body
        assert refuses == ("proxy_pass" in body), key
