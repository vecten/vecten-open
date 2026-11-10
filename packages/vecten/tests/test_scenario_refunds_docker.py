"""Design section 10, "Refunds", against the mocked provider and a fixture repository.

A sales lead asks for revenue by country, sees refunds counted in it and reports it. The gap
opens; the `semantic` skill's change (a mart column and the metric) merges as one reviewed pull
request; `ship` places the bundle without Terraform. Until the dbt DAG builds that commit the
MCP keeps the old definition and says "shipped, waiting for build"; after the build it swaps,
resolves the gap with the pull request and commit, tells the reporter on their next session,
and `vecten mcp validate --evals` holds the case that would catch a revert.
"""

import json

import pytest

from vecten.deploy import feedback as feedback_mod
from vecten.github import gh as gh_mod

from .deploy_fixtures import set_facts
from .docker_util import docker_available
from .fixture_project import render_fixture
from .scenarios.platform import Hosts, load_served, use_mock_cloud
from .scenarios.refunds import QUESTION, REQUEST, edit, project_at_a, semantic_fix
from .scenarios.repository import FixtureRepository

pytestmark = pytest.mark.docker
PASSWORD = "long-fixture-password"


class RecordingTerraform:
    created = 0

    def __init__(self):
        type(self).created += 1

    def which(self):
        raise AssertionError("ship ran terraform")

    def run(self, *args, **kwargs):
        raise AssertionError("ship ran terraform")


def by_country(answer):
    return {row[0]: float(row[1]) for row in answer["rows"]}


def test_refunds(invoke, tmp_path, monkeypatch):
    if not docker_available():
        pytest.skip("Docker unavailable")
    served = load_served()
    root = render_fixture(tmp_path / "repo", "postgres", deploy=True)
    project_at_a(root)
    repo = FixtureRepository(root, "acme/data")
    first = repo.init()
    set_facts(root, bootstrapped=True, state_bucket="mock-state", deployed_commit=first)
    deployed = repo.commit("Record the first deploy")

    cloud = tmp_path / "cloud"
    use_mock_cloud(monkeypatch, cloud)
    from vecten_mock_cloud import MockCloud

    MockCloud(root, "prod").store_outputs(MockCloud.OUTPUTS)
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
        assert ship()["commit"] == deployed
        hosts.load_raw()
        pg.rerun_setup()
        hosts.dbt_run()
        pg.rerun_setup()

        # 1-2. The lead's question, the old figure, the report and the gap.
        with hosts.mcp(monkeypatch, served) as mcp:
            ada = mcp.sign_in("ada@acme.test")["access_token"]
            health = mcp.call(ada, "model_health")["data"]
            assert (health["status"], health["live_commit"]) == ("ready", deployed), health
            grace = mcp.admin(ada, "users/create", {"email": "grace@acme.test", "name": "Grace"})
            assert grace["ok"]
            lead = mcp.sign_in("grace@acme.test", PASSWORD)["access_token"]
            before = mcp.call(lead, "query_metrics", REQUEST)
            assert by_country(before)["DE"] == 9200.5
            mcp.call(
                lead,
                "report_feedback",
                {
                    "question": QUESTION,
                    "expected": "Revenue net of refunds",
                    "returned": "DE 9200.50",
                    "tool_calls": [{"tool": "query_metrics", "arguments": REQUEST}],
                    "category": "wrong definition",
                    "raised_by": "user",
                },
            )
            mcp.runtime.feedback.fold()
            gaps = mcp.call(ada, "list_gaps")["data"]
            assert [g["names"] for g in gaps] == [["total_revenue"]], gaps
            gap = gaps[0]["id"]

        # 3. The semantic skill's change, validated, reviewed and merged as one pull request.
        repo.branch(f"semantic/{gap}")
        semantic_fix(root, gap)
        monkeypatch.chdir(root)
        validated = invoke("mcp", "validate", "--json")
        assert validated.exit_code == 0, validated.stdout
        repo.commit(f"Exclude refunds from total_revenue ({gap})")
        merge = repo.merge(
            f"semantic/{gap}",
            number=7,
            author="semantic-agent",
            reviewer="ada",
            body=f"Resolves {gap}: {QUESTION} counted refunds.",
        )

        # 4. Ship: the bundle on both hosts, no Terraform, no deploy dispatched.
        assert ship()["commit"] == merge
        assert RecordingTerraform.created == 0
        assert repo.gh.dispatched() == []
        manifest = json.loads((hosts.release("mcp") / ".vecten/ship/feedback.json").read_text())
        assert [(r["gap"], r["merge_commit"], r["reviewers"]) for r in manifest["resolutions"]] == [
            (gap, merge, ["ada"])
        ]

        with hosts.mcp(monkeypatch, served) as mcp:
            # 5. Restarted on the new release before the build: the old definition still serves.
            health = mcp.call(ada, "model_health")["data"]
            assert health["status"] == "shipped, waiting for build", health
            assert health["live_commit"] == deployed
            assert by_country(mcp.call(lead, "query_metrics", REQUEST))["DE"] == 9200.5
            waiting = mcp.call(ada, "read_gap", {"gap_id": gap})["data"]
            assert waiting["delivery_status"] == "merged, waiting for build", waiting

            # 6. The dbt DAG builds the merge; the MCP swaps and resolves the gap.
            assert hosts.dbt_run() == merge
            assert hosts.built()["commit"] == merge
            mcp.runtime.tick()
            health = mcp.call(ada, "model_health")["data"]
            assert (health["status"], health["live_commit"]) == ("ready", merge), health
            assert by_country(mcp.call(lead, "query_metrics", REQUEST))["DE"] == 9000.0
            resolved = mcp.call(ada, "read_gap", {"gap_id": gap})["data"]
            assert resolved["status"] == "resolved"
            assert resolved["pull_request"] == "https://github.com/acme/data/pull/7"
            assert resolved["resolved_commit"] == merge

            # 7. The reporter's next session carries the notice on its first tool response.
            again = served.human_token(
                mcp.http, mcp.runtime.oauth.resource, "grace@acme.test", PASSWORD
            )["access_token"]
            first_call = mcp.call_raw(again, "describe", {"dataset": "sales_pipeline"})
            notices = first_call["_meta"]["vecten/definition_changes"]
            assert [n["gap"] for n in notices] == [gap]
            assert notices[0]["pull_request"] == "https://github.com/acme/data/pull/7"
            later = mcp.call_raw(again, "describe", {"dataset": "sales_pipeline"})
            assert "vecten/definition_changes" not in later["_meta"]

    # 8. The eval case holds the definition: a revert fails it, naming the gap.
    monkeypatch.chdir(root)
    held = invoke("mcp", "validate", "--evals", "--json")
    assert held.exit_code == 0, held.stdout
    edit(
        root / "osi/metrics.json",
        "SUM(sales_pipeline.amount) - SUM(sales_pipeline.refunded_amount)",
        "SUM(sales_pipeline.amount)",
    )
    reverted = invoke("mcp", "validate", "--evals", "--json")
    assert reverted.exit_code == 6 and gap in reverted.stdout, reverted.stdout
