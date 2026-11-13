"""A bound container app in the generated Compose, NGINX and bundle; nothing for other apps."""

import json
import tomllib

import pytest
import tomli_w
import yaml

from vecten.stack.compose import SERVICES_NETWORK, generate_compose
from vecten.stack.nginx import generate_nginx_conf, service_site
from vecten_shared.apps import parse_app
from vecten_shared.config import VectenConfig, load_config
from vecten_shared.extension_record import Extension, read_record, record_text
from vecten_shared.services import bound_services
from vecten_shared.versions import VERSIONS

DEPLOY = {
    "cloud": "scaleway",
    "region": "fr-par",
    "domain": "data.acme.example",
    "dns_zone": "acme.example",
    "github_repository": "acme/platform",
}
VM_HOSTS = {"airflow": "airflow.data.acme.example", "mcp": "mcp.data.acme.example"}
OVERVIEW = {
    "title": "Sales overview",
    "description": "",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme/app.py",
    "datasets": ["orders"],
    "groups": ["default"],
}
FINANCE = {
    "title": "Finance",
    "description": "",
    "kind": "snowflake",
    "entry": "acme/finance.py",
    "datasets": ["revenue"],
    "groups": ["finance"],
}
SECRETS = ("ACME_API_KEY", "MCP_ACME_TOKEN")


def config(apps=None, **extra):
    return VectenConfig.model_validate(
        {
            "users": 5,
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "extensions": ["acme"],
            "deploy": DEPLOY,
            "apps": {"acme": apps} if apps else {},
            **extra,
        }
    )


def stack(tmp_path, declared=None, secrets=SECRETS):
    root = tmp_path / "proj"
    stack_dir = root / ".vecten" / "stack"
    stack_dir.mkdir(parents=True)
    apps = {
        name: parse_app("acme", name, value).app
        for name, value in (declared if declared is not None else {"overview": OVERVIEW}).items()
    }
    record = {"acme": Extension("1.0", (), secrets=secrets, apps=apps)}
    (root / "extensions.lock.json").write_text(record_text(record))
    return stack_dir


BOUND = {"overview": {"groups": ["sales"]}}


def test_a_container_app_runs_the_launcher_from_the_platform_image(tmp_path):
    doc = generate_compose(config(BOUND), "local", ["platform", "mcp"], stack(tmp_path))
    svc = doc["services"]["svc-acme-app-overview"]
    assert svc["image"] == f"{doc['name']}-airflow:{VERSIONS.vecten}"
    assert svc["profiles"] == ["platform"]
    assert svc["command"] == ["python", "-m", "vecten_shared.launch", "streamlit", "acme/app.py"]
    assert (svc["mem_limit"], svc["cpus"]) == ("512m", 0.5)
    assert svc["networks"] == [SERVICES_NETWORK]
    assert svc["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--silent",
        "--output",
        "/dev/null",
        "http://localhost:8501/_stcore/health",
    ]
    assert svc["environment"] == {
        "VECTEN_ENV": "local",
        "VECTEN_COMPONENT": "service",
        "VECTEN_SERVICE": "acme.app-overview",
        "VECTEN_PROJECT_ROOT": "/opt/airflow",
        "PORT": "8501",
        "VECTEN_PUBLIC_URL": "https://overview.localhost:8443",
        "ACME_API_KEY": "${ACME_API_KEY:-}",
        "APP_OVERVIEW_CLIENT_ID": "${APP_OVERVIEW_CLIENT_ID:-}",
        "APP_OVERVIEW_CLIENT_SECRET": "${APP_OVERVIEW_CLIENT_SECRET:-}",
    }


def test_on_the_laptop_no_mcp_url_is_rendered_since_no_container_reaches_it(tmp_path):
    doc = generate_compose(config(BOUND), "local", ["platform", "mcp"], stack(tmp_path))
    assert "VECTEN_MCP_URL" not in doc["services"]["svc-acme-app-overview"]["environment"]


def test_on_a_vm_the_app_reads_the_mcp_at_the_environments_hostname(tmp_path):
    doc = generate_compose(config(BOUND), "vm", ["platform"], stack(tmp_path), hosts=VM_HOSTS)
    env = doc["services"]["svc-acme-app-overview"]["environment"]
    assert env["VECTEN_MCP_URL"] == "https://mcp.data.acme.example/mcp"
    assert env["VECTEN_PUBLIC_URL"] == "https://overview.data.acme.example"
    assert env["APP_OVERVIEW_CLIENT_SECRET"] == "${APP_OVERVIEW_CLIENT_SECRET:-}"
    assert not [k for k in env if k.startswith(("SCW_", "AWS_", "AIRFLOW_CONN_"))]


def test_an_app_on_the_mcp_host_reads_its_mcp_credential_names(tmp_path):
    declared = {"overview": {**OVERVIEW, "components": ["mcp"]}}
    doc = generate_compose(config(BOUND), "local", ["platform", "mcp"], stack(tmp_path, declared))
    svc = doc["services"]["svc-acme-app-overview"]
    assert svc["profiles"] == ["mcp"]
    assert {k for k in svc["environment"] if "CLIENT" in k} == {
        "MCP_APP_OVERVIEW_CLIENT_ID",
        "MCP_APP_OVERVIEW_CLIENT_SECRET",
    }
    assert "MCP_ACME_TOKEN" in svc["environment"] and "ACME_API_KEY" not in svc["environment"]


def test_an_app_kept_off_the_laptop_is_not_run_there(tmp_path):
    bound = {"overview": {"groups": ["sales"], "local": False}}
    doc = generate_compose(config(bound), "local", ["platform", "mcp"], stack(tmp_path))
    assert "svc-acme-app-overview" not in doc["services"]


def test_the_agent_reads_the_app_credentials_from_the_hosts_scope(tmp_path):
    from vecten.stack.compose import host_services

    stack(tmp_path)
    record = read_record(tmp_path / "proj")
    names, bridge = host_services(config(BOUND), record, ["platform"])
    assert names == ["ACME_API_KEY", "APP_OVERVIEW_CLIENT_ID", "APP_OVERVIEW_CLIENT_SECRET"]
    assert bridge == "vecten-services"
    assert host_services(config(BOUND), record, ["mcp"]) == ([], "")


def test_nginx_serves_the_app_at_its_hostname_with_one_login_route_and_its_health(tmp_path):
    stack(tmp_path)
    [bound] = bound_services(config(BOUND), read_record(tmp_path / "proj"))
    site = service_site(bound, "overview.data.acme.example")
    assert site.upstream == "http://svc-acme-app-overview:8501"
    assert site.health_path == "/_stcore/health"
    [route] = site.routes
    assert (route.path, route.access, route.groups, route.websocket) == (
        "/",
        "login",
        ("sales",),
        True,
    )
    text = generate_nginx_conf(["platform"], VM_HOSTS, "vm", services=[site])
    assert "server_name overview.data.acme.example;" in text
    assert "proxy_pass $upstream/_stcore/health;" in tls(text, "overview.data.acme.example")


def test_nginx_limits_no_request_rate_by_address_for_the_app_or_the_mcp(tmp_path):
    # An app's MCP calls arrive from its host's one public address, so no
    # per-address rate limit may stand between the app and the MCP.
    stack(tmp_path)
    [bound] = bound_services(config(BOUND), read_record(tmp_path / "proj"))
    site = service_site(bound, "overview.data.acme.example")
    for components in (["platform"], ["mcp"], ["platform", "mcp"]):
        text = generate_nginx_conf(components, VM_HOSTS, "vm", services=[site])
        assert "limit_req" not in text and "limit_conn" not in text


def tls(text: str, name: str) -> str:
    import re

    blocks = re.findall(r"^server\s*{\n(.*?)\n}", text, re.S | re.M)
    return next(b for b in blocks if f"server_name {name};" in b and "listen 443" in b)


def _project_files(tmp_path, apps_record, apps_binding):
    """Compose and NGINX text for one project, local and on a VM."""
    root = tmp_path / "proj"
    stack_dir = root / ".vecten" / "stack"
    stack_dir.mkdir(parents=True, exist_ok=True)
    record = {"acme": Extension("1.0", (), secrets=SECRETS, apps=apps_record)}
    (root / "extensions.lock.json").write_text(record_text(record))
    cfg = config(apps_binding)
    rendered = {}
    for mode, hosts in (("local", None), ("vm", VM_HOSTS)):
        doc = generate_compose(
            cfg, mode, ["platform", "mcp"], stack_dir, project="acme", hosts=hosts
        )
        sites = [
            service_site(b, b.binding.hostname)
            for b in bound_services(cfg, read_record(root))
            if b.binding.hostname
        ]
        nginx = generate_nginx_conf(
            ["platform", "mcp"], hosts or {"airflow": "a", "mcp": "m"}, mode, services=sites
        )
        rendered[mode] = (yaml.safe_dump(doc, sort_keys=True), nginx)
    return rendered


@pytest.mark.parametrize(
    "binding", [None, {"finance": {"groups": ["finance"]}}], ids=["unbound", "snowflake-only"]
)
def test_a_project_without_a_bound_container_app_renders_the_same_bytes(tmp_path, binding):
    declared = {
        "overview": parse_app("acme", "overview", OVERVIEW).app,
        "finance": parse_app("acme", "finance", FINANCE).app,
    }
    without = _project_files(tmp_path, {}, None)
    assert _project_files(tmp_path, declared, binding) == without


def test_a_config_without_apps_saves_no_apps_table(tmp_path):
    from vecten_shared.config import save_config

    save_config(tmp_path, config())
    assert "apps" not in tomllib.loads((tmp_path / "vecten.toml").read_text())


# The bundle, through the deploy fixtures.


def _with_apps(root, bindings, declared=None):
    doc = tomllib.loads((root / "vecten.toml").read_text())
    doc["extensions"] = ["acme"]
    if bindings:
        doc["apps"] = {"acme": bindings}
    (root / "vecten.toml").write_text(tomli_w.dumps(doc))
    apps = {
        name: parse_app("acme", name, value).app
        for name, value in (declared if declared is not None else {"overview": OVERVIEW}).items()
    }
    (root / "extensions.lock.json").write_text(
        record_text({"acme": Extension("1.0", (), apps=apps)})
    )


@pytest.fixture
def project(tmp_path):
    from .deploy_fixtures import render_aws

    root = render_aws(tmp_path / "proj")
    (root / "dbt" / "target").mkdir()
    (root / "dbt" / "target" / "manifest.json").write_text("{}")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def _bundle(root, out, host="platform", outputs=None):
    from vecten.deploy.bundle import build_host_bundle
    from vecten_shared.provider.environment import EnvironmentOutputs

    from .deploy_fixtures import COMMIT, STANDARD_OUTPUTS

    parsed = EnvironmentOutputs.model_validate(outputs or STANDARD_OUTPUTS)
    return build_host_bundle(root, load_config(root), "prod", parsed, host, COMMIT, out)


def _read(bundle, name):
    import tarfile

    with tarfile.open(bundle, "r:gz") as tar:
        return tar.extractfile(name).read().decode("utf-8")


def test_the_bundle_serves_a_listed_app_hostname_and_hands_its_credentials_to_the_agent(
    project, tmp_path
):
    import copy

    from .deploy_fixtures import STANDARD_OUTPUTS
    from .services_fixtures import locked

    _with_apps(project, BOUND)
    locked(project)
    outputs = copy.deepcopy(STANDARD_OUTPUTS)
    outputs["hosts"]["platform"]["service_hostnames"] = ["overview.data.acme.com"]
    bundle = _bundle(project, tmp_path / "p", outputs=outputs)
    host = json.loads(_read(bundle, ".vecten/ship/host.json"))
    assert host["service_secrets"] == ["APP_OVERVIEW_CLIENT_ID", "APP_OVERVIEW_CLIENT_SECRET"]
    assert host["hostnames"] == ["airflow.data.acme.com", "overview.data.acme.com"]
    compose = yaml.safe_load(_read(bundle, ".vecten/stack/docker-compose.yml"))
    env = compose["services"]["svc-acme-app-overview"]["environment"]
    assert env["VECTEN_MCP_URL"] == "https://mcp.data.acme.com/mcp"
    nginx = _read(bundle, ".vecten/stack/nginx/conf.d/default.conf")
    assert "server_name overview.data.acme.com;" in nginx
    assert "set $upstream http://svc-acme-app-overview:8501;" in nginx


def test_a_bundle_whose_apps_run_nowhere_renders_the_stack_of_a_project_without_apps(
    project, tmp_path
):
    from .services_fixtures import locked

    _with_apps(project, {}, declared={})
    locked(project)
    files = (
        ".vecten/ship/host.json",
        ".vecten/stack/docker-compose.yml",
        ".vecten/stack/nginx/conf.d/default.conf",
    )
    before = {name: _read(_bundle(project, tmp_path / "a"), name) for name in files}
    _with_apps(
        project, {"finance": {"groups": ["finance"]}}, {"overview": OVERVIEW, "finance": FINANCE}
    )
    locked(project)
    after = {name: _read(_bundle(project, tmp_path / "b"), name) for name in files}
    assert after == before


def test_a_record_naming_an_app_credential_reaches_no_component(tmp_path):
    from vecten_shared.output import ExitCode, Failure

    stack_dir = stack(tmp_path, secrets=("APP_OVERVIEW_CLIENT_SECRET",))
    for bound in (None, BOUND):
        with pytest.raises(Failure) as caught:
            generate_compose(config(bound), "local", ["platform", "mcp"], stack_dir)
        assert caught.value.code == ExitCode.CONFIG
        assert "APP_OVERVIEW_CLIENT_SECRET" in caught.value.message
