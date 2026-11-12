"""NGINX for login routes: every request asks the MCP's gate, which names the person."""

import re

import pytest

from vecten.stack.nginx import IDENTITY_HEADERS, ServiceRoute

from .test_stack_nginx_services import conf, locations, servers, site, tls

HOME = ServiceRoute("/", "login", groups=("default",), key="acme-portal.web.home")
BOOKS = ServiceRoute("/books", "login", groups=("finance",), key="acme-portal.web.books")
HOOK = ServiceRoute("/hook", "public", key="acme-portal.web.hook")
PORTAL = "portal.data.acme.com"
COMPOSE_MCP = "set $vecten_gate_upstream http://mcp-server:8001;"
PUBLIC_MCP = "set $vecten_gate_upstream https://mcp.data.acme.com;"


def portal(*routes, host=PORTAL, **kwargs):
    return site(host=host, routes=routes or (HOME, BOOKS, HOOK), **kwargs)


def found(text, host=PORTAL):
    return locations(tls(text, host.split(":")[0]))


def test_a_login_route_asks_the_gate_for_each_request_and_forwards_its_answer():
    body = found(conf(portal()))["= /books"]
    assert "auth_request /_vecten/gate/verify/acme-portal.web.books;" in body
    for header in IDENTITY_HEADERS:
        variable = "$vecten_gate_" + header.removeprefix("X-Vecten-").lower()
        upstream = "$upstream_http_" + header.lower().replace("-", "_")
        assert f"auth_request_set {variable} {upstream};" in body
        assert f"proxy_set_header {header} {variable};" in body
    assert "error_page 401 = @vecten_gate_sign_in;" in body
    assert "error_page 403 = @vecten_gate_denied;" in body
    assert "set $upstream http://svc-acme-bot-bot:8000;" in body
    assert "proxy_set_header Cookie $vecten_service_cookie;" in body
    assert "if ($vecten_ambiguous_path) {\n            return 400;\n        }" in body
    assert "if ($vecten_cookie_repeated) {\n            return 400;\n        }" in body


def test_each_login_route_asks_with_its_own_name():
    text = conf(portal())
    assert "auth_request /_vecten/gate/verify/acme-portal.web.home;" in found(text)["/"]
    assert "auth_request" not in found(text)["= /hook"]


def test_the_check_is_internal_and_sends_only_the_gate_cookie_hostname_and_route():
    body = found(conf(portal()))["= /_vecten/gate/verify/acme-portal.web.books"]
    assert "        internal;\n" in body
    assert "proxy_method GET;" in body and "proxy_pass_request_body off;" in body
    assert 'proxy_set_header Content-Length "";' in body
    assert 'proxy_set_header Cookie "__Host-vecten-gate=$vecten_gate_session";' in body
    assert f"proxy_set_header X-Vecten-Gate-Host {PORTAL};" in body
    assert "proxy_set_header X-Vecten-Gate-Route acme-portal.web.books;" in body
    assert "proxy_pass $vecten_gate_upstream/gate/verify;" in body
    # The check runs inside the request, whose $upstream must still name the service.
    assert "$upstream " not in body.replace("$vecten_gate_upstream", "")


def test_without_a_session_the_browser_is_sent_to_start_on_the_same_hostname():
    body = found(conf(portal()))["@vecten_gate_sign_in"]
    # Relative, so the laptop's published port stays in the browser's address.
    assert "absolute_redirect off;" in body
    assert "return 302 /_vecten/gate/start?rd=$request_uri;" in body


def test_a_person_outside_the_groups_gets_a_page_and_a_way_to_sign_out():
    body = found(conf(portal()))["@vecten_gate_denied"]
    assert "default_type text/html;" in body
    page = re.search(r"return 403 '(.*)';", body)[1]
    assert "/_vecten/gate/logout" in page and "$" not in page and "'" not in page


@pytest.mark.parametrize("path", ["start", "callback", "logout"])
def test_the_gate_paths_go_to_the_mcp_with_the_hostname_and_the_gate_cookies(path):
    body = found(conf(portal()))[f"= /_vecten/gate/{path}"]
    assert COMPOSE_MCP in body
    assert f"proxy_pass $vecten_gate_upstream/gate/{path}$is_args$args;" in body
    assert f"proxy_set_header X-Vecten-Gate-Host {PORTAL};" in body
    assert "proxy_set_header Host mcp.data.acme.com;" in body
    assert (
        'proxy_set_header Cookie "__Host-vecten-gate=$vecten_gate_session; '
        'vecten-gate-state=$vecten_gate_state";' in body
    )
    assert "if ($vecten_cookie_repeated) {\n            return 400;\n        }" in body
    assert "internal;" not in body


def test_on_the_mcps_host_the_gate_is_reached_on_the_compose_network():
    text = conf(portal(), components=("mcp",))
    for key, body in found(text).items():
        if "proxy_pass $vecten_gate_upstream/gate/" in body:
            assert COMPOSE_MCP in body and "proxy_ssl" not in body, key


def test_on_another_host_the_gate_is_reached_over_https_with_the_certificate_checked():
    text = conf(portal(), components=("platform",))
    gate = {k: b for k, b in found(text).items() if "proxy_pass $vecten_gate_upstream/gate/" in b}
    assert len(gate) == 5
    for key, body in gate.items():
        assert PUBLIC_MCP in body, key
        assert "proxy_set_header Host mcp.data.acme.com;" in body
        assert "proxy_ssl_server_name on;" in body
        assert "proxy_ssl_name mcp.data.acme.com;" in body
        assert "proxy_ssl_verify on;" in body
        assert "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;" in body


def test_on_the_laptop_the_gate_keeps_the_published_port_out_of_its_upstream():
    text = conf(portal(host="portal.localhost:8443"), mode="local")
    body = found(text, "portal.localhost")["= /_vecten/gate/start"]
    assert COMPOSE_MCP in body and "proxy_set_header Host mcp.localhost;" in body
    assert "proxy_set_header X-Vecten-Gate-Host portal.localhost;" in body


def test_allow_on_a_login_route_is_checked_before_the_gate():
    route = ServiceRoute("/", "login", groups=("default",), allow=("10.0.0.0/8",), key="a.b.c")
    body = found(conf(portal(route)))["/"]
    assert body.index("deny all;") < body.index("auth_request ")


def test_a_hostname_without_login_routes_gets_no_gate():
    text = conf(portal(HOOK))
    assert "/_vecten/gate/" not in text and "auth_request" not in text
    assert "vecten_gate" not in text


def _map_regex(text, variable):
    pattern = re.search(rf'map \$http_cookie \${variable} \{{\n.*?"~(.*?)" \$', text, re.S)
    assert pattern, variable
    return pattern.group(1).replace("(?<", "(?P<")


def test_the_gate_cookies_are_read_by_their_exact_names():
    text = conf(portal())
    assert text.count("map $http_cookie $vecten_gate_session {") == 1
    session = _map_regex(text, "vecten_gate_session")
    state = _map_regex(text, "vecten_gate_state")

    def read(regex, cookie):
        match = re.search(regex, cookie)
        return next(v for v in match.groupdict().values()) if match else ""

    assert read(session, "a=1; __Host-vecten-gate=S1; b=2") == "S1"
    assert read(session, "__Host-vecten-gate=S1") == "S1"
    assert read(session, "x__Host-vecten-gate=S1") == ""
    assert read(session, "vecten-gate-state=V") == ""
    assert read(state, "__Host-vecten-gate=S1; vecten-gate-state=V") == "V"
    assert read(state, "xvecten-gate-state=V") == ""


def test_two_portals_share_the_maps_and_the_named_locations_stay_per_server():
    text = conf(portal(), portal(HOME, host="dash.data.acme.com"))
    assert text.count("map $http_cookie $vecten_gate_session {") == 1
    dash = found(text, "dash.data.acme.com")
    assert (
        "proxy_set_header X-Vecten-Gate-Host dash.data.acme.com;" in dash["= /_vecten/gate/start"]
    )
    assert len([b for b in servers(text) if "@vecten_gate_sign_in" in b]) == 2
