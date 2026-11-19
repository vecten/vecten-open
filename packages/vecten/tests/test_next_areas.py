"""`next` asks who may read each declared area and, after deploy, seeds the area grant."""

from vecten.next.areas import area_access_apply_items, area_access_ask_items
from vecten.next.items import Item
from vecten.next.post_deploy import post_deploy_items
from vecten_shared.config import VectenConfig

from .fixture_project import render_fixture


def config(**overrides):
    base = {
        "users": 3,
        "project": {"name": "acme", "scaffold_version": "0.1.0"},
        "wanted": [],
        "connectors": [{"name": "hubspot", "source": "dlt", "access": {"groups": ["sales"]}}],
    }
    return VectenConfig.model_validate({**base, **overrides})


def test_an_area_without_access_is_asked_for_listing_the_existing_groups():
    cfg = config(areas={"finance": {}, "board": {"access": {"groups": ["directors"]}}})
    assert area_access_ask_items(cfg) == [
        Item(
            "ask",
            "Who may read the finance area?",
            "Which groups may read the finance area (schemas core_finance and marts_finance)? "
            "Name one or more groups, comma-separated.",
            answer_keys=("access.finance",),
            allowed=("default", "directors", "sales"),
        )
    ]


def test_no_area_no_ask():
    assert area_access_ask_items(config()) == []


def test_the_area_grant_is_seeded_after_deploy_until_every_group_holds_it(tmp_path):
    cfg = config(areas={"finance": {"access": {"groups": ["finance", "board"]}}})
    assert area_access_apply_items(tmp_path, cfg, "prod") == []
    cfg.environments["prod"].deployed_commit = "abc123"
    assert area_access_apply_items(tmp_path, cfg, "prod") == [
        Item(
            "command",
            "Grant board, finance access to the finance area",
            "vecten group add board --env prod"
            " && vecten grant --area finance --group board --env prod"
            " && vecten group add finance --env prod"
            " && vecten grant --area finance --group finance --env prod",
        )
    ]
    cfg.environments["prod"].area_access_applied = ["finance"]
    assert area_access_apply_items(tmp_path, cfg, "prod") == []


def test_an_area_without_access_seeds_nothing_and_an_unknown_env_adds_none(tmp_path):
    cfg = config(areas={"finance": {}})
    cfg.environments["prod"].deployed_commit = "abc123"
    assert area_access_apply_items(tmp_path, cfg, "prod") == []
    assert area_access_apply_items(tmp_path, cfg, "staging") == []
    assert "staging" not in cfg.environments


def test_post_deploy_reports_the_area_grant_beside_the_connector_grants(tmp_path):
    from vecten.next.gitstate import GitState
    from vecten_shared.config import AccessDecl, AreaDecl, load_config, save_config

    root = render_fixture(tmp_path, "postgres", deploy=True)
    cfg = load_config(root)
    cfg.areas = {"finance": AreaDecl(access=AccessDecl(groups=["finance"]))}
    cfg.environments["prod"].deployed_commit = "abc123"
    cfg.environments["prod"].first_user_added = True
    save_config(root, cfg)
    git = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)
    titles = [i.title for i in post_deploy_items(root, load_config(root), "prod", git)]
    assert "Grant finance access to the finance area" in titles
