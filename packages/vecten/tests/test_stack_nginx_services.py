"""NGINX for service hostnames: segment-aware routes, open health, identity headers cleared."""

import re

import pytest

from vecten.stack.nginx import (
    IDENTITY_HEADERS,
    ServiceRoute,
    ServiceSite,
    generate_nginx_conf,
    service_site,
)
from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import Extension
from vecten_shared.services import bound_services, parse_service

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
        assert "proxy_set_header Upgrade $websocket_upgrade;" in body
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


@pytest.mark.parametrize("components", [("platform", "mcp"), ("platform",)])
def test_every_location_of_a_service_hostname_sets_the_identity_headers_itself(components):
    routes = [
        ServiceRoute("/", "public", websocket=True),
        ServiceRoute("/api", "public", allow=("10.0.0.0/8",)),
        ServiceRoute("/settings", "login", groups=("finance",), key="acme-bot.bot.settings"),
    ]
    text = conf(site(routes=routes), components=components)
    found = locations(tls(text, "bot.data.acme.com"))
    if "mcp" in components:
        assert "X-Vecten-On-Behalf-Of" not in tls(text, "mcp.data.acme.com")
    assert len(found) >= 13
    assert len(IDENTITY_HEADERS) == 5
    for key, body in found.items():
        assert 'proxy_set_header X-Vecten-On-Behalf-Of "";' in body, key
        login = key in ("= /settings", "/settings/")
        for header in IDENTITY_HEADERS:
            variable = "$vecten_gate_" + header.removeprefix("X-Vecten-").lower()
            expected = variable if login else '""'
            assert f"proxy_set_header {header} {expected};" in body, (key, header)
            assert body.count(f"proxy_set_header {header} ") == 1, (key, header)
        if "proxy_pass $vecten_gate_upstream/gate/" not in body and "proxy_pass" in body:
            assert "proxy_set_header Cookie $vecten_service_cookie;" in body, key
        elif "proxy_pass" in body:
            # The gate's own paths forward the gate's cookies alone, to the MCP.
            assert "proxy_set_header Cookie $vecten_service_cookie;" not in body, key
            assert '"__Host-vecten-gate=$vecten_gate_session' in body, key


def test_the_gate_cookies_are_stripped_by_maps_that_keep_every_other_cookie():
    text = conf(site())
    assert "map $http_cookie $vecten_cookie_stripped {" in text
    assert "map $vecten_cookie_stripped $vecten_cookie_stripped_twice {" in text
    assert "map $vecten_cookie_stripped_twice $vecten_service_cookie {" in text
    assert text.index("map $http_cookie") < text.index("server {")
    passes = [
        (re.search(rf'"~\*(\^.*?)" "\$vecten_cookie_{a}\$vecten_cookie_{b}";', text), a, b)
        for a, b in (("before", "after"), ("head", "tail"))
    ]
    assert all(found for found, _, _ in passes)

    def strip(cookie):
        for found, a, b in passes:
            match = re.match(found.group(1).replace("(?<", "(?P<"), cookie, re.I)
            cookie = (
                cookie if not match else match[f"vecten_cookie_{a}"] + match[f"vecten_cookie_{b}"]
            )
        return re.sub(r"^;\s*", "", cookie)

    gate, state = "__Host-vecten-gate=abc", "__Host-vecten-gate-state=V"
    assert strip(state) == "" and strip(f"a=1; {state}; b=2") == "a=1; b=2"
    assert strip(f"{gate}; a=1; {state}") == "a=1"
    assert strip(f"a=1; {state}; b=2; {gate}") == "a=1; b=2"
    assert strip("__HOST-vecten-GATE-state=V; a=1") == "a=1"
    assert strip("x__Host-vecten-gate-state=V; a=1") == "x__Host-vecten-gate-state=V; a=1"
    # One pass per name: a second cookie of a name stays, and the request is refused.
    assert strip("__HOST-VECTEN-GATE=decoy; __Host-vecten-gate=REAL") == "__Host-vecten-gate=REAL"
    assert strip("__Host-vecten-gate-statex=V; a=1") == "__Host-vecten-gate-statex=V; a=1"
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


def test_a_login_route_is_held_where_no_mcp_can_admit_it():
    # A laptop whose components leave the MCP out has no gate to ask.
    routes = [ServiceRoute("/", "public"), ServiceRoute("/settings", "login", groups=("finance",))]
    text = conf(
        site(host="bot.localhost:8443", routes=routes), mode="local", components=["platform"]
    )
    found = locations(tls(text, "bot.localhost"))
    for key in ("= /settings", "/settings/"):
        assert "return 503;" in found[key]
        assert "proxy_pass" not in found[key] and "auth_request" not in found[key]
    assert "/_vecten/gate/" not in text


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
        # Python's `$` also matches before a final newline, so `/admin\n` reaches `/admin`.
        "/admin%0a",
        "/admin%0A/x",
        "/a%00b",
        "/a%09b",
        "/a%1Fb",
        "/a%1fb",
        "/a%7Fb",
        "/a%7f",
    ):
        assert re.search(regex, uri), uri
    for uri in (
        "/api/messages",
        "/api/file.js",
        "/api/a..b",
        "/api?next=/x/../y",
        "/api?u=a//b",
        "/api?q=a%0Ab",
        "/a%20b",
        "/a%2Ab",
        "/a%7Eb",
    ):
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
    assert re.search(regex, "a=1; __Host-vecten-gate-state=V", re.I)
    assert re.search(regex, "a=1; __host-vecten-gate=b", re.I)
    assert not re.search(regex, "a=1; b=2", re.I)
    assert not re.search(regex, "x__Host-vecten-gate=1", re.I)
    for key, body in locations(tls(text, "bot.data.acme.com")).items():
        refuses = "if ($vecten_cookie_repeated) {\n            return 400;\n        }" in body
        assert refuses == ("proxy_pass" in body), key


_WORD = r"""(?:"([^"]*)"|'([^']*)'|(\S+?))"""


def map_value(text: str, source: str, target: str, value: str) -> str:
    """What NGINX's `map <source> <target>` gives for `value`: the first matching regex, else
    the exact string, else the default."""
    found = re.search(
        rf"^map {re.escape(source)} {re.escape(target)} {{\n(.*?)\n}}", text, re.S | re.M
    )
    assert found, (source, target)
    table = {}
    for match in re.finditer(rf"^\s+{_WORD}\s+{_WORD};$", found.group(1), re.M):
        key = next(g for g in match.groups()[:3] if g is not None)
        table[key] = next(g for g in match.groups()[3:] if g is not None)
    for key, result in table.items():
        if key.startswith("~*") and re.search(key[2:], value, re.I):
            return result
        if key.startswith("~") and not key.startswith("~*") and re.search(key[1:], value):
            return result
    return table.get(value, table["default"])


@pytest.mark.parametrize("components", [("platform", "mcp"), ("platform",)])
def test_only_a_websocket_upgrade_is_forwarded(components):
    # A server accepting another protocol on the upgraded connection, such as h2c, would take
    # its later requests past every check NGINX makes per request.
    text = conf(site(routes=[ServiceRoute("/", "public", websocket=True)]), components=components)
    for sent in ("websocket", "WebSocket", "WEBSOCKET"):
        assert map_value(text, "$http_upgrade", "$websocket_upgrade", sent) == "websocket"
    for sent in ("h2c", "websocket, h2c", "h2c, websocket", "websocketx", "", "HTTP/2.0"):
        assert map_value(text, "$http_upgrade", "$websocket_upgrade", sent) == "", sent
    assert map_value(text, "$websocket_upgrade", "$connection_upgrade", "websocket") == "upgrade"
    assert map_value(text, "$websocket_upgrade", "$connection_upgrade", "") == "close"
    assert "$http_upgrade" not in text.replace("map $http_upgrade ", "")
    upgrades = re.findall(r"proxy_set_header Upgrade .*", text)
    assert upgrades and set(upgrades) == {"proxy_set_header Upgrade $websocket_upgrade;"}


def bound_site(declared_routes: dict, bound_routes: dict) -> ServiceSite:
    declared = {
        "command": ["python", "-m", "acme.portal"],
        "port": 8000,
        "health": {"path": "/healthz"},
        "routes": declared_routes,
    }
    config = VectenConfig.model_validate(
        {
            "users": 5,
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "extensions": ["acme"],
            "deploy": {
                "cloud": "scaleway",
                "region": "fr-par",
                "domain": "data.acme.com",
                "dns_zone": "acme.com",
                "github_repository": "acme/platform",
            },
            "services": {"acme": {"portal": {"hostname": "portal", "routes": bound_routes}}},
        }
    )
    service = parse_service("acme", "portal", declared).service
    record = {"acme": Extension("1.0", (), services={"portal": service})}
    (bound,) = bound_services(config, record)
    return service_site(bound, "portal.data.acme.com")


HOME = {"path": "/", "access": "public"}
ADMIN = {"path": "/admin", "access": "login", "groups": ["default"]}


def test_a_declared_route_left_unbound_answers_404_under_a_bound_route_covering_it():
    site = bound_site({"home": HOME, "admin": ADMIN}, {"home": {"access": "public"}})
    found = locations(tls(conf(site), "portal.data.acme.com"))
    assert "proxy_pass $upstream;" in found["/"]
    for key in ("= /admin", "/admin/"):
        assert "return 404;" in found[key], key
        assert "proxy_pass" not in found[key] and "auth_request" not in found[key], key
        for header in IDENTITY_HEADERS:
            assert f'proxy_set_header {header} "";' in found[key], (key, header)


def test_a_bound_route_under_an_unbound_one_is_still_served():
    nested = {"path": "/admin/api", "access": "public"}
    site = bound_site(
        {"home": HOME, "admin": ADMIN, "api": nested},
        {"home": {"access": "public"}, "api": {"access": "public"}},
    )
    found = locations(tls(conf(site), "portal.data.acme.com"))
    assert "return 404;" in found["/admin/"] and "proxy_pass" not in found["/admin/"]
    assert "proxy_pass $upstream;" in found["/admin/api/"]
    assert "proxy_pass $upstream;" in found["= /admin/api"]


def case_refused(text: str, host: str, uri: str) -> bool:
    """Whether the server for `host` answers `uri` (NGINX's decoded, merged $uri) with 404
    before any location is chosen."""
    block = tls(text, host)
    check = r"^    if \((\$vecten_case_\w+)\) {\n        return 404;\n    }$"
    found = re.search(check, block, re.M)
    if not found:
        return False
    return map_value(text, "$uri", found.group(1), uri) == "1"


PORTAL = "portal.data.acme.com"


def test_a_case_variant_of_a_declared_prefix_is_refused_before_any_route():
    site = bound_site(
        {"home": HOME, "admin": ADMIN, "settings": {**ADMIN, "path": "/settings.v2"}},
        {"home": {"access": "public"}, "admin": {"access": "login", "groups": ["default"]}},
    )
    text = conf(site)
    for uri in ("/ADMIN", "/Admin/x", "/aDmIn/users", "/SETTINGS.V2/a", "/Settings.v2"):
        assert case_refused(text, PORTAL, uri), uri
    for uri in ("/", "/admin", "/admin/x", "/settings.v2/a", "/adminx", "/ADMINX", "/docs/x"):
        assert not case_refused(text, PORTAL, uri), uri
    # A regex metacharacter in a prefix matches only itself.
    assert not case_refused(text, PORTAL, "/settingsXv2/a")
    # The check runs in the server's rewrite phase, ahead of every location.
    block = tls(text, PORTAL)
    assert block.index("if ($vecten_case_") < block.index("location ")


def test_a_case_variant_is_judged_by_the_longest_declared_prefix():
    nested = {"path": "/admin/public", "access": "public"}
    secret = {"path": "/docs/secret", "access": "login", "groups": ["default"]}
    docs = {"path": "/docs", "access": "public"}
    site = bound_site(
        {"admin": ADMIN, "public": nested, "docs": docs, "secret": secret},
        {
            "admin": {"access": "login", "groups": ["default"]},
            "public": {"access": "public"},
            "docs": {"access": "public"},
            "secret": {"access": "login", "groups": ["default"]},
        },
    )
    text = conf(site)
    for uri in ("/admin/public/x", "/admin/x", "/docs/x", "/docs/secret/x", "/docs/SECRETx"):
        assert not case_refused(text, PORTAL, uri), uri
    for uri in ("/ADMIN/public/x", "/admin/PUBLIC/x", "/docs/SECRET/x", "/DOCS/secret"):
        assert case_refused(text, PORTAL, uri), uri


def test_prefixes_differing_only_in_case_each_keep_their_own_path():
    site = bound_site(
        {"lower": {"path": "/docs", "access": "public"}, "upper": {**ADMIN, "path": "/Docs"}},
        {"lower": {"access": "public"}, "upper": {"access": "login", "groups": ["default"]}},
    )
    text = conf(site)
    assert not case_refused(text, PORTAL, "/docs/x")
    assert not case_refused(text, PORTAL, "/Docs/x")
    assert case_refused(text, PORTAL, "/DOCS/x")


def test_a_site_whose_only_route_is_the_root_needs_no_case_check():
    text = conf(site(routes=[ServiceRoute("/", "public")]))
    assert "$vecten_case_" not in text


def test_each_site_gets_its_own_case_check():
    text = conf(
        site(host="bot.data.acme.com", routes=[ServiceRoute("/a", "public")]),
        site(host="portal.data.acme.com", routes=[ServiceRoute("/b", "public")]),
    )
    assert case_refused(text, "bot.data.acme.com", "/A/x")
    assert not case_refused(text, "bot.data.acme.com", "/B/x")
    assert case_refused(text, "portal.data.acme.com", "/B/x")


def test_an_unbound_route_at_the_root_leaves_one_fallback():
    site = bound_site(
        {"home": HOME, "hook": {"path": "/hook", "access": "public"}},
        {"hook": {"access": "public"}},
    )
    block = tls(conf(site), "portal.data.acme.com")
    assert block.count("    location / {") == 1
    found = locations(block)
    assert "return 404;" in found["/"] and "proxy_pass" not in found["/"]
    assert "proxy_pass $upstream;" in found["/hook/"]


def test_rendered_locations_enforce_the_declared_route_contract():
    model = bound_site(
        {"home": HOME, "admin": ADMIN, "reports": {"path": "/reports", "access": "public"}},
        {"home": {"access": "public"}, "admin": {"access": "login", "groups": ["default"]}},
    )
    text = conf(model)
    rendered = locations(tls(text, model.host))
    for route in model.routes:
        body = rendered["/" if route.path == "/" else "= " + route.path]
        if route.access == "login":
            assert f"auth_request /_vecten/gate/verify/{route.key};" in body
        else:
            assert "auth_request " not in body
        for name in (*IDENTITY_HEADERS, "X-Vecten-On-Behalf-Of"):
            headers = re.findall(rf"proxy_set_header {name} (.*?);", body)
            expected = (
                '""'
                if route.access == "public" or name.endswith("On-Behalf-Of")
                else "$vecten_gate_" + name.removeprefix("X-Vecten-").lower()
            )
            assert headers == [expected]
        assert "proxy_set_header Cookie $vecten_service_cookie;" in body
        assert "proxy_set_header Upgrade $websocket_upgrade;" in body
    for path in model.unbound:
        body = rendered["= " + path]
        assert "return 404;" in body and "proxy_pass" not in body
