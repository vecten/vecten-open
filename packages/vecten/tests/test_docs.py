import re
from pathlib import Path

import pytest
from typer.core import TyperGroup
from typer.main import get_command

from vecten.cli.app import app
from vecten.stack.compose import LOCAL_AIRFLOW_API_PORT, LOCAL_HTTP_PORT, LOCAL_HTTPS_PORT
from vecten_shared.output import ExitCode

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "docs" / "cli.md"


def cli_section(title: str) -> str:
    return CLI.read_text().split(f"\n## {title}\n")[1].split("\n## ")[0]


def top_level_commands() -> set[str]:
    root = get_command(app)
    assert isinstance(root, TyperGroup)
    return {name for name, cmd in root.commands.items() if not getattr(cmd, "hidden", False)}


def test_hidden_dev_group_is_excluded():
    assert "dev" in get_command(app).commands
    assert "dev" not in top_level_commands()


def test_cli_page_lists_every_command():
    table = cli_section("Commands")
    for name in top_level_commands():
        assert re.search(rf"^\| `{re.escape(name)}`", table, re.M), (
            f"{name} missing from the docs/cli.md command table"
        )


def test_cli_command_table_has_no_stale_rows():
    rows = set(re.findall(r"^\| `([^`]+)` \|", cli_section("Commands"), re.M))
    assert rows == top_level_commands()


README = REPO / "README.md"


def test_readme_opening_leads_with_the_mcp():
    text = README.read_text()
    # The logo and badge blocks sit above the title.
    title, opening = text[text.index("# Vecten Open") :].split("\n\n")[:2]
    assert title.startswith("# "), title
    opening = " ".join(opening.split())
    mcp = opening.find("MCP")
    assert mcp != -1, opening
    for later in ("Airflow", "dbt"):
        assert later not in opening[:mcp], later
    assert "your own cloud" in opening


def test_readme_is_short_and_has_no_guide_block():
    text = README.read_text().split("\n## Internal phase\n")[0]
    assert len(text.splitlines()) < 150
    assert "vecten-guide" not in text


def test_readme_links_the_deep_dives():
    text = README.read_text()
    for page in (
        "warehouse",
        "connectors",
        "orchestration",
        "mcp",
        "cli",
        "agentic-flow",
        "why-vecten",
        "deploy-aws",
        "deploy-scaleway",
    ):
        assert f"](docs/{page}.md)" in text, page


@pytest.mark.parametrize("page", ["README.md", "docs/orchestration.md"])
def test_local_ports_are_documented(page):
    text = (REPO / page).read_text()
    assert f"https://airflow.localhost:{LOCAL_HTTPS_PORT}" in text
    assert f"https://mcp.localhost:{LOCAL_HTTPS_PORT}" in text
    assert f"http://airflow.localhost:{LOCAL_HTTP_PORT}" in text
    assert f"`http://127.0.0.1:{LOCAL_AIRFLOW_API_PORT}`" in text


def test_exit_codes_documented():
    text = (REPO / "docs" / "exit-codes.md").read_text()
    for code in ExitCode:
        assert re.search(rf"^\| {code.value} \| {code.name} \|", text, re.M), code.name


def connecting_sources_section() -> str:
    page = (REPO / "docs" / "connectors.md").read_text()
    return page.split("\n## Four kinds of source\n")[1].split("\n## Extensions\n")[0]


def test_connectors_page_documents_the_dlt_paths_and_the_no_connectors_stance():
    section = connecting_sources_section()
    for phrase in (
        "dlt verified source",
        "`--from rest`",
        "`--from sql`",
        "`--from filesystem`",
        "extension",
        "written in the project",
    ):
        assert phrase in section, phrase
    assert "Vecten ships no connectors of its own." in section
    assert "vecten-managed connector" not in section
    for command in (
        "vecten connector add",
        "vecten connector run",
        "--record",
        "vecten raw schema",
        "vecten secret set",
    ):
        assert command in section, command
    assert "uv tool install vecten --with" in section


def test_connectors_page_says_fixtures_hold_real_records():
    section = " ".join(connecting_sources_section().split())
    for phrase in ("real records", "up to 200 per object", "committed", "personal data"):
        assert phrase in section, phrase


def test_connectors_page_names_only_the_four_source_kinds():
    bullets = [line for line in connecting_sources_section().splitlines() if line.startswith("- ")]
    assert len(bullets) == 4, bullets


def modelling_section() -> str:
    page = (REPO / "docs" / "warehouse.md").read_text()
    return page.split("\n## Modelling\n")[1].split("\n## ")[0]


def test_warehouse_page_modelling_names_the_local_build_and_the_validator():
    section = modelling_section()
    for phrase in (
        "vecten dbt ",
        "vecten dbt-build-local",
        "--check",
        "vecten mcp validate",
        "osi/datasets.json",
        "osi/metrics.json",
        "docs/schema-convention.md",
    ):
        assert phrase in section, phrase
    flat = " ".join(section.split())
    assert "Docker" in flat and "Snowflake" in flat


AGENTIC = REPO / "docs" / "agentic-flow.md"


def test_agentic_flow_page_names_the_loop_and_the_guide():
    flat = " ".join(AGENTIC.read_text().split())
    for phrase in ("vecten next", "vecten guide", "skills/guide/SKILL.md", "plugins/vecten"):
        assert phrase in flat, phrase
    for kind in ("`ask`", "`command`", "`skill`"):
        assert kind in flat, kind
    assert "offline" in flat
    assert "](agent-guide.md)" in flat


def test_agentic_flow_page_gives_the_loop_and_loads_the_plugin():
    flat = " ".join(AGENTIC.read_text().split())
    for phrase in (
        "`vecten next --json`",
        "priority order",
        "`.mcp.json`",
        "`claude --plugin-dir plugins/vecten`",
        "`<path or URL of the checkout>`",
        "`claude plugin install vecten@vecten`",
        "`claude plugin marketplace add vecten/vecten-open`",
    ):
        assert phrase in flat, phrase


def test_readme_installs_the_plugin_from_the_public_marketplace():
    assert "/plugin install vecten --marketplace vecten/vecten-open" in README.read_text()


FIGURE = re.compile(r"\d+(?:\.\d+)?%|\b\d+/\d+\b")


def test_agentic_flow_figures_match_skill_evaluations():
    section = AGENTIC.read_text().split("\n## Why not just Claude Code")[1].split("\n## ")[0]
    quoted = set(FIGURE.findall(section))
    assert quoted, "the page quotes no evaluation figures"
    recorded = set(FIGURE.findall((REPO / "docs" / "skill-evaluations.md").read_text()))
    assert quoted <= recorded, quoted - recorded


def test_cli_command_rows_describe_next_mcp_answer_and_guide():
    table = cli_section("Commands")
    rows = {m[0]: m[1] for m in re.findall(r"^\| `([^`]+)` \| (.+?) \|", table, re.M)}
    assert "git" in rows["next"] and "intake" in rows["next"]
    for phrase in ("`validate`", "`connection`", "`next`"):
        assert phrase in rows["mcp"], phrase
    for phrase in ("`mcp.*`", "`alert_webhook.<env>`", "`none`"):
        assert phrase in rows["answer"], phrase
    for topic in ("`cloud`", "`domain`", "`github`"):
        assert topic in rows["guide"], topic


def test_deploy_aws_page_gives_the_steps_in_order():
    flat = " ".join((REPO / "docs" / "deploy-aws.md").read_text().split())
    steps = [
        "`vecten init --deploy`",
        "`vecten github init`",
        "`vecten bootstrap --env prod`",
        "`vecten secret set <NAME> --env prod --from-local`",
        "`vecten github push`",
        "`vecten deploy --env prod`",
    ]
    at = [flat.find(step) for step in steps]
    assert -1 not in at, dict(zip(steps, at, strict=True))
    assert at == sorted(at), dict(zip(steps, at, strict=True))
    for phrase in ("OIDC", "docs/runbook.md", "`vecten destroy --env prod`"):
        assert phrase in flat, phrase


def test_deploy_scaleway_page_gives_the_steps_in_order():
    flat = " ".join((REPO / "docs" / "deploy-scaleway.md").read_text().split())
    steps = [
        "`vecten init --deploy`",
        "`vecten github init`",
        "`vecten bootstrap --env prod`",
        "`vecten secret set <NAME> --env prod --from-local`",
        "`vecten github push`",
        "`vecten deploy --env prod`",
    ]
    at = [flat.find(step) for step in steps]
    assert -1 not in at, dict(zip(steps, at, strict=True))
    assert at == sorted(at), dict(zip(steps, at, strict=True))
    for phrase in (
        "`scw init`",
        "OIDC",
        "VECTEN_SCW_ACCESS_KEY",
        "database_public_cidrs",
        "docs/runbook.md",
        "`vecten destroy --env prod`",
        "terraform/scaleway",
    ):
        assert phrase in flat, phrase
    assert "dns_project_id" not in flat


def test_cli_command_rows_describe_the_deploy_commands():
    table = cli_section("Commands")
    rows = {
        m[0]: (m[1], m[2]) for m in re.findall(r"^\| `([^`]+)` \| (.+) \| (\S+) \|$", table, re.M)
    }
    for name in ("bootstrap", "deploy", "ship", "destroy"):
        assert rows[name][0].startswith("`--env <env>`"), name
        assert rows[name][1] == "v0.1", name
    assert rows["user"][0].startswith("`add-admin <firstname> --env <env>`")
    assert rows["user"][1] == "v0.1"


def test_cli_page_covers_every_public_leaf_command():
    from .test_cli_contract import iter_commands

    text = CLI.read_text()
    for name, command in iter_commands(get_command(app)):
        if name.startswith("dev ") or getattr(command, "hidden", False):
            continue
        assert f"`vecten {name}" in text, name


def test_semantic_guide_lists_the_expression_allow_list():
    from vecten_shared.osi.expressions import FUNCTIONS

    guide = (REPO / "docs" / "semantic-model.md").read_text()
    section = guide.split("## Allowed functions")[1].split("\n## ")[0]
    assert set(re.findall(r"`([A-Z_]+)`", section)) == set(FUNCTIONS)


def test_semantic_guide_names_every_extension_key_and_value():
    from vecten_shared.osi.validate import ADDITIVITY, GRAINS, ROLLUPS

    guide = (REPO / "docs" / "semantic-model.md").read_text()
    section = guide.split("## The Vecten extension")[1].split("\n## ")[0]
    keys = ["dataset", "additivity", "non_additive_over", "rollup", "verified"]
    keys += ["snapshot_derived", "grain"]
    for word in [*keys, *ADDITIVITY, *ROLLUPS, *GRAINS]:
        assert f"`{word}`" in section, word


def test_docs_index_links_every_page():
    index = (REPO / "docs" / "README.md").read_text()
    pages = sorted(p.name for p in (REPO / "docs").glob("*.md"))
    missing = [
        name
        for name in pages
        if name not in ("README.md", "releasing.md") and f"]({name})" not in index
    ]
    assert missing == []


CONTRIBUTING = REPO / "CONTRIBUTING.md"


def test_contributing_quick_start_runs_from_a_checkout():
    block = CONTRIBUTING.read_text().split("\n## Running from a checkout\n")[1]
    lines = [line.strip() for line in block.split("\n\n")[1].splitlines()]
    assert lines[:2] == ["uv sync --all-packages", "uv run vecten dev build-images"]
    assert lines[2].startswith("uv run vecten init ")
    runs = [line for line in lines if line.startswith("uv run ")]
    assert runs[-2].endswith(" vecten up") and runs[-1].endswith(" vecten down")


def test_contributing_names_the_render_command():
    flat = " ".join(CONTRIBUTING.read_text().split())
    assert "`uv run vecten dev render-plugin --repo .`" in flat
