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
DEV_OK_STATUS = ("vecten/ship/dev", "success")


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


def test_one_commits_status_is_read_from_a_full_page(tmp_path):
    gh = FakeGh(statuses={COMMIT: [DEV_OK_STATUS]})
    github_env.ship_status(gh, tmp_path, "acme/data", COMMIT, "dev")
    assert gh.calls[-1][1] == f"repos/acme/data/commits/{COMMIT}/status?per_page=100"


def test_a_project_rendered_by_the_next_release_may_record_an_order():
    # PROMOTION_SINCE names the release that introduces the order; `vecten dev bump-version`
    # to that release must render projects allowed to record one.
    from packaging.version import Version

    from vecten_shared.config import PROMOTION_SINCE
    from vecten_shared.versions import VERSIONS

    current = Version(VERSIONS.vecten)
    next_release = Version(f"{current.major}.{current.minor}.{current.micro + 1}")
    assert Version(PROMOTION_SINCE) <= next_release


A, B = sha(1), sha(2)


def ship_through(gh, tmp_path, commit, *, fails=False, required=True, during=None):
    """Run `recorded_ship` for dev on `commit`; `fails` raises from the ship before it
    returns, and `during` is called while the ship runs."""
    from vecten.deploy.run import recorded_ship

    def ship():
        if during is not None:
            during()
        if fails:
            raise Failure(ExitCode.TOOL_FAILED, "the bundle could not be uploaded")
        return ["platform"]

    return recorded_ship(gh, tmp_path, "acme/data", "dev", commit, ship, required=required)


def dev_shipped(gh, tmp_path):
    return github_env.last_shipped(gh, tmp_path, "acme/data", "main", "dev")


def test_a_failed_retry_of_the_commit_the_environment_runs_keeps_it_shipped(tmp_path):
    gh = FakeGh(history=[B, A])
    ship_through(gh, tmp_path, A)
    ship_through(gh, tmp_path, B)
    with pytest.raises(Failure) as exc:
        ship_through(gh, tmp_path, B, fails=True)
    assert dev_shipped(gh, tmp_path) == B
    assert gh.latest_status(B, "vecten/ship/dev") == "success"
    assert "success" in exc.value.message and B[:8] in exc.value.message


def test_a_retry_of_the_commit_the_environment_runs_still_counts_while_it_ships(tmp_path):
    gh = FakeGh(history=[B, A])
    ship_through(gh, tmp_path, A)
    ship_through(gh, tmp_path, B)
    seen = []
    ship_through(gh, tmp_path, B, during=lambda: seen.append(dev_shipped(gh, tmp_path)))
    assert seen == [B]
    assert dev_shipped(gh, tmp_path) == B
    assert gh.posted_statuses()[-1] == (B, "vecten/ship/dev", "success")


def test_a_failed_first_ship_leaves_the_earlier_commit_shipped(tmp_path):
    gh = FakeGh(history=[B, A])
    ship_through(gh, tmp_path, A)
    seen = []
    with pytest.raises(Failure):
        ship_through(
            gh, tmp_path, B, fails=True, during=lambda: seen.append(dev_shipped(gh, tmp_path))
        )
    assert seen == [A]
    assert dev_shipped(gh, tmp_path) == A
    assert gh.latest_status(B, "vecten/ship/dev") == "failure"


def test_a_failed_first_ship_with_nothing_shipped_before_leaves_nothing_shipped(tmp_path):
    gh = FakeGh(history=[B, A])
    with pytest.raises(Failure):
        ship_through(gh, tmp_path, B, fails=True)
    assert dev_shipped(gh, tmp_path) is None


def test_a_failed_retry_of_a_commit_the_environment_left_keeps_the_later_one_shipped(tmp_path):
    # dev shipped A, then B, then rolled back to A with --commit; a retry of B that fails,
    # before or after its hosts changed, leaves them on A's bundle.
    gh = FakeGh(history=[B, A])
    ship_through(gh, tmp_path, A)
    ship_through(gh, tmp_path, B)
    ship_through(gh, tmp_path, A)
    assert dev_shipped(gh, tmp_path) == A
    with pytest.raises(Failure):
        ship_through(gh, tmp_path, B, fails=True)
    assert dev_shipped(gh, tmp_path) == A


def test_a_ship_whose_earlier_status_cannot_be_read_stops_before_any_host(tmp_path, monkeypatch):
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    gh = FakeGh(history=[B, A], failing=("api",))
    seen = []
    with pytest.raises(Failure) as exc:
        ship_through(gh, tmp_path, B, during=lambda: seen.append("shipped"))
    assert seen == []
    assert f"commits/{B}/status" in exc.value.message


def test_an_unread_earlier_status_is_a_warning_where_nothing_promotes_from(tmp_path, monkeypatch):
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    gh = FakeGh(history=[B, A], failing=("api",))
    shipped, warnings = ship_through(gh, tmp_path, B, required=False)
    assert shipped == ["platform"]
    assert any("could not be read" in w for w in warnings), warnings


def test_an_earlier_status_read_is_retried_like_a_write(tmp_path, monkeypatch):
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    gh = FakeGh(statuses={B: [DEV_OK_STATUS]})
    gh.transient["status"] = 2
    assert github_env.ship_status(gh, tmp_path, "acme/data", B, "dev") == "success"
    gh.transient["status"] = 100
    with pytest.raises(Failure):
        github_env.ship_status(gh, tmp_path, "acme/data", B, "dev")
    reads = [c for c in gh.calls if c[:1] == ["api"] and "/status?" in c[1]]
    assert len(reads) == 3 + github_env.STATUS_ATTEMPTS
