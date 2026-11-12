import importlib.util
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from vecten.deploy.aws.ship import component_url
from vecten.deploy.bundle import build_host_bundle
from vecten.deploy.vm import apply as agent
from vecten_shared.config import load_config, save_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs
from vecten_shared.sql import setup_sql_path
from vecten_shared.versions import VERSIONS

from .deploy_fixtures import (
    COMMIT,
    STANDARD_OUTPUTS,
    add_connector,
    minimal_outputs,
    render_aws,
    set_facts,
)


@pytest.fixture
def project(tmp_path):
    root = render_aws(tmp_path / "proj")
    add_connector(root, "hubspot", secrets=("HUBSPOT_API_KEY",))
    (root / "dbt" / "models" / "marts" / "orders.sql").write_text("select 1\n")
    (root / "dbt" / "target").mkdir()
    (root / "dbt" / "target" / "manifest.json").write_text("{}")
    (root / "connectors" / "hubspot" / "__pycache__").mkdir()
    (root / "connectors" / "hubspot" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (root / ".env").write_text("HUBSPOT_API_KEY=hunter2\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".vecten" / "records").mkdir(parents=True, exist_ok=True)
    (root / ".vecten" / "records" / "up.json").write_text("{}")
    return root


def build(root, out_dir, host="platform", outputs=None, commit=COMMIT):
    config = load_config(root)
    outputs = EnvironmentOutputs.model_validate(outputs or STANDARD_OUTPUTS)
    return build_host_bundle(root, config, "prod", outputs, host, commit, out_dir)


def members(path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def read(path: Path, name: str) -> str:
    with tarfile.open(path, "r:gz") as tar:
        return tar.extractfile(name).read().decode("utf-8")


def test_build_lineage_uses_only_git_proven_ancestors(project, monkeypatch):
    from types import SimpleNamespace

    from vecten.deploy.bundle import build_lineage

    calls = []
    older = "b" * 40

    def run(args, root):
        calls.append((args, root))
        return SimpleNamespace(returncode=0, stdout=f"{COMMIT}\n{older}\n")

    monkeypatch.setattr("vecten.deploy.bundle.get_git", lambda: SimpleNamespace(run=run))
    assert build_lineage(project, COMMIT) == {"commit": COMMIT, "ancestors": [older]}
    assert calls == [(["rev-list", COMMIT], project)]


def test_bundle_holds_the_content_and_the_host_stack(project, tmp_path):
    bundle = build(project, tmp_path / "out")
    names = members(bundle)
    assert json.loads(read(bundle, ".vecten/ship/build-lineage.json")) == {
        "commit": COMMIT,
        "ancestors": [],
    }
    for expected in (
        "vecten.toml",
        "orchestration/dags/dbt_build.py",
        "connectors/hubspot/__init__.py",
        "dbt/dbt_project.yml",
        "dbt/models/marts/orders.sql",
        "osi/metrics.json",
        "evals/semantic.yaml",
        ".vecten/stack/docker-compose.yml",
        ".vecten/stack/nginx/conf.d/default.conf",
        ".vecten/stack/build/airflow.Dockerfile",
        ".vecten/stack/build/mcp.Dockerfile",
        ".vecten/stack/build/requirements.txt",
        ".vecten/environments/prod.json",
        ".vecten/ship/host.json",
        ".vecten/ship/apply.py",
        ".vecten/ship/warehouse_setup.sql",
    ):
        assert expected in names, expected


def test_bundle_leaves_out_secrets_history_records_and_build_output(project, tmp_path):
    names = members(build(project, tmp_path / "out"))
    assert ".env" not in names
    assert not [n for n in names if n == ".git" or n.startswith(".git/")]
    assert not [n for n in names if n.startswith(".vecten/records")]
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert not [n for n in names if n.startswith("dbt/target")]
    assert not [n for n in names if n.startswith("deploy/") or n.startswith("docs/")]
    bundle = build(project, tmp_path / "o2")
    texts = [read(bundle, n) for n, m in names.items() if m.isfile()]
    assert not [t for t in texts if "hunter2" in t]


def test_bundle_ships_the_project_packages_and_not_the_generated_ones(project, tmp_path):
    listed = "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n"
    (project / "dbt" / "project-packages.yml").write_text(listed)
    (project / "dbt" / "packages.yml").write_text("packages:\n  - local: /laptop/path\n")
    (project / "dbt" / "package-lock.yml").write_text("packages: []\n")
    bundle = build(project, tmp_path / "out")
    names = members(bundle)
    assert read(bundle, "dbt/project-packages.yml") == listed
    assert "dbt/packages.yml" not in names and "dbt/package-lock.yml" not in names


def test_compose_file_is_for_this_host_only(project, tmp_path):
    platform = yaml.safe_load(
        read(build(project, tmp_path / "a"), ".vecten/stack/docker-compose.yml")
    )
    assert "airflow-api-server" in platform["services"]
    assert "mcp-server" not in platform["services"]
    assert "postgres" not in platform["services"]
    assert platform["services"]["airflow-api-server"]["build"]["context"] == "./build"
    mcp = yaml.safe_load(
        read(build(project, tmp_path / "b", host="mcp"), ".vecten/stack/docker-compose.yml")
    )
    assert set(mcp["services"]) == {"mcp-server", "nginx"}


def test_dockerfiles_build_on_the_published_base_images(project, tmp_path):
    bundle = build(project, tmp_path / "out")
    text = read(bundle, ".vecten/stack/build/airflow.Dockerfile")
    assert text.startswith(f"FROM ghcr.io/vecten/vecten-airflow:{VERSIONS.vecten}\n")


def test_nginx_conf_uses_letsencrypt_for_the_host_names(project, tmp_path):
    conf = read(build(project, tmp_path / "out"), ".vecten/stack/nginx/conf.d/default.conf")
    assert "server_name airflow.data.acme.com;" in conf
    assert "/etc/letsencrypt/live/airflow.data.acme.com/fullchain.pem" in conf
    assert "mcp.data.acme.com" not in conf


PAUSED = "AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION"


def test_shipped_dags_start_paused_only_when_the_tfvars_opt_in(project, tmp_path):
    def paused(out):
        doc = yaml.safe_load(read(build(project, out), ".vecten/stack/docker-compose.yml"))
        return doc["services"]["airflow-scheduler"]["environment"][PAUSED]

    assert paused(tmp_path / "default") == "False"
    tfvars = project / "deploy" / "envs" / "prod.tfvars"
    rendered = tfvars.read_text()
    tfvars.write_text(rendered + "dags_paused_at_creation = true\n")
    assert paused(tmp_path / "opted-in") == "True"
    tfvars.write_text(rendered + "dags_paused_at_creation = false\n")
    assert paused(tmp_path / "opted-out") == "False"


def test_shipped_nginx_carries_the_airflow_allow_list(project, tmp_path):
    path = ".vecten/stack/nginx/conf.d/default.conf"
    assert "deny all;" not in read(build(project, tmp_path / "open"), path)
    config = load_config(project)
    config.airflow.allow = ["203.0.113.0/24"]
    save_config(project, config)
    conf = read(build(project, tmp_path / "narrowed"), path)
    assert "allow 203.0.113.0/24;" in conf and "deny all;" in conf
    assert "location = /api/v2/monitor/health {" in conf


def test_host_json_satisfies_the_agent_contract(project, tmp_path):
    host = json.loads(read(build(project, tmp_path / "out"), ".vecten/ship/host.json"))
    assert set(host) == set(agent.HOST_KEYS)
    assert host["host"] == "platform"
    assert host["components"] == ["platform"]
    assert host["env"] == "prod"
    assert host["project"] == "acme"
    assert host["region"] == "eu-central-1"
    assert host["commit"] == COMMIT
    assert host["setup_host"] is False
    assert host["hostnames"] == ["airflow.data.acme.com"]
    assert host["secret_prefix"] == "vecten/acme/prod/"
    assert host["compose_project"] == "vecten-acme-prod"
    assert host["connector_secrets"] == ["HUBSPOT_API_KEY"]
    assert host["compose_plugin_version"] == VERSIONS.docker_compose_plugin
    assert host["buildx_plugin_version"] == VERSIONS.docker_buildx_plugin
    assert host["certbot_image"] == VERSIONS.certbot_image
    assert host["postgres_image"] == f"postgres:{VERSIONS.postgres_major}"
    # The platform VM is small unless tfvars sets platform_size.
    assert host["airflow_parallelism"] == 4
    assert host["mcp_pool_cap"] == 3


def test_setup_host_is_flagged(project, tmp_path):
    host = json.loads(read(build(project, tmp_path / "out", host="mcp"), ".vecten/ship/host.json"))
    assert host["setup_host"] is True
    assert host["hostnames"] == ["mcp.data.acme.com"]


def test_tiers_follow_platform_size_and_size(project, tmp_path):
    tfvars = project / "deploy" / "envs" / "prod.tfvars"
    text = tfvars.read_text().replace('size              = "small"', 'size = "large"')
    tfvars.write_text(text + 'platform_size = "medium"\n')
    platform = json.loads(read(build(project, tmp_path / "a"), ".vecten/ship/host.json"))
    assert platform["airflow_parallelism"] == 16
    mcp = json.loads(read(build(project, tmp_path / "b", host="mcp"), ".vecten/ship/host.json"))
    assert mcp["mcp_pool_cap"] == 5 and mcp["airflow_parallelism"] == 32


def test_minimal_main_host_carries_both_components(tmp_path):
    root = render_aws(tmp_path / "proj", profile="minimal")
    bundle = build(root, tmp_path / "out", host="main", outputs=minimal_outputs())
    host = json.loads(read(bundle, ".vecten/ship/host.json"))
    assert host["components"] == ["platform", "mcp"]
    assert host["setup_host"] is True
    compose = yaml.safe_load(read(bundle, ".vecten/stack/docker-compose.yml"))
    assert {"airflow-api-server", "mcp-server", "nginx"} <= set(compose["services"])


def test_environment_file_and_agent_files_are_the_shipped_ones(project, tmp_path):
    bundle = build(project, tmp_path / "out")
    outputs = json.loads(read(bundle, ".vecten/environments/prod.json"))
    assert EnvironmentOutputs.model_validate(outputs) == EnvironmentOutputs.model_validate(
        STANDARD_OUTPUTS
    )
    assert read(bundle, ".vecten/ship/warehouse_setup.sql") == setup_sql_path().read_text()
    assert read(bundle, ".vecten/ship/apply.py") == Path(agent.__file__).read_text()


def test_two_builds_at_one_commit_are_byte_identical(project, tmp_path):
    first = build(project, tmp_path / "a")
    second = build(project, tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()
    for member in members(first).values():
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
        assert member.mtime == members(second)[member.name].mtime
    names = list(members(first))
    assert names == sorted(names)


def test_missing_mount_dirs_get_their_stand_ins(tmp_path):
    root = render_aws(tmp_path / "proj")
    for path in sorted((root / "connectors").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (root / "connectors").rmdir()
    bundle = build(root, tmp_path / "out")
    names = members(bundle)
    assert names[".vecten/stack/empty/connectors"].isdir()
    compose = yaml.safe_load(read(bundle, ".vecten/stack/docker-compose.yml"))
    volumes = compose["services"]["airflow-scheduler"]["volumes"]
    assert "./empty/connectors:/opt/airflow/connectors" in volumes
    assert "../../dbt:/opt/airflow/dbt" in volumes


def test_bundle_lands_in_out_dir_named_by_host_and_commit(project, tmp_path):
    bundle = build(project, tmp_path / "out")
    assert bundle == tmp_path / "out" / f"platform-{COMMIT}.tar.gz"


def test_unknown_host_is_refused(project, tmp_path):
    with pytest.raises(Failure) as exc:
        build(project, tmp_path / "out", host="nosuch")
    assert exc.value.code == ExitCode.INTERNAL
    assert "nosuch" in exc.value.message


def test_symlink_in_content_is_refused(project, tmp_path):
    (project / "dbt" / "models" / "link.sql").symlink_to(tmp_path / "elsewhere.sql")
    with pytest.raises(Failure) as exc:
        build(project, tmp_path / "out")
    assert exc.value.code == ExitCode.CONFIG
    assert "dbt/models/link.sql" in exc.value.message


def test_commit_must_be_a_git_object_name(project, tmp_path):
    with pytest.raises(Failure) as exc:
        build(project, tmp_path / "out", commit="main; rm -rf /")
    assert exc.value.code == ExitCode.INTERNAL


def test_agent_loads_the_staged_host_json(project, tmp_path):
    bundle = build(project, tmp_path / "out")
    release = tmp_path / "release"
    with tarfile.open(bundle, "r:gz") as tar:
        tar.extractall(release, filter="data")
    spec = importlib.util.spec_from_file_location(
        "shipped_agent", release / ".vecten" / "ship" / "apply.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    host = module.load_host(release)
    assert module.load_outputs(release, host["env"])["setup_host"] == "mcp"
    for empty in ("dags", "dbt", "connectors"):
        source = {"dags": "orchestration/dags", "dbt": "dbt", "connectors": "connectors"}[empty]
        assert (release / source).is_dir()


def test_dbt_members_are_group_writable_for_the_airflow_group(project, tmp_path):
    found = members(build(project, tmp_path / "out"))
    dbt = {n: m for n, m in found.items() if n == "dbt" or n.startswith("dbt/")}
    assert dbt["dbt"].isdir() and dbt["dbt"].mode == 0o775
    assert dbt["dbt/models/marts"].mode == 0o775
    assert dbt["dbt/dbt_project.yml"].mode == 0o664
    assert dbt["dbt/models/marts/orders.sql"].mode == 0o664
    assert all(m.gid == 0 for m in dbt.values())


def test_members_outside_dbt_keep_owner_only_writes(project, tmp_path):
    found = members(build(project, tmp_path / "out"))
    for name, member in found.items():
        if name == "dbt" or name.startswith("dbt/"):
            continue
        assert member.mode == (0o755 if member.isdir() else 0o644), name


def test_project_compose_override_is_shipped(project, tmp_path):
    override = project / "orchestration" / "docker-compose.override.yml"
    assert override.is_file()
    bundle = build(project, tmp_path / "out")
    assert read(bundle, "orchestration/docker-compose.override.yml") == override.read_text()


def test_bundle_without_an_override_has_none(project, tmp_path):
    (project / "orchestration" / "docker-compose.override.yml").unlink()
    names = members(build(project, tmp_path / "out"))
    assert "orchestration/docker-compose.override.yml" not in names


def staging_outputs() -> dict:
    outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
    outputs["environment"] = "staging"
    outputs["secret_prefix"] = "vecten/acme/staging/"
    outputs["hosts"]["platform"]["hostnames"] = ["airflow.staging.acme.com"]
    outputs["hosts"]["mcp"]["hostnames"] = ["mcp.staging.acme.com"]
    return outputs


def test_a_second_environment_serves_its_own_domain_end_to_end(project, tmp_path):
    prod = (project / "deploy" / "envs" / "prod.tfvars").read_text()
    staging = prod.replace('"prod"', '"staging"').replace('"data.acme.com"', '"staging.acme.com"')
    assert '"staging.acme.com"' in staging
    (project / "deploy" / "envs" / "staging.tfvars").write_text(staging)
    config = load_config(project)
    outputs = EnvironmentOutputs.model_validate(staging_outputs())
    bundles = {
        host: build_host_bundle(project, config, "staging", outputs, host, COMMIT, tmp_path / host)
        for host in ("platform", "mcp")
    }
    conf = read(bundles["platform"], ".vecten/stack/nginx/conf.d/default.conf")
    assert "server_name airflow.staging.acme.com;" in conf
    assert "/etc/letsencrypt/live/airflow.staging.acme.com/fullchain.pem" in conf
    assert "data.acme.com" not in conf
    mcp_conf = read(bundles["mcp"], ".vecten/stack/nginx/conf.d/default.conf")
    assert "server_name mcp.staging.acme.com;" in mcp_conf
    assert "/etc/letsencrypt/live/mcp.staging.acme.com/privkey.pem" in mcp_conf
    assert "data.acme.com" not in mcp_conf
    for host, name in (("platform", "airflow"), ("mcp", "mcp")):
        doc = json.loads(read(bundles[host], ".vecten/ship/host.json"))
        assert doc["hostnames"] == [f"{name}.staging.acme.com"]
    platform = yaml.safe_load(read(bundles["platform"], ".vecten/stack/docker-compose.yml"))
    airflow_env = platform["services"]["airflow-api-server"]["environment"]
    assert airflow_env["AIRFLOW__API__BASE_URL"] == "https://airflow.staging.acme.com"
    mcp = yaml.safe_load(read(bundles["mcp"], ".vecten/stack/docker-compose.yml"))
    mcp_env = mcp["services"]["mcp-server"]["environment"]
    assert mcp_env["VECTEN_MCP_PUBLIC_URL"] == "https://mcp.staging.acme.com"
    assert component_url(outputs, "platform") == (
        "https://airflow.staging.acme.com/api/v2/monitor/health"
    )
    assert component_url(outputs, "mcp") == "https://mcp.staging.acme.com/health"


SCW_PROJECT_ID = "0123abcd-1111-2222-3333-444455556666"


def scaleway_outputs() -> dict:
    outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
    outputs["region"] = "fr-par"
    outputs["bucket"] = "vecten-acme-prod-artifacts-0123abcd"
    outputs["secret_prefix"] = "/vecten/acme/prod/"
    for key in outputs["postgres"]:
        outputs["postgres"][key]["master_secret"] = f"{key}-secret-id"
    return outputs


def test_host_json_carries_the_aws_cloud_contract(project, tmp_path):
    host = json.loads(read(build(project, tmp_path / "out"), ".vecten/ship/host.json"))
    assert host["cloud"] == "aws"
    assert host["project_id"] == ""
    assert host["bucket"] == "vecten-acme-prod-data"
    assert host["plugin_dir"] == "/usr/libexec/docker/cli-plugins"


def test_host_json_carries_the_scaleway_cloud_contract(tmp_path):
    root = render_aws(tmp_path / "proj", cloud="scaleway")
    set_facts(root, cloud_ids={"project": SCW_PROJECT_ID})
    bundle = build(root, tmp_path / "out", outputs=scaleway_outputs())
    host = json.loads(read(bundle, ".vecten/ship/host.json"))
    assert set(host) == set(agent.HOST_KEYS)
    assert host["cloud"] == "scaleway"
    assert host["region"] == "fr-par"
    assert host["project_id"] == SCW_PROJECT_ID
    assert host["bucket"] == "vecten-acme-prod-artifacts-0123abcd"
    assert host["secret_prefix"] == "/vecten/acme/prod/"
    assert host["plugin_dir"] == "/usr/local/lib/docker/cli-plugins"
    compose = yaml.safe_load(read(bundle, ".vecten/stack/docker-compose.yml"))
    env = compose["services"]["airflow-scheduler"]["environment"]
    assert env["SCW_SECRET_KEY"] == "${SCW_SECRET_KEY}"
    assert env["AIRFLOW_CONN_AWS_DEFAULT"] == "${AIRFLOW_CONN_AWS_DEFAULT}"


def test_a_scaleway_bundle_needs_the_bootstrapped_project_id(tmp_path):
    root = render_aws(tmp_path / "proj", cloud="scaleway")
    with pytest.raises(Failure) as exc:
        build(root, tmp_path / "out", outputs=scaleway_outputs())
    assert exc.value.code == ExitCode.CONFIG
    assert 'cloud_ids["project"]' in exc.value.message
    assert "vecten bootstrap" in exc.value.message


def test_bundle_carries_feedback_manifest_and_mcp_mount(project, tmp_path):
    bundle = build(project, tmp_path / "out", host="mcp")
    manifest = json.loads(read(bundle, ".vecten/ship/feedback.json"))
    assert manifest == {"commit": COMMIT, "ancestors": [], "resolutions": [], "unreviewed": []}
    compose = yaml.safe_load(read(bundle, ".vecten/stack/docker-compose.yml"))
    server = compose["services"]["mcp-server"]
    assert server["environment"]["VECTEN_FEEDBACK_MANIFEST"] == "/app/feedback.json"
    assert "../ship/feedback.json:/app/feedback.json:ro" in server["volumes"]
