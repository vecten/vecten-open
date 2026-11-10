import re

from vecten.stack.compose import generate_compose
from vecten.stack.nginx import generate_nginx_conf
from vecten_shared.config import VectenConfig

HOSTS = {"airflow": "airflow.localhost", "mcp": "mcp.localhost"}
VM_HOSTS = {"airflow": "airflow.data.acme.example", "mcp": "mcp.data.acme.example"}
DEPLOY = {
    "cloud": "aws",
    "region": "eu-central-1",
    "domain": "data.acme.example",
    "dns_zone": "acme.example",
    "github_repository": "acme/platform",
}


def servers(conf):
    return re.findall(r"server\s*{(.*?)\n}", conf, re.S)


def mcp_block(conf):
    return next(b for b in servers(conf) if "server_name mcp.localhost" in b and "listen 443" in b)


def location(block, prefix):
    start = block[block.index(prefix) :]
    return start[: start.index("\n    }") + 6]


def test_public_admin_without_allow_list_adds_no_location():
    assert "location /admin" not in generate_nginx_conf(["mcp"], HOSTS, "local")


def test_admin_allow_list_restricts_only_the_panel():
    conf = generate_nginx_conf(
        ["mcp"], HOSTS, "vm", admin_allow=["203.0.113.0/24", "2001:db8::/32"]
    )
    block = mcp_block(conf)
    admin = location(block, "location /admin {")
    assert "allow 203.0.113.0/24;" in admin and "allow 2001:db8::/32;" in admin
    assert admin.index("allow 2001:db8::/32;") < admin.index("deny all;")
    assert "set $upstream http://mcp-server:8001;" in admin
    assert "deny all" not in location(block, "location / {")


def test_admin_off_takes_the_panel_off_the_public_listener():
    block = mcp_block(generate_nginx_conf(["mcp"], HOSTS, "vm", admin_panel="off"))
    assert location(block, "location /admin {").startswith("location /admin {\n        return 404;")


def test_admin_settings_do_not_touch_the_airflow_server():
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, "vm", admin_panel="off")
    airflow = next(
        b for b in servers(conf) if "server_name airflow.localhost" in b and "listen 443" in b
    )
    assert "/admin" not in airflow


def test_admin_off_publishes_the_mcp_port_on_vm_loopback_only(tmp_path):
    raw = {
        "users": 5,
        "components": ["mcp"],
        "project": {"name": "acme", "scaffold_version": "0.1.0"},
        "deploy": DEPLOY,
    }
    doc = generate_compose(
        VectenConfig.model_validate(raw), "vm", ["mcp"], tmp_path, hosts=VM_HOSTS
    )
    assert "ports" not in doc["services"]["mcp-server"]
    raw["mcp"] = {"admin_panel": "off"}
    doc = generate_compose(
        VectenConfig.model_validate(raw), "vm", ["mcp"], tmp_path, hosts=VM_HOSTS
    )
    assert doc["services"]["mcp-server"]["ports"] == ["127.0.0.1:8001:8001"]
