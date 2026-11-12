"""The promotion order as the CLI reads it, and the commit statuses that record each ship."""

import re

import pytest

from vecten.deploy import github_env
from vecten.deploy.promotion import (
    SHIPPED_SEARCH_LIMIT,
    newest_shipped,
    promotion_order,
    ship_context,
)
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure

from .deploy_fixtures import COMMIT, FakeGh, gh_fields, render_aws

OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def ordered(root, order=("dev", "prod"), version="0.1.1", tfvars=("dev",)):
    """Declare `order` in vecten.toml under `version`, with a tfvars file for each of `tfvars`."""
    prod = root / "deploy" / "envs" / "prod.tfvars"
    for env in tfvars:
        text = re.sub(r'(?m)^(environment\s*=\s*)"prod"', rf'\1"{env}"', prod.read_text())
        assert f'"{env}"' in text
        (root / "deploy" / "envs" / f"{env}.tfvars").write_text(text)
    text = (root / "vecten.toml").read_text()
    text = text.replace("scaffold_version = ", f'scaffold_version = "{version}"\n# ', 1)
    names = ", ".join(f'"{env}"' for env in order)
    text = text.replace("[deploy]\n", f"[deploy]\npromotion = [{names}]\n", 1)
    (root / "vecten.toml").write_text(text)
    return root


@pytest.fixture
def project(tmp_path):
    return render_aws(tmp_path / "proj")


def test_a_project_without_an_order_reads_prod_alone(project):
    assert promotion_order(project, load_config(project)) == ("prod",)


def test_an_order_whose_environments_all_have_tfvars_is_read(project):
    ordered(project)
    assert promotion_order(project, load_config(project)) == ("dev", "prod")


def test_an_environment_without_tfvars_is_named_with_the_key(project):
    ordered(project, ("dev", "staging", "prod"), tfvars=("dev",))
    with pytest.raises(Failure) as exc:
        promotion_order(project, load_config(project))
    assert exc.value.code == ExitCode.CONFIG
    assert "deploy.promotion" in exc.value.message
    assert "deploy/envs/staging.tfvars" in exc.value.message


def test_an_order_under_an_older_scaffold_names_vecten_update(project):
    ordered(project, version="0.1.0")
    with pytest.raises(Failure) as exc:
        promotion_order(project, load_config(project))
    assert exc.value.code == ExitCode.REFUSED and "vecten update" in exc.value.message


def test_the_ship_context_names_the_environment():
    assert ship_context("dev") == "vecten/ship/dev"


def sha(n: int) -> str:
    return f"{n:040x}"


@pytest.mark.parametrize("state", ["pending", "success", "failure"])
def test_a_status_is_set_on_the_commit_for_the_environment(tmp_path, state):
    gh = FakeGh()
    github_env.set_ship_status(
        gh, tmp_path, "acme/data", COMMIT, "dev", state, target_url="https://run/7"
    )
    assert gh.posted_statuses() == [(COMMIT, "vecten/ship/dev", state)]
    call = gh.calls[-1]
    assert call[:4] == ["api", "-X", "POST", f"repos/acme/data/statuses/{COMMIT}"]
    assert gh_fields(call) == {
        "state": state,
        "context": "vecten/ship/dev",
        "description": github_env.SHIP_STATUS_DESCRIPTIONS[state],
        "target_url": "https://run/7",
    }


def test_a_status_is_read_for_one_commit_and_context(tmp_path):
    gh = FakeGh(statuses={COMMIT: [("vecten/ship/dev", "pending"), ("vecten/ship/dev", "success")]})
    assert github_env.ship_status(gh, tmp_path, "acme/data", COMMIT, "dev") == "success"
    assert github_env.ship_status(gh, tmp_path, "acme/data", COMMIT, "prod") is None
    assert github_env.ship_status(gh, tmp_path, "acme/data", OTHER, "dev") is None


def test_the_newest_successful_commit_is_what_the_environment_shipped(tmp_path):
    gh = FakeGh(
        history=[sha(3), sha(2), sha(1)],
        statuses={
            sha(2): [("vecten/ship/dev", "success")],
            sha(1): [("vecten/ship/dev", "success")],
        },
    )
    assert github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev") == sha(2)


def test_after_a_rollback_the_commit_the_hosts_went_back_to_is_what_shipped(tmp_path):
    # b71d004 failed its health poll, so the hosts run a3f9c2e again.
    gh = FakeGh(
        history=[sha(2), sha(1)],
        statuses={
            sha(2): [("vecten/ship/dev", "pending"), ("vecten/ship/dev", "failure")],
            sha(1): [("vecten/ship/dev", "pending"), ("vecten/ship/dev", "success")],
        },
    )
    assert github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev") == sha(1)


def test_a_pending_or_other_environments_status_does_not_count(tmp_path):
    gh = FakeGh(
        history=[sha(3), sha(2), sha(1)],
        statuses={
            sha(3): [("vecten/ship/dev", "pending")],
            sha(2): [("vecten/ship/prod", "success")],
        },
    )
    assert github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev") is None


def test_the_search_reads_the_last_200_commits_and_no_more(tmp_path):
    history = [sha(n) for n in range(300, 0, -1)]
    inside = FakeGh(history=history, statuses={history[199]: [("vecten/ship/dev", "success")]})
    assert github_env.last_shipped(inside, tmp_path, "acme/data", "main", "dev") == history[199]
    beyond = FakeGh(history=history, statuses={history[200]: [("vecten/ship/dev", "success")]})
    assert github_env.last_shipped(beyond, tmp_path, "acme/data", "main", "dev") is None
    pages = [c for c in beyond.calls if c[:2] == ["api", "graphql"]]
    assert len(pages) == SHIPPED_SEARCH_LIMIT // 100


def test_the_search_names_the_branch_and_the_context(tmp_path):
    gh = FakeGh(history=[sha(1)])
    github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev")
    fields = gh_fields(gh.calls[0])
    assert fields["owner"] == "acme" and fields["name"] == "data"
    assert fields["branch"] == "refs/heads/main" and fields["context"] == "vecten/ship/dev"


def test_a_failing_gh_names_the_call(tmp_path):
    gh = FakeGh(failing=("api",))
    with pytest.raises(Failure) as exc:
        github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev")
    assert exc.value.code == ExitCode.TOOL_FAILED and "graphql" in exc.value.message


def test_nothing_shipped_in_the_last_200_commits_names_commit(tmp_path):
    gh = FakeGh(history=[sha(n) for n in range(250, 0, -1)])
    with pytest.raises(Failure) as exc:
        newest_shipped(gh, tmp_path, "acme/data", "main", "dev")
    message = exc.value.message
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "dev" in message and "200" in message and "main" in message and "--commit" in message


def test_the_newest_shipped_commit_is_returned(tmp_path):
    gh = FakeGh(history=[sha(2), sha(1)], statuses={sha(1): [("vecten/ship/dev", "success")]})
    assert newest_shipped(gh, tmp_path, "acme/data", "main", "dev") == sha(1)


def test_the_commit_whose_success_was_set_last_is_what_shipped(tmp_path):
    # staging shipped b, then rolled back to a with --commit: a's success is the later one.
    gh = FakeGh(
        history=[sha(2), sha(1)],
        statuses={
            sha(2): [("vecten/ship/dev", "success", "2026-03-01T10:00:00Z")],
            sha(1): [("vecten/ship/dev", "success", "2026-03-02T09:00:00Z")],
        },
    )
    assert github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev") == sha(1)
    query = gh_fields(gh.calls[0])["query"]
    assert "createdAt" in query


def test_a_later_failure_on_a_commit_does_not_hide_an_earlier_success_elsewhere(tmp_path):
    gh = FakeGh(
        history=[sha(2), sha(1)],
        statuses={
            sha(2): [("vecten/ship/dev", "failure", "2026-03-02T10:00:00Z")],
            sha(1): [("vecten/ship/dev", "success", "2026-03-01T10:00:00Z")],
        },
    )
    assert github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev") == sha(1)
