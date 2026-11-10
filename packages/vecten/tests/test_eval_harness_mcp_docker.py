"""The evaluation harness's deployed MCP: the mocked deploy's release served over TLS under its
public name, with the warehouse built from the release and people signed in by password."""

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from vecten_shared.config import load_config, save_config
from vecten_shared.versions import VERSIONS

from .deploy_fixtures import set_facts
from .docker_util import docker_available, free_port
from .fixture_project import render_fixture
from .scenarios.platform import load_served, use_mock_cloud
from .scenarios.repository import FixtureRepository

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import ca as ca_mod  # noqa: E402
from eval_harness import gh_standin  # noqa: E402
from eval_harness import warehouse as warehouse_mod  # noqa: E402
from eval_harness.front import Front  # noqa: E402
from eval_harness.mcp_service import DeployedMcp, wait_until  # noqa: E402

pytestmark = pytest.mark.docker
HOST = "mcp.data.acme.test"
PASSWORD = "harness-sign-in-password"
ADMIN_PASSWORD = "warehouse-admin-password"
PROVISIONER = {
    "MCP_PROVISIONER_PASSWORD": "provisioner-password-1",
    "MCP_IMPERSONATOR_PASSWORD": "impersonator-password-1",
}
REQUEST = {"metrics": ["pipeline_value"], "dimensions": ["sales_pipeline.country"]}


def start_front(authority, tmp_path):
    """The harness's TLS front on a free port; (front, server, thread, base URL)."""
    cert, key = authority.issue([HOST], tmp_path / "front")
    front = Front()
    port = free_port()
    server = front.server("127.0.0.1", port, cert, key)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_until(lambda: server.started, 30)
    return front, server, thread, f"https://{HOST}:{port}"


def client(monkeypatch, authority, base):
    """An HTTPS client that reaches HOST on the loopback address, trusting the authority.

    The MCP accepts only its own public name in the Host header, so the port stays out of it.
    """
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        return real_getaddrinfo("127.0.0.1" if host == HOST else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    context = ssl.create_default_context(cafile=str(authority.cert_path))
    return httpx.Client(base_url=base, verify=context, headers={"Host": HOST})


def deployed_project(tmp_path):
    root = render_fixture(tmp_path / "repo", "postgres", deploy=True, domain="data.acme.test")
    metrics = json.loads((root / "osi/metrics.json").read_text())
    metrics["semantic_model"][0]["metrics"].append(
        {
            "name": "pipeline_value",
            "description": "The sum of deal amounts.",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(sales_pipeline.amount)"}]
            },
        }
    )
    (root / "osi/metrics.json").write_text(json.dumps(metrics, indent=2))
    repo = FixtureRepository(root, "acme/data")
    first = repo.init()
    set_facts(root, bootstrapped=True, state_bucket="mock-state", deployed_commit=first)
    return root, repo, repo.commit("Record the first deploy")


def answer_admins(root, repo):
    """`vecten answer mcp.admins ada@acme.test` after the deploy, then the push that ships it."""
    config = load_config(root)
    save_config(
        root,
        config.model_copy(
            update={"mcp": config.mcp.model_copy(update={"admins": ["ada@acme.test"]})}
        ),
    )
    return repo.commit("Answer the first admin")


def test_the_deployed_mcp_answers_a_signed_in_person_over_tls(tmp_path, monkeypatch):
    if not docker_available():
        pytest.skip("Docker unavailable")
    served = load_served()
    root, repo, commit = deployed_project(tmp_path)
    cloud = tmp_path / "cloud"
    use_mock_cloud(monkeypatch, cloud)
    from vecten_mock_cloud import MockCloud

    plane = MockCloud(root, "prod")
    outputs = plane.store_outputs(MockCloud.OUTPUTS)
    plane.ship_hosts(root, load_config(root), "prod", outputs, commit)

    authority = ca_mod.Authority.create(tmp_path / "ca")
    front, server, thread, base = start_front(authority, tmp_path)
    said = []
    service = DeployedMcp(
        front,
        cloud_dir=cloud,
        work=tmp_path / "mcp",
        host=HOST,
        password=PASSWORD,
        say=said.append,
    )
    http = client(monkeypatch, authority, base)
    try:
        waiting = http.get("/mcp")
        assert waiting.status_code == 503
        service.start()
        assert service.ready.wait(600), said
        assert service.served == commit
        state = service.runtime.oauth.backend.state
        assert state.by_email("ada@acme.test") is None

        admins = answer_admins(root, repo)
        plane.ship_hosts(root, load_config(root), "prod", outputs, admins)
        assert wait_until(lambda: service.served == admins, 300), said
        state = service.runtime.oauth.backend.state
        assert wait_until(lambda: state.get("passwords", "ada@acme.test") is not None, 30), said
        resource = service.runtime.oauth.resource
        ada = served.human_token(http, resource, "ada@acme.test", PASSWORD)["access_token"]
        mcp = served.Served(runtime=service.runtime, http=http, pg=None)
        created = mcp.admin(ada, "users/create", {"email": "grace@acme.test", "name": "Grace"})
        assert created["ok"], created
        assert wait_until(lambda: state.get("passwords", "grace@acme.test") is not None, 30)
        grace = served.human_token(http, resource, "grace@acme.test", PASSWORD)["access_token"]
        answer = mcp.call_raw(grace, "query_metrics", REQUEST)
        assert not answer.get("isError"), answer
        assert "vecten/receipt" in answer["_meta"], answer["_meta"]
        rows = mcp.call(grace, "query_metrics", REQUEST)["rows"]
        assert {row[0] for row in rows} == {"DE", "GB", "FR"}
        health = mcp.call(ada, "model_health")["data"]
        assert (health["status"], health["live_commit"]) == ("ready", admins), health
    finally:
        http.close()
        service.stop()
        server.should_exit = True
        thread.join(15)


def test_without_harness_passwords_the_admin_signs_in_through_admin_link(tmp_path, monkeypatch):
    """Guide cases 1 and 2: the person gets in the way the product lets them, through the
    token `vecten mcp admin-link` stores in the mocked cloud."""
    if not docker_available():
        pytest.skip("Docker unavailable")
    from vecten.mcp_admin_link import issue_admin_link

    served = load_served()
    root, repo, _first = deployed_project(tmp_path)
    config = load_config(root)
    config.mcp.auth, config.mcp.admins = "password", ["ada@acme.test"]
    save_config(root, config)
    commit = repo.commit("Password sign-in for Ada")
    cloud = tmp_path / "cloud"
    use_mock_cloud(monkeypatch, cloud)
    from vecten_mock_cloud import MockCloud

    plane = MockCloud(root, "prod")
    outputs = plane.store_outputs(MockCloud.OUTPUTS)
    plane.ship_hosts(root, load_config(root), "prod", outputs, commit)
    authority = ca_mod.Authority.create(tmp_path / "ca")
    front, server, thread, base = start_front(authority, tmp_path)
    said = []
    service = DeployedMcp(
        front,
        cloud_dir=cloud,
        work=tmp_path / "mcp",
        host=HOST,
        password=PASSWORD,
        seed_passwords=False,
        say=said.append,
    )
    http = client(monkeypatch, authority, base)
    try:
        service.start()
        assert service.ready.wait(600), said
        backend = service.runtime.oauth.backend
        assert backend.state.get("passwords", "ada@acme.test") is None
        link = issue_admin_link(root, "prod", None)
        backend.set_password(link["setup_token"], "ada-chose-this-password")
        resource = service.runtime.oauth.resource
        token = served.human_token(http, resource, "ada@acme.test", "ada-chose-this-password")
        assert token["access_token"]
        with pytest.raises(ValueError):
            backend.authenticate("ada@acme.test", PASSWORD, "eval-test")
    finally:
        http.close()
        service.stop()
        server.should_exit = True
        thread.join(15)


def vecten(env, root, *args, input=None):
    result = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", *args, "--json"],
        cwd=root,
        env=env,
        input=input,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_semantic_3_starts_its_warehouse_provisioned_with_the_fixture_table(tmp_path):
    from eval_harness import cli, projects
    from eval_harness.cases import CASES, SEMANTIC_PROVISIONER

    if not docker_available():
        pytest.skip("Docker unavailable")
    case = CASES["semantic-3"]
    paths = cli.Paths(tmp_path / "evals", "semantic-3")
    built = projects.build(case, paths.dir)
    authority = ca_mod.Authority.create(paths.ca)
    container = f"vecten-eval-warehouse-test-{uuid.uuid4().hex[:8]}"
    warehouse = cli.start_warehouse(
        case, paths, authority, built, port=free_port(), container=container
    )
    try:
        columns = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "postgres",
                "-d",
                "analytics",
                "-Atc",
                "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) "
                "FROM information_schema.columns WHERE table_schema = 'marts' "
                "AND table_name = 'orders'",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert columns == "order_id,total_amount,refund_amount,country"
        roles = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "postgres",
                "-Atc",
                "SELECT rolname FROM pg_roles",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert {"provisioner", "impersonator"} <= set(roles)
        stored = json.loads((paths.cloud / "secrets" / "prod.json").read_text())
        assert {k: stored[k] for k in SEMANTIC_PROVISIONER} == SEMANTIC_PROVISIONER
    finally:
        warehouse.stop()


def test_the_existing_warehouse_is_introspected_provisioned_and_served(tmp_path, monkeypatch):
    """Guide case 2 on the harness: the warehouse container, the administrator's provisioner
    run, and the deployed MCP over it. The locator names 127.0.0.1 here, since this process
    cannot resolve warehouse.acme.test without the hosts entry."""
    if not docker_available():
        pytest.skip("Docker unavailable")
    served = load_served()
    authority = ca_mod.Authority.create(tmp_path / "ca")
    cert, key = authority.issue([warehouse_mod.HOST, "127.0.0.1"], tmp_path / "leaf")
    port = free_port()
    warehouse = warehouse_mod.ExistingWarehouse(
        tmp_path / "warehouse",
        password=ADMIN_PASSWORD,
        port=port,
        container=f"vecten-eval-warehouse-test-{uuid.uuid4().hex[:8]}",
    )
    cloud = tmp_path / "cloud"
    use_mock_cloud(monkeypatch, cloud)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        **gh_standin.mock_cloud_environ(cloud, os.environ),
    }
    warehouse.start(cert, key, f"postgres:{VERSIONS.postgres_major}")
    try:
        locator = f"postgresql://mcp_admin@127.0.0.1:{port}/analytics"
        vecten(
            env,
            tmp_path,
            "init",
            "proj",
            "--name",
            "acme",
            "--users",
            "20",
            "--has-warehouse",
            "yes",
            "--warehouse-url",
            locator,
            "--cloud",
            "aws",
            "--domain",
            "data.acme.test",
            "--dns-zone",
            "acme.test",
            "--github-repository",
            "acme/data",
        )
        root = tmp_path / "proj"
        vecten(env, root, "secret", "set", "WAREHOUSE_ADMIN_PASSWORD", input=ADMIN_PASSWORD)
        # The rendered project holds an empty datasets draft, which introspect refuses to
        # overwrite without --replace.
        assert vecten(env, root, "mcp", "introspect", "--replace")["datasets"] == 1
        from vecten_mock_cloud import MockCloud

        plane = MockCloud(root, "prod")
        for name, value in PROVISIONER.items():
            plane.put_secret(name, value)
        warehouse.provision(root, PROVISIONER, env)

        metrics = json.loads((root / "osi/metrics.json").read_text())
        datasets = json.loads((root / "osi/datasets.json").read_text())
        dataset = datasets["semantic_model"][0]["datasets"][0]["name"]
        metrics["semantic_model"][0]["metrics"].append(
            {
                "name": "order_revenue",
                "description": "The sum of order amounts.",
                "expression": {
                    "dialects": [{"dialect": "ANSI_SQL", "expression": f"SUM({dataset}.amount)"}]
                },
            }
        )
        (root / "osi/metrics.json").write_text(json.dumps(metrics, indent=2))
        config = load_config(root)
        save_config(
            root,
            config.model_copy(
                update={"mcp": config.mcp.model_copy(update={"admins": ["ada@acme.test"]})}
            ),
        )
        repo = FixtureRepository(root, "acme/data")
        commit = repo.init()
        outputs = plane.store_outputs(gh_standin.deployed_outputs(load_config(root), "prod"))
        plane.ship_hosts(root, load_config(root), "prod", outputs, commit)

        front, server, thread, base = start_front(authority, tmp_path)
        service = DeployedMcp(
            front, cloud_dir=cloud, work=tmp_path / "mcp", host=HOST, password=PASSWORD
        )
        http = client(monkeypatch, authority, base)
        try:
            service.start()
            assert service.ready.wait(600)
            state = service.runtime.oauth.backend.state
            assert wait_until(lambda: state.get("passwords", "ada@acme.test") is not None, 30)
            resource = service.runtime.oauth.resource
            ada = served.human_token(http, resource, "ada@acme.test", PASSWORD)["access_token"]
            mcp = served.Served(runtime=service.runtime, http=http, pg=None)
            created = mcp.admin(ada, "users/create", {"email": "grace@acme.test", "name": "Grace"})
            assert created["ok"], created
            assert wait_until(lambda: state.get("passwords", "grace@acme.test") is not None, 30)
            grace = served.human_token(http, resource, "grace@acme.test", PASSWORD)
            request = {"metrics": ["order_revenue"]}
            answer = mcp.call(grace["access_token"], "query_metrics", request)
            assert Decimal(str(answer["rows"][0][0])) == warehouse_mod.ORDER_REVENUE
        finally:
            http.close()
            service.stop()
            server.should_exit = True
            thread.join(15)
    finally:
        warehouse.stop()
