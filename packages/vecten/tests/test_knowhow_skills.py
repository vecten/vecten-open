import json
import re
import shlex
import subprocess
import zipfile
from importlib import resources
from pathlib import Path

import pytest
import typer
import yaml

from vecten.cli.app import app
from vecten.intake.answers import ANSWERS_FILE
from vecten.knowhow import load_skill, skill_body, skill_frontmatter, skill_names
from vecten.knowhow.mcp_servers import mcp_servers_document
from vecten.knowhow.render import (
    GUIDE_DOCUMENT,
    MARKETPLACE_DESCRIPTION,
    MARKETPLACE_FILE,
    PLUGIN_DESCRIPTION,
    PLUGIN_DIR,
    PUBLIC_URL,
    guide_document,
    render_guide_document,
    render_plugin,
)
from vecten.next.deploy import (
    deploy_gate_items,
    deploy_prerequisite_items,
    deploy_question_items,
)
from vecten.next.existing_warehouse import existing_warehouse_items
from vecten.next.gitstate import GitState
from vecten.next.ingestion import ingestion_items
from vecten.next.intake import intake_items
from vecten.next.items import Item
from vecten.next.modelling import modelling_items
from vecten.next.post_deploy import ADMIN, OIDC, post_deploy_items
from vecten.next.semantic import semantic_items
from vecten.next.stack import stack_items
from vecten_shared.config import AccessDecl, ConnectorDecl, load_config
from vecten_shared.versions import VERSIONS, minor_constraint

from . import test_next_assemble, test_next_deploy, test_next_project
from .fixture_project import render_fixture
from .test_next_deploy import render_mcp_only_deploy
from .test_next_project import POSTGRES_URL, SNOWFLAKE_URL, render_mcp_only

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / PLUGIN_DIR
MAX_BODY_LINES = 400
INLINE_COMMAND = re.compile(r"`(vecten [^`]+)`")
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
CREDENTIAL_LITERAL = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)[\"']?\s*[=:]\s*[\"'][^\"'\s]{6,}[\"']"
)
EVAL_KEYS = {"id", "prompt", "expected_output", "files", "expectations"}
ENV_REFERENCE = re.compile(r"\$\{[^}]*\}")
SUPPORTED_EXPANSION = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*(:-[^}]*)?\}$")
PATH_SUFFIX = re.compile(r"^(/[A-Za-z0-9_.-]+)*$")
# Resolutions `next` reports before the CLI registers their commands, keyed to the task that
# lands each. The guard test fails as soon as one registers, so the option check covers it then.
PLANNED_COMMANDS: dict[tuple[str, ...], str] = {}


def _skill_dir(name: str):
    return resources.files("vecten.knowhow").joinpath("skills", name)


def _commands_named(body: str) -> list[str]:
    named = INLINE_COMMAND.findall(body)
    for block in FENCE.findall(body):
        named += [line.strip() for line in block.splitlines() if line.strip().startswith("vecten ")]
    return named


def _unknown_parts(root, line: str) -> list[str]:
    """The words and options of one `vecten ...` line that the CLI does not register."""
    words = shlex.split(line.replace("<", "").replace(">", ""))[1:]
    command = root
    problems = []
    value_next = False
    for word in words:
        if value_next:
            value_next = False
            continue
        if word.startswith("-"):
            if command.context_settings.get("ignore_unknown_options"):
                break
            option = word.split("=", 1)[0]
            params = {opt: p for p in command.params for opt in (*p.opts, *p.secondary_opts)}
            if option not in params and option != "--help":
                problems.append(f"{line}: {command.name} has no option {option}")
            # A group's own option may precede its subcommand, so its value is not a command.
            param = params.get(option)
            value_next = "=" not in word and param is not None and not param.is_flag
        # Typer builds its groups on its own vendored click, so a group is known by its commands.
        elif hasattr(command, "commands"):
            if word not in command.commands:
                problems.append(f"{line}: {command.name or 'vecten'} has no command {word}")
                break
            command = command.commands[word]
    return problems


def _planned(line: str) -> bool:
    words = tuple(shlex.split(line.replace("<", "").replace(">", ""))[1:])
    return any(words[: len(path)] == path for path in PLANNED_COMMANDS)


def test_skill_names_include_connector_model_and_guide():
    names = skill_names()
    assert {"connector", "model", "guide"} <= set(names)
    assert names == sorted(names)


@pytest.mark.parametrize("name", skill_names())
def test_frontmatter_names_the_skill_and_when_to_use_it(name):
    meta = skill_frontmatter(name)
    assert meta["name"] == name
    assert len(meta["description"]) > 80 and "vecten" in meta["description"]


@pytest.mark.parametrize("name", skill_names())
def test_body_is_the_file_without_frontmatter(name):
    body = skill_body(name)
    assert not body.startswith(("---", "\n")) and body.startswith("# ")
    assert load_skill(name).endswith(body)
    assert len(body.splitlines()) < MAX_BODY_LINES


@pytest.mark.parametrize("name", skill_names())
def test_every_vecten_command_in_a_body_is_registered(name):
    root = typer.main.get_command(app)
    named = _commands_named(skill_body(name))
    assert named, name
    problems = [p for line in named if not _planned(line) for p in _unknown_parts(root, line)]
    assert not problems, problems


def test_planned_commands_are_not_registered_yet():
    root = typer.main.get_command(app)
    registered = []
    for path, task in PLANNED_COMMANDS.items():
        command = root
        for word in path:
            command = getattr(command, "commands", {}).get(word)
            if command is None:
                break
        if command is not None:
            registered.append(f"vecten {' '.join(path)} (tasks.md {task})")
    assert not registered, f"registered now, drop from PLANNED_COMMANDS: {registered}"


@pytest.mark.parametrize("name", skill_names())
def test_no_body_carries_a_credential_literal(name):
    assert not CREDENTIAL_LITERAL.search(skill_body(name))


def _secret_set_values(line: str) -> list[str]:
    """Positional words after `vecten secret set <NAME>`: a value typed on the command line."""
    words = shlex.split(line.replace("<", "").replace(">", ""))
    if words[1:3] != ["secret", "set"]:
        return []
    command = typer.main.get_command(app).commands["secret"].commands["set"]
    takes_value = {o for p in command.params if not getattr(p, "is_flag", True) for o in p.opts}
    positional, rest = [], iter(words[3:])
    for word in rest:
        if word.startswith("-"):
            if word in takes_value:
                next(rest, None)
        else:
            positional.append(word)
    return positional[1:]


@pytest.mark.parametrize("name", skill_names())
def test_no_secret_set_line_carries_a_value(name):
    lines = _commands_named(skill_body(name))
    assert not [line for line in lines if _secret_set_values(line)]


def test_secret_value_check_catches_a_value():
    assert _secret_set_values("vecten secret set DEMO_API_KEY") == []
    assert _secret_set_values("vecten secret set DEMO_API_KEY --env prod") == []
    assert _secret_set_values("vecten secret set DEMO_API_KEY abc123")


@pytest.mark.parametrize("name", skill_names())
def test_evals_follow_the_skill_creator_schema(name):
    skill_dir = _skill_dir(name)
    doc = json.loads(skill_dir.joinpath("evals", "evals.json").read_text(encoding="utf-8"))
    assert doc["skill_name"] == name
    assert doc["evals"]
    ids = [case["id"] for case in doc["evals"]]
    assert len(ids) == len(set(ids)) and all(isinstance(i, int) for i in ids)
    for case in doc["evals"]:
        assert EVAL_KEYS <= case.keys(), case.get("id")
        assert case["prompt"].strip() and case["expected_output"].strip()
        assert case["expectations"] and all(isinstance(e, str) for e in case["expectations"])
        for path in case["files"]:
            assert skill_dir.joinpath(*path.split("/")).is_file(), path


def test_wheel_ships_the_skills_without_their_evals(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--package", "vecten", "--out-dir", str(tmp_path)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = tmp_path.glob("vecten-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    for name in skill_names():
        assert f"vecten/knowhow/skills/{name}/SKILL.md" in names, name
    assert not [n for n in names if n.startswith("vecten/knowhow/skills/") and "/evals/" in n]


def test_unknown_skill_is_refused():
    for loader in (load_skill, skill_body, skill_frontmatter):
        with pytest.raises(ValueError):
            loader("../knowhow")


def _item_commands(items) -> list[str]:
    """Every `vecten ...` command an item's resolution or `show_first` names, one per `&&` part."""
    commands = []
    for item in items:
        for text in (item.resolution, *item.show_first):
            parts = (part.strip() for part in text.split("&&"))
            commands += [part for part in parts if part.startswith("vecten ")]
    return commands


def _items_of(module) -> list[Item]:
    found = []
    for value in vars(module).values():
        values = value.values() if isinstance(value, dict) else [value]
        found += [v for v in values if isinstance(v, Item)]
    return found


def _staged_platform_items(tmp_path) -> list[Item]:
    root = render_fixture(tmp_path / "platform", "postgres", deploy=True)
    config = load_config(root)
    config.wanted = ["hubspot"]
    config.connectors.append(ConnectorDecl(name="ghost", source="custom"))
    connector = root / "connectors" / "demo"
    connector.mkdir(parents=True, exist_ok=True)
    (connector / "connector.py").touch()
    fixtures = connector / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "companies.jsonl").touch()
    items = [*stack_items(root, config), *ingestion_items(root, config)]
    fixtures.rename(connector / "recorded")
    items += ingestion_items(root, config)
    items += modelling_items(root, config) + semantic_items(root, config)
    no_layer = config.model_copy(update={"deploy": None})
    items += deploy_question_items(root, no_layer, {"cloud": "none"})
    items += deploy_question_items(root, no_layer, test_next_deploy.DEPLOY_ANSWERS)
    (root / ".env").write_text("HUBSPOT_API_KEY=x\n")
    dirty = GitState(has_repo=True, has_origin=True, dirty=True, ahead=True)
    items += deploy_prerequisite_items(root, config, "prod", dirty)
    items += deploy_gate_items(config, "prod")
    config.connector("demo").access = AccessDecl(groups=["sales"])
    config.environments["prod"].deployed_commit = "abc123"
    items += post_deploy_items(root, config, "prod", dirty)
    return items


def _staged_mcp_only_items(tmp_path) -> list[Item]:
    items: list[Item] = []
    for url in (POSTGRES_URL, SNOWFLAKE_URL):
        root = render_mcp_only(tmp_path / url.split(":")[0], url)
        config = load_config(root)
        items += semantic_items(root, config) + existing_warehouse_items(root, config, "prod")
        items += deploy_question_items(root, config, {"cloud": "none"})
        root = render_mcp_only_deploy(tmp_path / f"{url.split(':')[0]}-deploy", url)
        config = load_config(root)
        clean = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)
        items += deploy_prerequisite_items(root, config, "prod", clean)
    return items


def test_every_command_next_can_emit_is_registered_or_planned(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ANSWERS_FILE).write_text(
        yaml.safe_dump({"has_warehouse": "no", "sources": [], "warehouse": "postgres"})
        + yaml.safe_dump({"name": "acme", "users": 3})
    )
    items = [*intake_items(empty)]
    for module in (test_next_deploy, test_next_assemble, test_next_project):
        items += _items_of(module)
    items += _staged_platform_items(tmp_path) + _staged_mcp_only_items(tmp_path)
    commands = sorted(set(_item_commands(items)))
    for expected in (
        "vecten init",
        "vecten up",
        "vecten connector add ghost",
        "vecten connector run demo --record",
        "vecten bootstrap --env prod",
        "vecten group add sales --env prod",
        "vecten secret set MCP_PROVISIONER_PASSWORD --env prod --from-local",
        "vecten secret set MCP_PROVISIONER_PASSWORD",
        "vecten mcp provisioner-script",
        "vecten estimate --size small --components platform,mcp --warehouse postgres",
        "vecten estimate --size small --components mcp --warehouse snowflake",
        "vecten github push",
    ):
        assert expected in commands, expected
    root = typer.main.get_command(app)
    problems = [p for line in commands if not _planned(line) for p in _unknown_parts(root, line)]
    assert not problems, problems


def test_item_command_check_catches_a_misspelt_flag():
    root = typer.main.get_command(app)
    item = Item("command", "t", "vecten github push && vecten connector run demo --recrod")
    lines = _item_commands([item])
    assert lines == ["vecten github push", "vecten connector run demo --recrod"]
    assert [p for line in lines for p in _unknown_parts(root, line)]


def test_command_check_catches_a_wrong_option():
    root = typer.main.get_command(app)
    assert _unknown_parts(root, "vecten connector run demo --record") == []
    assert _unknown_parts(root, "vecten connector run demo --objects x")
    assert _unknown_parts(root, "vecten connector launch demo")
    assert _unknown_parts(root, "vecten dbt build --select x") == []


def test_mcp_servers_document_pins_both_servers():
    servers = mcp_servers_document()["mcpServers"]
    assert set(servers) == {"airflow", "dbt"}
    airflow, dbt = servers["airflow"], servers["dbt"]
    assert airflow["command"] == dbt["command"] == "uvx"
    airflow_spec = f"astro-airflow-mcp{minor_constraint(VERSIONS.airflow_mcp_server)}"
    assert airflow["args"] == [airflow_spec, "--transport", "stdio"]
    assert dbt["args"] == [f"dbt-mcp{minor_constraint(VERSIONS.dbt_mcp)}"]
    assert airflow["env"] == {
        "AIRFLOW_API_URL": "${AIRFLOW_API_URL:-http://127.0.0.1:8482}",
        "AIRFLOW_USERNAME": "${AIRFLOW_USERNAME:-admin}",
        "AIRFLOW_PASSWORD": "${AIRFLOW_PASSWORD}",
    }
    assert dbt["env"] == {
        "DBT_PROJECT_DIR": "${CLAUDE_PROJECT_DIR:-.}/dbt",
        "DBT_PATH": "${DBT_PATH}",
    }
    assert json.loads(json.dumps(mcp_servers_document())) == mcp_servers_document()


def test_mcp_json_uses_only_supported_expansion():
    for name, server in mcp_servers_document()["mcpServers"].items():
        for key, value in server["env"].items():
            references = ENV_REFERENCE.findall(value)
            assert references, (name, key, value)
            assert all(SUPPORTED_EXPANSION.match(r) for r in references), (name, key, value)
            assert PATH_SUFFIX.match(ENV_REFERENCE.sub("", value)), (name, key, value)


def test_expansion_check_catches_a_value():
    assert not SUPPORTED_EXPANSION.match("${airflow_password}")
    assert not SUPPORTED_EXPANSION.match("${AIRFLOW_PASSWORD:?missing}")
    assert SUPPORTED_EXPANSION.match("${AIRFLOW_PASSWORD:-}")
    assert not PATH_SUFFIX.match(ENV_REFERENCE.sub("", "s3cret${PWD}"))


def test_guide_dispatches_to_the_vendored_skill_paths():
    body = skill_body("guide")
    for name in ("connector", "model", "semantic"):
        assert f"skills/{name}/SKILL.md" in body, name


def test_guide_names_every_post_deploy_answer_key():
    body = skill_body("guide")
    keys = [*OIDC.answer_keys, *ADMIN.answer_keys, "mcp.auth", "alert_webhook.prod"]
    assert [k for k in keys if k not in body] == []
    assert "vecten deploy --env prod" in body


def test_guide_evals_cover_both_branches_and_start_empty():
    text = _skill_dir("guide").joinpath("evals", "evals.json").read_text(encoding="utf-8")
    cases = json.loads(text)["evals"]
    assert len(cases) == 3
    full, existing, empty = (" ".join(case["expectations"]) for case in cases)
    assert "vecten answer has_warehouse no" in full and "vecten deploy --env prod" in full
    assert '"yes"' in full and "vecten user add" in full and "vecten mcp connection" in full
    assert "vecten answer has_warehouse yes" in existing and "vecten mcp introspect" in existing
    assert "WAREHOUSE_ADMIN_PASSWORD" in existing and "provisioner-script" in existing
    assert "vecten grant --schema raw_demo --group sales --env prod" in full
    assert "start empty" in empty and "Validate the semantic model" in empty


def test_guide_evals_continue_to_a_connected_client_and_a_compiled_answer():
    root = _skill_dir("guide").joinpath("evals")
    cases = json.loads(root.joinpath("evals.json").read_text(encoding="utf-8"))["evals"]
    human = root.joinpath("files", "scripted-human.md").read_text(encoding="utf-8")
    for case in cases[:2]:
        expectations = " ".join(case["expectations"])
        assert "connected an MCP client" in expectations, case["id"]
        assert "`query_metrics`" in expectations and "`sql-exec`" in expectations, case["id"]
        assert "vecten/receipt" in expectations, case["id"]
        assert "grace@acme.test" in case["expected_output"], case["id"]
    assert "connected an MCP client" not in " ".join(cases[2]["expectations"])
    assert human.count("After the empty `next`, as Grace") == 2
    assert "VECTEN_CONTROL_PLANE=vecten_mock_cloud:MockCloud" in human


def test_guide_evals_sign_in_with_a_password_link():
    root = _skill_dir("guide").joinpath("evals")
    cases = json.loads(root.joinpath("evals.json").read_text(encoding="utf-8"))["evals"]
    human = root.joinpath("files", "scripted-human.md").read_text(encoding="utf-8")
    for case in cases[:2]:
        expectations = " ".join(case["expectations"])
        steps = [
            "vecten answer mcp.auth password",
            "vecten answer mcp.admins ada@acme.test",
            "vecten mcp admin-link --env prod",
            "vecten login --env prod",
            "vecten user add --email grace@acme.test",
            "vecten user password-link",
        ]
        assert [step for step in steps if step not in expectations] == [], case["id"]
        positions = [expectations.index(step) for step in steps]
        assert positions == sorted(positions), case["id"]
    assert "login.acme.test" not in human
    assert "login.acme.test" not in root.joinpath("evals.json").read_text(encoding="utf-8")
    assert human.count("eval-ada-sign-in-0b7e") == 2


@pytest.mark.parametrize(
    "phrase",
    [
        'Copy `deploy/envs/prod.tfvars` to `deploy/envs/dev.tfvars`, set `environment = "dev"`',
        "Record the order with `vecten answer deploy.promotion dev,prod`",
        "`vecten answer deploy.promotion none` removes the order",
        "Exit 4 naming `vecten update` means the project's workflows predate promotion",
        "it now walks `dev` first",
        "It ships the commit dev shipped last.",
        "run `vecten promote --env <next env>` only on their explicit yes",
        "`vecten promote` without their yes to promoting",
        "`next` never asks about an order",
        "as the way on, once the next environment is deployed",
    ],
)
def test_guide_walks_the_promotion_order(phrase):
    assert phrase in flat("guide")


def test_guide_tells_how_a_password_user_gets_a_link():
    body = skill_body("guide")
    assert "vecten user password-link" in body and "vecten mcp admin-link --env prod" in body


def test_guide_says_next_reports_the_password_link():
    body = skill_body("guide")
    bullet = body[body.index("Give <email> a password link") :].split("\n- ", 1)[0]
    assert "15 minutes" in " ".join(bullet.split())


@pytest.mark.parametrize("name", skill_names())
def test_plugin_skill_is_the_package_file(name):
    assert (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8") == load_skill(name)
    assert not (PLUGIN / "skills" / name / "evals").exists()


def test_plugin_carries_no_mcp_servers():
    assert not (PLUGIN / ".mcp.json").exists()


def test_plugin_description_names_every_skill():
    assert PLUGIN_DESCRIPTION.startswith("Build and run a Vecten Open data platform")
    for name in skill_names():
        assert name in PLUGIN_DESCRIPTION, name
    assert "MCP" not in PLUGIN_DESCRIPTION


def test_plugin_layout_lists_every_skill():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "vecten" and manifest["version"] == VERSIONS.vecten
    assert manifest["license"] == "Apache-2.0" and manifest["author"] == {"name": "Vecten"}
    assert manifest["homepage"] == manifest["repository"] == PUBLIC_URL
    assert manifest["description"] == PLUGIN_DESCRIPTION
    assert manifest["displayName"] == "Vecten Open" and manifest["keywords"]
    assert sorted(p.name for p in PLUGIN.iterdir()) == [".claude-plugin", "skills"]
    dirs = sorted(p.name for p in (PLUGIN / "skills").iterdir())
    assert dirs == skill_names()
    for name in dirs:
        text = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---\n")[1]
        assert yaml.safe_load(frontmatter)["name"] == name


def test_marketplace_lists_the_plugin():
    doc = json.loads((REPO / MARKETPLACE_FILE).read_text(encoding="utf-8"))
    assert doc["name"] == "vecten" and doc["owner"] == {"name": "Vecten"}
    assert doc["description"] == MARKETPLACE_DESCRIPTION
    assert doc["plugins"] == [
        {
            "name": "vecten",
            "source": "./plugins/vecten",
            "description": PLUGIN_DESCRIPTION,
            "version": VERSIONS.vecten,
            "homepage": PUBLIC_URL,
            "repository": PUBLIC_URL,
        }
    ]
    assert (REPO / doc["plugins"][0]["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_guide_document_is_the_skill_body():
    text = (REPO / GUIDE_DOCUMENT).read_text(encoding="utf-8")
    assert text == guide_document()
    assert text.startswith("<!-- Generated by `vecten dev render-plugin`")
    assert len(re.findall(r"^# ", text, re.M)) == 1
    assert text.split("-->\n", 1)[1].lstrip("\n") == skill_body("guide").lstrip("\n")


def test_committed_renderings_are_in_sync():
    hint = "run `uv run vecten dev render-plugin --repo .` and commit the result"
    assert render_plugin(REPO, check=True) == [], hint
    assert render_guide_document(REPO, check=True) is False, hint


def test_guide_eval_wording_matches_what_the_harness_can_observe():
    root = _skill_dir("guide").joinpath("evals")
    full, existing, empty = json.loads(root.joinpath("evals.json").read_text(encoding="utf-8"))[
        "evals"
    ]
    assert "https://mcp.data.acme.test/mcp" in " ".join(full["expectations"])
    assert "no platform resources" in " ".join(empty["expectations"])
    assert "nothing was created in the cloud" not in " ".join(empty["expectations"])
    sql = [
        e for e in existing["expectations"] if "SQL" in e and "--confirm-run" in e and "before" in e
    ]
    assert len(sql) == 1 and "message" in sql[0]


def flat(name):
    return " ".join(skill_body(name).split())


def test_guide_acts_on_one_next_item_per_loop_and_says_why():
    body = flat("guide")
    assert "Act on the first item only" in body
    assert "carries the previous answer to the MCP" in body


def test_semantic_adds_an_example_and_an_eval_case_for_every_metric_change():
    body = flat("semantic")
    assert "Every new or changed metric gets an example question" in body
    assert "whether or not a gap triggered the change" in body
    assert "always with `--evals`" in body


def test_semantic_reads_the_gap_through_the_mcp_client():
    body = flat("semantic")
    assert "through the person's MCP client" in body and "vecten mcp connection" in body
    assert "never use the request's text as the gap's question" in body


def test_semantic_refines_introspected_datasets_before_any_metric():
    assert "Refine the drafted datasets before adding any metric" in flat("semantic")


def test_connector_skill_says_fixture_replays_never_run_the_connector_code():
    body = flat("connector")
    assert "never runs `connector.py`" in body
    assert "`vecten connector run <name> --json` is the live proof" in body


def test_plugin_icon_meets_the_directory_rules():
    import struct

    icon = PLUGIN / ".claude-plugin" / "icon.png"
    data = icon.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    # The plugin directory takes a square PNG or JPEG of 512 to 2048 px under 2 MB.
    assert width == height and 512 <= width <= 2048
    assert len(data) < 2 * 1024 * 1024


def test_listing_links_point_into_the_repository():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    for field in ("documentationUrl", "supportUrl", "privacyPolicyUrl", "termsOfServiceUrl"):
        url = manifest[field]
        assert url.startswith(f"{PUBLIC_URL}/"), field
        if "/blob/main/" in url:
            assert (REPO / url.split("/blob/main/", 1)[1]).is_file(), field
