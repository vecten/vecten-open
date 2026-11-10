"""Design section 10, "Renamed field", against the mocked provider and a fixture repository.

The HubSpot sync fails three nights running because the upstream renamed a field. The
connector fix merges as a reviewed pull request, the ship workflow places the bundle, and the
next scheduled run is green without anyone dispatching `deploy`: no Terraform runs and the
built commit advances. Through the operations loop, `vecten issues` lists the failure once
with its three nights, the `operate` skill reads it through `issues show` and routes it to
`connector`, whose fix is what this scenario commits, and the list is empty after the next
green run. The laptop flavour runs the same loop against the local stack.
"""

import json

import pytest

from vecten.deploy import feedback as feedback_mod
from vecten.github import gh as gh_mod
from vecten.issues import airflow as airflow_mod
from vecten_shared.records import write_record

from .deploy_fixtures import set_facts
from .docker_util import docker_available
from .scenarios.hubspot import DEALS, hubspot_project, upstream, write_connector
from .scenarios.platform import Hosts, load_served, use_mock_cloud
from .scenarios.repository import FixtureRepository

pytestmark = pytest.mark.docker

DEALS_ISSUE = "iss_airflow_sync_hubspot_default_sync.deals_KeyError"


class NoGaps:
    """The admin API of a healthy MCP with no open gap; the Refunds scenario exercises gaps."""

    def __init__(self, root, env):
        pass

    def read(self, operation, params):
        if operation == "model-health":
            return {"status": "ready", "errors": [], "warnings": []}
        if operation == "access-health":
            return {"checked_at": None, "failures": []}
        return []


class RecordingTerraform:
    created = 0

    def __init__(self):
        type(self).created += 1

    def which(self):
        raise AssertionError("ship ran terraform")

    def run(self, *args, **kwargs):
        raise AssertionError("ship ran terraform")


def raw_deals(pg):
    return pg.sql("SELECT id, dealname FROM raw_hubspot.deals ORDER BY id", user="loader")


def issues(invoke, root, monkeypatch, *env) -> list[dict]:
    monkeypatch.chdir(root)
    result = invoke("issues", *env, "--json")
    assert result.exit_code == 0, result.stdout
    doc = json.loads(result.stdout)
    assert set(doc["sources"].values()) == {"ok"}, doc["sources"]
    return doc["issues"]


def show(invoke, root, monkeypatch, issue_id, *env) -> dict:
    monkeypatch.chdir(root)
    result = invoke("issues", "show", issue_id, *env, "--json")
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["issue"]


def assert_three_failed_nights_listed(listed, read):
    """What `operate` sees: one issue for the object, routed to `connector`, its evidence
    naming the renamed field."""
    assert [(i["id"], i["occurrences"]) for i in listed] == [(DEALS_ISSUE, 3)]
    (issue,) = listed
    assert (issue["kind"], issue["resolution"]) == ("skill", "connector")
    shown = read(DEALS_ISSUE)
    assert shown["evidence"]["untrusted"] is True
    assert shown["evidence"]["error_class"] == "KeyError"
    assert "'dealname'" in shown["evidence"]["excerpt"]


def test_renamed_field(invoke, tmp_path, monkeypatch):
    if not docker_available():
        pytest.skip("Docker unavailable")
    served = load_served()
    root = hubspot_project(tmp_path / "repo")
    repo = FixtureRepository(root, "acme/data")
    first = repo.init()
    set_facts(root, bootstrapped=True, state_bucket="mock-state", deployed_commit=first)
    deployed = repo.commit("Record the first deploy")

    response = tmp_path / "upstream" / "deals.json"
    response.parent.mkdir()
    upstream(response, "dealname")
    monkeypatch.setenv("HUBSPOT_FIXTURE_FILE", str(response))
    cloud = tmp_path / "cloud"
    use_mock_cloud(monkeypatch, cloud)
    from vecten_mock_cloud import MockCloud

    plane = MockCloud(root, "prod")
    plane.store_outputs(MockCloud.OUTPUTS)
    plane.put_secret("AIRFLOW_ADMIN_PASSWORD", "admin-password")
    monkeypatch.setattr("vecten.mcp_client.AdminClient", NoGaps)
    monkeypatch.setattr(feedback_mod, "get_gh", lambda: repo.gh)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: repo.gh)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:RecordingTerraform")
    RecordingTerraform.created = 0

    def ship():
        monkeypatch.chdir(root)
        result = invoke("ship", "--env", "prod", "--json")
        assert result.exit_code == 0, result.stdout
        return json.loads(result.stdout)

    with served.laptop_postgres(tmp_path) as pg:
        hosts = Hosts(cloud, pg, monkeypatch, tmp_path / "scratch")
        monkeypatch.setattr(airflow_mod, "TRANSPORT", hosts.airflow.transport())
        prod = ("--env", "prod")
        # 1. Deployed and green: the sync lands the deals, the dbt DAG builds the commit.
        assert ship()["commit"] == deployed
        hosts.load_raw()
        assert hosts.sync("hubspot", ["deals"]) == [
            {"object": "deals", "ok": True, "rows": 3, "error": None}
        ]
        assert hosts.dbt_run() == deployed
        assert issues(invoke, root, monkeypatch, *prod) == []

        # 2. The upstream renames the field; three nights fail and nothing is built past it.
        upstream(response, "deal_name")
        for _night in range(3):
            [outcome] = hosts.sync("hubspot", ["deals"])
            assert not outcome["ok"] and "dealname" in outcome["error"], outcome
        assert hosts.built()["commit"] == deployed
        assert_three_failed_nights_listed(
            issues(invoke, root, monkeypatch, *prod),
            lambda i: show(invoke, root, monkeypatch, i, *prod),
        )

        # 3. The connector skill's fix: read the new field, keep the raw column stable.
        repo.branch("connector/hubspot-deal-name")
        write_connector(root, "deal_name")
        repo.commit("Read deal_name, renamed upstream")
        fix = repo.merge(
            "connector/hubspot-deal-name",
            number=8,
            author="connector-agent",
            reviewer="ada",
            body="HubSpot renamed dealname to deal_name.",
        )

        # 4. The ship workflow places the bundle; no Terraform, no deploy dispatched.
        assert ship() == {
            "ok": True,
            "env": "prod",
            "dispatched": False,
            "commit": fix,
            "hosts": ["mcp", "platform"],
        }
        assert RecordingTerraform.created == 0
        assert repo.gh.dispatched() == []
        assert hosts.release("platform").name == fix

        # 5. The next scheduled run is green and raw keeps its shape.
        assert hosts.sync("hubspot", ["deals"]) == [
            {"object": "deals", "ok": True, "rows": 3, "error": None}
        ]
        assert raw_deals(pg) == [(i, name) for i, name in DEALS]

        # 6. The dbt DAG builds the fix, and the built commit advances past the deployed one.
        assert hosts.dbt_run() == fix
        built = hosts.built()
        assert built["commit"] == fix and deployed in built["ancestors"]

        # 7. `operate` closes the issue only when `issues` lists nothing, after the green run.
        assert issues(invoke, root, monkeypatch, *prod) == []


def test_renamed_field_on_the_laptop(invoke, tmp_path, monkeypatch):
    """The same loop against the laptop stack: the local Airflow's API and `.vecten/`."""
    if not docker_available():
        pytest.skip("Docker unavailable")
    served = load_served()
    root = hubspot_project(tmp_path / "repo")
    (root / ".env").write_text("AIRFLOW_ADMIN_USER=admin\nAIRFLOW_ADMIN_PASSWORD=admin-password\n")
    write_record(root, "up", {"airflow_api": "http://127.0.0.1:8482", "airflow_user": "admin"})
    monkeypatch.setattr("vecten.mcp_client.AdminClient", NoGaps)
    response = tmp_path / "upstream" / "deals.json"
    response.parent.mkdir()
    upstream(response, "dealname")
    monkeypatch.setenv("HUBSPOT_FIXTURE_FILE", str(response))

    with served.laptop_postgres(tmp_path) as pg:
        hosts = Hosts(tmp_path / "cloud", pg, monkeypatch, tmp_path / "scratch", laptop=root)
        monkeypatch.setattr(airflow_mod, "TRANSPORT", hosts.airflow.transport())
        hosts.load_raw()
        assert hosts.sync("hubspot", ["deals"])[0]["ok"]
        assert issues(invoke, root, monkeypatch) == []

        upstream(response, "deal_name")
        for _night in range(3):
            assert not hosts.sync("hubspot", ["deals"])[0]["ok"]
        assert_three_failed_nights_listed(
            issues(invoke, root, monkeypatch),
            lambda i: show(invoke, root, monkeypatch, i),
        )

        write_connector(root, "deal_name")
        assert hosts.sync("hubspot", ["deals"])[0]["ok"]
        assert raw_deals(pg) == [(i, name) for i, name in DEALS]
        assert issues(invoke, root, monkeypatch) == []
