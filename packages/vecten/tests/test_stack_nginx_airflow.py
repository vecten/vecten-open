import re

from vecten.stack.nginx import AIRFLOW_HEALTH_PATH, generate_nginx_conf

HOSTS = {"airflow": "airflow.data.acme.example", "mcp": "mcp.data.acme.example"}
NETWORKS = ["203.0.113.0/24", "2001:db8::/32"]


def servers(conf):
    return re.findall(r"server\s*{(.*?)\n}", conf, re.S)


def airflow_tls_block(conf):
    return next(
        b
        for b in servers(conf)
        if "server_name airflow.data.acme.example" in b and "listen 443" in b
    )


def location(block, header):
    start = block[block.index(header) :]
    return start[: start.index("\n    }") + 6]


def test_without_an_allow_list_airflow_has_one_open_location():
    block = airflow_tls_block(generate_nginx_conf(["platform"], HOSTS, "vm"))
    assert block.count("location ") == 1
    assert "allow " not in block and "deny all" not in block
    assert "set $upstream http://airflow-api-server:8080;" in location(block, "location / {")


def test_allow_list_narrows_every_airflow_path_but_health():
    conf = generate_nginx_conf(["platform"], HOSTS, "vm", airflow_allow=NETWORKS)
    block = airflow_tls_block(conf)
    root = location(block, "location / {")
    assert "allow 203.0.113.0/24;" in root and "allow 2001:db8::/32;" in root
    assert root.index("allow 2001:db8::/32;") < root.index("deny all;")
    assert "set $upstream http://airflow-api-server:8080;" in root
    # The ship health poll comes from runners no list can name, so the exact path stays open.
    health = location(block, f"location = {AIRFLOW_HEALTH_PATH} {{")
    assert "allow" not in health and "deny" not in health
    assert "set $upstream http://airflow-api-server:8080;" in health


def test_allow_list_leaves_the_challenge_and_redirect_on_port_80_open():
    conf = generate_nginx_conf(["platform"], HOSTS, "vm", airflow_allow=NETWORKS)
    plain = next(
        b
        for b in servers(conf)
        if "server_name airflow.data.acme.example" in b and "listen 80;" in b
    )
    assert "deny" not in plain and "/.well-known/acme-challenge/" in plain


def test_airflow_allow_list_does_not_touch_the_mcp_server():
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, "vm", airflow_allow=NETWORKS)
    mcp = next(
        b for b in servers(conf) if "server_name mcp.data.acme.example" in b and "listen 443" in b
    )
    assert "deny all" not in mcp and "allow " not in mcp


def test_mcp_admin_allow_list_does_not_touch_the_airflow_server():
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, "vm", admin_allow=NETWORKS)
    assert "deny all" not in airflow_tls_block(conf)
