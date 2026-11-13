import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from vecten.stack import compose as compose_mod
from vecten.stack.compose import (
    STACK_DIR,
    compose_project_name,
    generate_compose,
    local_hostnames,
    vm_hostnames,
    write_compose,
)
from vecten.stack.env import ensure_local_env
from vecten_shared.config import ConnectorDecl, VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs
from vecten_shared.provider.local import LOCAL_PG_PORT_ENV, LOCAL_PG_PUBLISHED_PORT
from vecten_shared.versions import VERSIONS

DEPLOY = {
    "cloud": "aws",
    "region": "eu-central-1",
    "domain": "data.acme.example",
    "dns_zone": "acme.example",
    "github_repository": "acme/platform",
}
VM_HOSTS = {"airflow": "airflow.data.acme.example", "mcp": "mcp.data.acme.example"}


def cfg(components, warehouse="postgres", profile="standard", deploy=None):
    raw = {
        "users": 5,
        "components": components,
        "profile": profile,
        "project": {"name": "acme", "scaffold_version": "0.1.0"},
        "warehouse": {"kind": warehouse},
    }
    if deploy:
        raw["deploy"] = deploy
    return VectenConfig.model_validate(raw)


EXPECTED_PROFILES = {
    "postgres": ["local"],
    "airflow-api-server": ["platform"],
    "airflow-scheduler": ["platform"],
    "airflow-dag-processor": ["platform"],
    "airflow-triggerer": ["platform"],
    "log-cleaner": ["platform"],
    "mcp-server": ["mcp"],
    "nginx": ["platform", "mcp", "local"],
}


def test_services_and_profiles_are_exactly_the_spec(tmp_path):
    doc = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    assert {name: svc["profiles"] for name, svc in doc["services"].items()} == EXPECTED_PROFILES


def test_local_mcp_reads_build_evidence_only_with_platform(tmp_path):
    for components in (["platform", "mcp"], ["mcp"]):
        doc = generate_compose(cfg(components), "local", components, tmp_path)
        mounts = doc["services"]["mcp-server"]["volumes"]
        assert ("../artifacts:/opt/airflow/artifacts:ro" in mounts) == ("platform" in components)


def _outputs(hosts: dict) -> EnvironmentOutputs:
    return EnvironmentOutputs.model_validate(
        {
            "environment": "staging",
            "region": "eu-central-1",
            "hosts": hosts,
            "setup_host": next(iter(hosts)),
            "bucket": "b",
            "secret_prefix": "vecten/acme/staging/",
            "postgres": {},
        }
    )


def _host(components, names):
    return {
        "instance_id": "i-0",
        "public_ip": "1.2.3.4",
        "components": components,
        "hostnames": names,
    }


def test_vm_hostnames_come_from_every_host_in_the_outputs():
    standard = _outputs(
        {
            "platform": _host(["platform"], ["airflow.stg.acme.example"]),
            "mcp": _host(["mcp"], ["mcp.stg.acme.example"]),
        }
    )
    assert vm_hostnames(standard) == {
        "airflow": "airflow.stg.acme.example",
        "mcp": "mcp.stg.acme.example",
    }
    minimal = _outputs(
        {"main": _host(["platform", "mcp"], ["airflow.stg.acme.example", "mcp.stg.acme.example"])}
    )
    assert vm_hostnames(minimal) == vm_hostnames(standard)
    assert local_hostnames()["mcp"] == "mcp.localhost:8443"


def test_vm_hostnames_refuse_unpaired_components():
    broken = _outputs({"main": _host(["platform", "mcp"], ["airflow.stg.acme.example"])})
    with pytest.raises(Failure) as exc:
        vm_hostnames(broken)
    assert exc.value.code == ExitCode.INTERNAL


def test_vm_stack_needs_the_hostnames_from_the_outputs(tmp_path):
    with pytest.raises(Failure) as exc:
        generate_compose(cfg(["platform", "mcp"], deploy=DEPLOY), "vm", ["mcp"], tmp_path)
    assert exc.value.code == ExitCode.INTERNAL


def test_vm_mode_has_no_postgres_and_only_host_components(tmp_path):
    doc = generate_compose(
        cfg(["platform", "mcp"], deploy=DEPLOY), "vm", ["mcp"], tmp_path, hosts=VM_HOSTS
    )
    assert set(doc["services"]) == {"mcp-server", "nginx"}
    assert doc["services"]["mcp-server"]["environment"]["VECTEN_MCP_PUBLIC_URL"] == (
        "https://mcp.data.acme.example"
    )
    doc = generate_compose(
        cfg(["platform", "mcp"], profile="minimal", deploy=DEPLOY),
        "vm",
        ["platform", "mcp"],
        tmp_path,
        hosts=VM_HOSTS,
    )
    assert "postgres" not in doc["services"] and "airflow-scheduler" in doc["services"]


def test_images_come_from_the_versions_table(tmp_path):
    doc = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    assert doc["services"]["postgres"]["image"] == f"postgres:{VERSIONS.postgres_major}"
    assert doc["services"]["nginx"]["image"] == VERSIONS.nginx_image
    assert doc["services"]["log-cleaner"]["image"] == VERSIONS.log_cleaner_image
    assert doc["services"]["airflow-api-server"]["build"]["dockerfile"] == "airflow.Dockerfile"
    assert doc["services"]["mcp-server"]["build"]["dockerfile"] == "mcp.Dockerfile"


@pytest.mark.parametrize("mode", ["local", "vm"])
def test_only_the_api_server_builds_the_airflow_image(tmp_path, mode):
    if mode == "local":
        doc = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    else:
        doc = _vm(["platform"])
    builder = doc["services"]["airflow-api-server"]
    assert builder["build"]["dockerfile"] == "airflow.Dockerfile"
    for name in ("airflow-scheduler", "airflow-dag-processor", "airflow-triggerer"):
        service = doc["services"][name]
        assert "build" not in service, name
        assert service["image"] == builder["image"]
        assert service["pull_policy"] == "never"


def test_airflow_env_is_three_x_local_executor_and_hostname_base_url(tmp_path):
    doc = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    env = doc["services"]["airflow-scheduler"]["environment"]
    assert env["AIRFLOW__CORE__EXECUTOR"] == "LocalExecutor"
    assert env["AIRFLOW__API__BASE_URL"] == "https://airflow.localhost:8443"
    assert "AIRFLOW__API__ENABLE_PROXY_FIX" not in env
    assert env["FORWARDED_ALLOW_IPS"] == "*"
    assert env["AIRFLOW__API__SECRET_KEY"] == "${AIRFLOW_API_SECRET_KEY}"
    assert env["AIRFLOW__API_AUTH__JWT_SECRET"] == "${AIRFLOW_JWT_SECRET}"
    assert env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"].startswith(
        "postgresql+psycopg2://airflow:${AIRFLOW_DB_PASSWORD}@postgres/airflow"
    )
    command = doc["services"]["airflow-api-server"]["command"][-1].strip()
    assert command == "airflow db migrate && exec airflow api-server --proxy-headers"


def test_postgres_init_and_log_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv(LOCAL_PG_PORT_ENV, raising=False)
    doc = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    pg = doc["services"]["postgres"]
    assert any(v.endswith(":/docker-entrypoint-initdb.d:ro") for v in pg["volumes"])
    assert pg["healthcheck"]["test"] == ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
    assert "log_line_prefix=%m [%p] %u@%d app=%a " in pg["command"]
    assert pg["environment"]["VECTEN_WITH_MCP"] == "true"
    assert "VECTEN_WITH_PLATFORM" not in pg["environment"]
    assert pg["ports"] == [f"127.0.0.1:{LOCAL_PG_PUBLISHED_PORT}:5432"] == ["127.0.0.1:55432:5432"]
    assert not hasattr(compose_mod, "LOCAL_PG_PORT")
    write_compose(tmp_path, cfg(["platform", "mcp"]))
    init = tmp_path / STACK_DIR / "pg-init"
    script = init / "10-vecten.sh"
    assert script.is_file() and os.stat(script).st_mode & stat.S_IXUSR
    assert (init / "vecten" / "warehouse_setup.sql").is_file()
    assert not list(init.glob("*.sql"))


def test_postgres_publishes_the_port_the_environment_names(tmp_path, monkeypatch):
    monkeypatch.setenv(LOCAL_PG_PORT_ENV, "23456")
    doc = generate_compose(cfg(["platform"]), "local", ["platform"], tmp_path)
    assert doc["services"]["postgres"]["ports"] == ["127.0.0.1:23456:5432"]


def test_airflow_api_is_published_on_loopback_in_the_local_stack_only(tmp_path):
    local = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    assert compose_mod.LOCAL_AIRFLOW_API_PORT == 8482
    assert local["services"]["airflow-api-server"]["ports"] == ["127.0.0.1:8482:8080"]
    for name in AIRFLOW_SERVICES[1:]:
        assert "ports" not in local["services"][name], name
    vm = generate_compose(
        cfg(["platform", "mcp"], deploy=DEPLOY), "vm", ["platform", "mcp"], tmp_path, hosts=VM_HOSTS
    )
    assert "ports" not in vm["services"]["airflow-api-server"]


@pytest.mark.parametrize(
    ("mode", "setting", "expected"),
    [
        # The laptop starts DAGs paused whatever the setting says.
        ("local", None, "True"),
        ("local", False, "True"),
        ("local", True, "True"),
        # A VM schedules new DAGs unless its environment opts in to pausing them.
        ("vm", None, "False"),
        ("vm", False, "False"),
        ("vm", True, "True"),
    ],
)
def test_dags_start_paused_on_the_laptop_and_scheduled_on_a_vm(tmp_path, mode, setting, expected):
    kwargs = {} if setting is None else {"dags_paused_at_creation": setting}
    if mode == "vm":
        config = cfg(["platform", "mcp"], deploy=DEPLOY)
        kwargs["hosts"] = VM_HOSTS
    else:
        config = cfg(["platform", "mcp"])
    doc = generate_compose(config, mode, ["platform", "mcp"], tmp_path, **kwargs)
    for name in AIRFLOW_SERVICES:
        environment = doc["services"][name]["environment"]
        assert environment["AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION"] == expected, name


@pytest.mark.parametrize("hosts", [["platform", "mcp"], ["platform"], ["mcp"]])
def test_every_vm_host_runs_with_its_environments_snowflake_account(tmp_path, hosts):
    config = cfg(["platform", "mcp"], warehouse="snowflake", deploy=DEPLOY)
    doc = generate_compose(
        config, "vm", hosts, tmp_path, hosts=VM_HOSTS, snowflake_account="ACME-DEV"
    )
    found = [name for name in (*AIRFLOW_SERVICES, "mcp-server") if name in doc["services"]]
    assert found
    for name in found:
        environment = doc["services"][name]["environment"]
        assert environment["VECTEN_SNOWFLAKE_ACCOUNT"] == "ACME-DEV", name


@pytest.mark.parametrize(
    ("mode", "warehouse"), [("local", "snowflake"), ("vm", "postgres"), ("local", "postgres")]
)
def test_only_a_snowflake_vm_stack_carries_the_account(tmp_path, mode, warehouse):
    deploy = DEPLOY if mode == "vm" else None
    config = cfg(["platform", "mcp"], warehouse=warehouse, deploy=deploy)
    kwargs = {"hosts": VM_HOSTS} if mode == "vm" else {}
    plain = generate_compose(config, mode, ["platform", "mcp"], tmp_path, **kwargs)
    given = generate_compose(
        config, mode, ["platform", "mcp"], tmp_path, snowflake_account="ACME-DEV", **kwargs
    )
    assert given == plain
    assert "VECTEN_SNOWFLAKE_ACCOUNT" not in yaml.safe_dump(given)


def test_mcp_only_local(tmp_path):
    doc = generate_compose(cfg(["mcp"]), "local", ["mcp"], tmp_path)
    assert set(doc["services"]) == {"postgres", "mcp-server", "nginx"}
    assert doc["services"]["postgres"]["environment"]["VECTEN_WITH_MCP"] == "true"


def test_platform_only_local_turns_mcp_off(tmp_path):
    doc = generate_compose(cfg(["platform"]), "local", ["platform"], tmp_path)
    assert doc["services"]["postgres"]["environment"]["VECTEN_WITH_MCP"] == "false"


def test_write_compose_lists_override(tmp_path):
    (tmp_path / "orchestration").mkdir()
    (tmp_path / "orchestration" / "docker-compose.override.yml").write_text("services: {}\n")
    path = write_compose(tmp_path, cfg(["platform", "mcp"]))
    assert path == tmp_path / STACK_DIR / "docker-compose.yml"
    files = (tmp_path / STACK_DIR / "compose.project").read_text().split()
    assert files == [str(path), str(tmp_path / "orchestration" / "docker-compose.override.yml")]
    yaml.safe_load(path.read_text())


def test_write_compose_mounts_empty_dirs_for_absent_project_dirs(tmp_path):
    (tmp_path / "dbt").mkdir()
    path = write_compose(tmp_path, cfg(["platform", "mcp"]))
    volumes = yaml.safe_load(path.read_text())["services"]["airflow-scheduler"]["volumes"]
    assert "../../dbt:/opt/airflow/dbt" in volumes
    assert "./empty/connectors:/opt/airflow/connectors" in volumes
    assert "./empty/dags:/opt/airflow/dags/project" in volumes
    assert (tmp_path / STACK_DIR / "empty" / "connectors").is_dir()
    assert (tmp_path / STACK_DIR / "empty" / "dags").is_dir()


def test_ensure_local_env_generates_missing_and_keeps_existing(tmp_path):
    (tmp_path / ".env").write_text("LOADER_PASSWORD=keepme\n")
    values = ensure_local_env(tmp_path, cfg(["platform", "mcp"]))
    assert values["LOADER_PASSWORD"] == "keepme"
    assert values["AIRFLOW_ADMIN_USER"] == "admin"
    # Airflow's own login is the only one; NGINX asks for no password.
    assert "AIRFLOW_BASIC_AUTH_USER" not in values
    assert "AIRFLOW_BASIC_AUTH_PASSWORD" not in values
    for name in (
        "AIRFLOW_DB_PASSWORD",
        "POSTGRES_MASTER_PASSWORD",
        "AIRFLOW_FERNET_KEY",
        "AIRFLOW_API_SECRET_KEY",
        "AIRFLOW_JWT_SECRET",
    ):
        assert values[name]
    assert values["AIRFLOW_JWT_SECRET"] != values["AIRFLOW_API_SECRET_KEY"]
    assert values == ensure_local_env(tmp_path, cfg(["platform", "mcp"]))
    assert dotenv_values(tmp_path / ".env", interpolate=False) == values


@pytest.mark.skipif(not shutil.which("docker"), reason="docker CLI missing")
def test_compose_config_validates(tmp_path):
    (tmp_path / "orchestration").mkdir()
    (tmp_path / "orchestration" / "docker-compose.override.yml").write_text(
        yaml.safe_dump({"services": {"probe": {"image": "alpine:3.21", "command": ["true"]}}})
    )
    (tmp_path / ".env").write_text("")
    path = write_compose(tmp_path, cfg(["platform", "mcp"]))
    override = tmp_path / "orchestration" / "docker-compose.override.yml"
    r = subprocess.run(
        ["docker", "compose", "--project-directory", str(path.parent)]
        + ["--env-file", str(tmp_path / ".env"), "-f", str(path)]
        + ["-f", str(override), "--profile", "platform", "--profile", "mcp"]
        + ["--profile", "local", "config", "--format", "json"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    services = json.loads(r.stdout)["services"]
    assert services["probe"]["image"] == "alpine:3.21"
    assert {"postgres", "airflow-api-server", "mcp-server", "nginx"} <= set(services)


AIRFLOW_SERVICES = (
    "airflow-api-server",
    "airflow-scheduler",
    "airflow-dag-processor",
    "airflow-triggerer",
)


def test_airflow_runs_as_the_host_uid_locally_only(tmp_path):
    local = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    for name in AIRFLOW_SERVICES:
        assert local["services"][name]["user"] == f"{os.getuid()}:0", name
    vm = generate_compose(
        cfg(["platform", "mcp"], deploy=DEPLOY), "vm", ["platform", "mcp"], tmp_path, hosts=VM_HOSTS
    )
    for name in AIRFLOW_SERVICES:
        assert "user" not in vm["services"][name], name


@pytest.mark.parametrize(
    ("mode", "artifacts"),
    [("local", "../artifacts"), ("vm", "airflow-artifacts")],
)
def test_airflow_reads_the_project_config_and_osi_and_keeps_artifacts(tmp_path, mode, artifacts):
    config = cfg(["platform", "mcp"], deploy=DEPLOY)
    doc = generate_compose(config, mode, ["platform", "mcp"], tmp_path / STACK_DIR, hosts=VM_HOSTS)
    # On the laptop `vecten issues` reads each run's dbt results from `.vecten/artifacts`.
    assert ("airflow-artifacts" in doc["volumes"]) == (mode == "vm")
    for name in AIRFLOW_SERVICES:
        service = doc["services"][name]
        assert "../../vecten.toml:/opt/airflow/vecten.toml:ro" in service["volumes"], name
        assert "../../osi:/opt/airflow/osi:ro" in service["volumes"], name
        assert f"{artifacts}:/opt/airflow/artifacts" in service["volumes"], name
        assert service["environment"]["VECTEN_ARTIFACT_DIR"] == "/opt/airflow/artifacts", name
    assert f"{artifacts}:/opt/airflow/artifacts" not in doc["services"]["mcp-server"]["volumes"]


def test_write_compose_creates_the_laptop_artifacts_directory(tmp_path):
    config = cfg(["platform"])
    write_compose(tmp_path, config)
    assert (tmp_path / ".vecten" / "artifacts").is_dir()


def test_compose_project_name_is_unique_per_checkout(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    config = cfg(["platform", "mcp"])
    name_a = compose_project_name(a, config)
    digest = hashlib.sha256(str(a.resolve()).encode()).hexdigest()[:8]
    assert name_a == f"vecten-acme-{digest}"
    assert name_a != compose_project_name(b, config)
    doc = generate_compose(config, "local", ["platform", "mcp"], a / STACK_DIR)
    assert doc["name"] == name_a


def test_project_layer_images_are_tagged_per_checkout(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    config = cfg(["platform", "mcp"])
    tags = {}
    for root in (a, b):
        doc = generate_compose(config, "local", ["platform", "mcp"], root / STACK_DIR)
        name = compose_project_name(root, config)
        assert doc["services"]["airflow-scheduler"]["image"] == (
            f"{name}-airflow:{VERSIONS.vecten}"
        )
        assert doc["services"]["mcp-server"]["image"] == f"{name}-mcp:{VERSIONS.vecten}"
        tags[root] = doc["services"]["mcp-server"]["image"]
    assert tags[a] != tags[b]


def _with_connectors(connectors, deploy=None):
    config = cfg(["platform", "mcp"], deploy=deploy)
    return config.model_copy(
        update={"connectors": [ConnectorDecl.model_validate(c) for c in connectors]}
    )


def test_airflow_services_receive_loader_password_runtime_vars_and_connector_secrets(tmp_path):
    config = _with_connectors(
        [
            {"name": "hubspot", "source": "dlt", "secrets": ["HUBSPOT_API_KEY"]},
            {
                "name": "billing",
                "source": "rest",
                "secrets": ["BILLING_API_KEY", "HUBSPOT_API_KEY"],
            },
        ]
    )
    doc = generate_compose(config, "local", ["platform", "mcp"], tmp_path / ".vecten" / "stack")
    env = doc["services"]["airflow-scheduler"]["environment"]
    assert env["LOADER_PASSWORD"] == "${LOADER_PASSWORD}"
    assert env["ALERT_WEBHOOK_URL"] == "${ALERT_WEBHOOK_URL:-}"
    assert env["VECTEN_PROJECT_ROOT"] == "/opt/airflow" and env["VECTEN_INSIDE_COMPOSE"] == "1"
    assert env["VECTEN_DLT_DIR"] == "/tmp/vecten-dlt"
    assert env["DLT_DATA_DIR"] == "/tmp/vecten-dlt/data"
    assert env["RUNTIME__DLTHUB_TELEMETRY"] == "false"
    assert env["HUBSPOT_API_KEY"] == "${HUBSPOT_API_KEY:-}"
    assert env["BILLING_API_KEY"] == "${BILLING_API_KEY:-}"
    assert [k for k in env if k.endswith("_API_KEY")] == ["BILLING_API_KEY", "HUBSPOT_API_KEY"]
    assert "LOADER_PASSWORD" not in doc["services"]["mcp-server"]["environment"]
    assert "HUBSPOT_API_KEY" not in doc["services"]["nginx"].get("environment", {})


def test_vm_airflow_services_receive_the_same_runtime_block(tmp_path):
    config = _with_connectors(
        [{"name": "hubspot", "source": "dlt", "secrets": ["HUBSPOT_API_KEY"]}], deploy=DEPLOY
    )
    doc = generate_compose(
        config, "vm", ["platform"], tmp_path / ".vecten" / "stack", hosts=VM_HOSTS
    )
    env = doc["services"]["airflow-api-server"]["environment"]
    assert env["LOADER_PASSWORD"] == "${LOADER_PASSWORD}"
    assert env["HUBSPOT_API_KEY"] == "${HUBSPOT_API_KEY:-}"


@pytest.mark.parametrize(
    "secret",
    [
        "AIRFLOW__CORE__FERNET_KEY",
        "AIRFLOW__API__OTHER",
        "LOADER_PASSWORD",
        "VECTEN_ENV",
        "KEY}:-x",
        "1KEY",
    ],
)
def test_connector_secret_cannot_shadow_a_stack_variable(tmp_path, secret):
    # model_construct skips the config validators so compose's own guard is what refuses.
    decl = ConnectorDecl.model_construct(name="x", source="rest", secrets=[secret])
    config = cfg(["platform", "mcp"]).model_copy(update={"connectors": [decl]})
    with pytest.raises(Failure) as exc:
        generate_compose(config, "local", ["platform"], tmp_path / ".vecten" / "stack")
    assert exc.value.code == ExitCode.CONFIG and secret in str(exc.value)


def _vm(host_components, profile="standard"):
    config = cfg(["platform", "mcp"], profile=profile, deploy=DEPLOY)
    return generate_compose(
        config, "vm", host_components, Path("/build") / STACK_DIR, hosts=VM_HOSTS
    )


ENV_FILE_MOUNT = "../environments/${VECTEN_ENV}.json:/opt/vecten-env/environment.json:ro"


def test_vm_platform_host_document():
    doc = _vm(["platform"])
    assert "postgres" not in doc["services"] and "mcp-server" not in doc["services"]
    assert doc["services"]["nginx"]["ports"] == ["443:443", "80:80"]
    assert not any("htpasswd" in v for v in doc["services"]["nginx"]["volumes"])
    for name in AIRFLOW_SERVICES:
        service = doc["services"][name]
        env = service["environment"]
        assert (
            "/run/vecten/airflow_passwords.json:"
            "/opt/airflow/simple_auth_manager_passwords.json.generated"
        ) in service["volumes"], name
        assert not any(v.startswith("./airflow_passwords.json") for v in service["volumes"])
        assert ENV_FILE_MOUNT in service["volumes"], name
        assert env["VECTEN_ENVIRONMENT_FILE"] == "/opt/vecten-env/environment.json"
        assert env["AWS_REGION"] == "${AWS_REGION}"
        assert env["AWS_DEFAULT_REGION"] == "${AWS_REGION}"
        assert env["VECTEN_BUNDLE_COMMIT"] == "${VECTEN_BUNDLE_COMMIT}"
        assert env["VECTEN_ARTIFACT_URL"] == "${VECTEN_ARTIFACT_URL}"
        assert env["AIRFLOW__LOGGING__REMOTE_LOGGING"] == "True"
        assert env["AIRFLOW__LOGGING__REMOTE_BASE_LOG_FOLDER"] == (
            "${AIRFLOW_REMOTE_BASE_LOG_FOLDER}"
        )
        assert env["AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID"] == "aws_default"
        assert env["AIRFLOW__CORE__PARALLELISM"] == "${AIRFLOW_PARALLELISM}"
        assert env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == "${AIRFLOW_DATABASE_URL}"
        assert "user" not in service
    # The bundle is rendered on another machine, so the build context is relative.
    assert doc["services"]["airflow-api-server"]["build"]["context"] == "./build"


def test_vm_mcp_service_carries_the_pool_cap_and_environment_file():
    doc = _vm(["mcp"])
    mcp = doc["services"]["mcp-server"]
    assert mcp["environment"]["VECTEN_MCP_POOL_CAP"] == "${VECTEN_MCP_POOL_CAP}"
    assert mcp["environment"]["VECTEN_ENVIRONMENT_FILE"] == "/opt/vecten-env/environment.json"
    assert mcp["environment"]["AWS_REGION"] == "${AWS_REGION}"
    assert ENV_FILE_MOUNT in mcp["volumes"]
    assert mcp["build"]["context"] == "./build"
    assert not any("htpasswd" in v for v in doc["services"]["nginx"]["volumes"])


def test_vm_main_host_document_has_both_components():
    doc = _vm(["platform", "mcp"], profile="minimal")
    assert {"airflow-api-server", "mcp-server", "nginx"} <= set(doc["services"])
    assert "postgres" not in doc["services"]
    assert "VECTEN_MCP_POOL_CAP" not in doc["services"]["airflow-scheduler"]["environment"]


SCALEWAY_DEPLOY = {**DEPLOY, "cloud": "scaleway", "region": "fr-par"}
SCALEWAY_RUNTIME = (
    "SCW_ACCESS_KEY",
    "SCW_SECRET_KEY",
    "SCW_DEFAULT_REGION",
    "SCW_DEFAULT_PROJECT_ID",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ENDPOINT_URL_S3",
)


def _scaleway_vm(host_components, profile="standard"):
    config = cfg(["platform", "mcp"], profile=profile, deploy=SCALEWAY_DEPLOY)
    return generate_compose(
        config, "vm", host_components, Path("/build") / STACK_DIR, hosts=VM_HOSTS
    )


def test_scaleway_vm_services_take_the_key_and_endpoint_from_the_agent():
    doc = _scaleway_vm(["platform", "mcp"], profile="minimal")
    for name in (*AIRFLOW_SERVICES, "mcp-server"):
        env = doc["services"][name]["environment"]
        assert env["VECTEN_PROVIDER"] == "${VECTEN_PROVIDER}", name
        assert env["AWS_REGION"] == "${AWS_REGION}", name
        for var in SCALEWAY_RUNTIME:
            assert env[var] == f"${{{var}}}", (name, var)
    for name in AIRFLOW_SERVICES:
        env = doc["services"][name]["environment"]
        assert env["AIRFLOW_CONN_AWS_DEFAULT"] == "${AIRFLOW_CONN_AWS_DEFAULT}"
        assert env["AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID"] == "aws_default"
    assert "AIRFLOW_CONN_AWS_DEFAULT" not in doc["services"]["mcp-server"]["environment"]


def test_a_scaleway_vm_document_holds_no_literal_credential():
    doc = _scaleway_vm(["platform", "mcp"], profile="minimal")
    for service in doc["services"].values():
        for var, value in service.get("environment", {}).items():
            if var.startswith(("SCW_", "AWS_ACCESS", "AWS_SECRET", "AIRFLOW_CONN_")):
                assert value == f"${{{var}}}", var


def test_an_aws_vm_document_carries_no_scaleway_variable():
    doc = _vm(["platform", "mcp"], profile="minimal")
    for service in doc["services"].values():
        env = service.get("environment", {})
        assert not set(SCALEWAY_RUNTIME) & set(env)
        assert "AIRFLOW_CONN_AWS_DEFAULT" not in env


def test_local_mode_has_none_of_the_vm_additions(tmp_path):
    doc = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    for name in ("airflow-scheduler", "mcp-server"):
        env = doc["services"][name]["environment"]
        assert not {"AWS_REGION", "VECTEN_ENVIRONMENT_FILE", "VECTEN_ARTIFACT_URL"} & set(env)
    assert not any("htpasswd" in v for v in doc["services"]["nginx"]["volumes"])


def test_local_mcp_issuer_uses_one_port(tmp_path):
    from urllib.parse import urlparse

    doc = generate_compose(cfg(["platform", "mcp"]), "local", ["platform", "mcp"], tmp_path)
    issuer = doc["services"]["mcp-server"]["environment"]["VECTEN_MCP_PUBLIC_URL"]
    assert urlparse(issuer).port == compose_mod.LOCAL_HTTPS_PORT
    assert issuer == f"https://{local_hostnames()['mcp']}"
