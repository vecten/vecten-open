"""The evaluation harness's entry point: one case per evaluation, what each brings up, and the
shell environment it prints."""

import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import time
from http.client import HTTPSConnection
from importlib import resources
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import cases, cli, demo_api  # noqa: E402

HARNESS = Path(__file__).parent / "fixtures" / "eval_harness"
SKILLS = resources.files("vecten.knowhow").joinpath("skills")


def eval_ids(skill):
    return [
        e["id"]
        for e in json.loads(SKILLS.joinpath(f"{skill}/evals/evals.json").read_text())["evals"]
    ]


def test_every_evaluation_case_has_a_harness_case():
    expected = {
        f"{skill}-{i}" if len(eval_ids(skill)) > 1 else skill
        for skill in ("connector", "model", "guide", "operate", "semantic")
        for i in eval_ids(skill)
    }
    assert set(cases.CASES) == expected


def test_each_case_names_its_skill_and_evaluation():
    for name, case in cases.CASES.items():
        assert case.eval_id in eval_ids(case.skill), name
        if case.issues:
            assert SKILLS.joinpath(f"operate/evals/grader/{case.issues}.json").is_file()


@pytest.mark.parametrize(
    ("name", "hosts"),
    [
        ("connector", ["api.demo.test"]),
        ("guide-1", ["api.demo.test", "mcp.data.acme.test"]),
        ("guide-2", ["mcp.data.acme.test", "warehouse.acme.test"]),
        ("guide-3", []),
        ("operate-1", []),
    ],
)
def test_the_hosts_a_case_needs(name, hosts):
    assert cases.CASES[name].hostnames() == hosts


def test_guide_2_lists_every_secret_the_human_types():
    names = [s.name for s in cases.CASES["guide-2"].secrets]
    assert names == [
        "WAREHOUSE_ADMIN_PASSWORD",
        "MCP_PROVISIONER_PASSWORD",
        "MCP_IMPERSONATOR_PASSWORD",
    ]
    assert cases.CASES["connector"].secrets[0].value == demo_api.READ_TOKEN


def source(env_file, command, tmp_path):
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("VECTEN_", "EVAL_"))}
    clean["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        ["sh", "-c", f". {env_file} && {command}"],
        env=clean,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


def test_env_writes_a_file_a_shell_sources_to_reach_the_stand_ins(tmp_path, capsys):
    assert cli.main(["env", "guide-1", "--dir", str(tmp_path / "evals")]) == 0
    printed = capsys.readouterr().out
    env_file = tmp_path / "evals" / "guide-1" / "env.sh"
    assert f". {env_file}" in printed
    found = source(
        env_file, "gh auth status && python -c 'import vecten_mock_cloud' && env", tmp_path
    )
    assert found.returncode == 0, found.stderr
    assert "VECTEN_CONTROL_PLANE=vecten_mock_cloud:MockCloud" in found.stdout
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        assert f"{name}=" in found.stdout
    assert f"VECTEN_MOCK_CLOUD_DIR={tmp_path / 'evals' / 'guide-1' / 'cloud'}" in found.stdout


def test_env_sh_gives_each_case_its_own_config_home(tmp_path, capsys):
    """`vecten` keeps its MCP sessions under XDG_CONFIG_HOME; a shared one leaks a sign-in."""
    for base in (tmp_path / "a", tmp_path / "b"):
        assert cli.main(["env", "guide-3", "--dir", str(base)]) == 0
        found = source(base / "guide-3" / "env.sh", "env", tmp_path)
        assert f"XDG_CONFIG_HOME={base / 'guide-3' / 'config'}" in found.stdout


def test_the_operate_environment_puts_the_issues_replay_first(tmp_path, capsys):
    assert cli.main(["env", "operate-2", "--dir", str(tmp_path / "evals")]) == 0
    env_file = tmp_path / "evals" / "operate-2" / "env.sh"
    found = source(env_file, "cd / && vecten issues --env prod --json", tmp_path)
    doc = json.loads(found.stdout)
    assert [i["kind"] for i in doc["issues"]] == ["command", "ask"]


def test_an_operate_case_builds_its_project_and_names_it(tmp_path, capsys):
    assert cli.main(["env", "operate-1", "--dir", str(tmp_path / "evals")]) == 0
    printed = capsys.readouterr().out
    root = tmp_path / "evals" / "operate-1" / "proj"
    assert (root / "connectors" / "hubspot" / "connector.py").is_file()
    assert f"Start the agent in {root}" in printed
    env_file = tmp_path / "evals" / "operate-1" / "env.sh"
    found = source(env_file, "env", tmp_path)
    upstream = tmp_path / "evals" / "operate-1" / "upstream" / "deals.json"
    assert f"HUBSPOT_FIXTURE_FILE={upstream}" in found.stdout


@pytest.mark.parametrize("name", ["guide-1", "guide-2"])
def test_the_guide_cases_sign_in_the_product_s_way(tmp_path, capsys, name):
    """The guide evaluations test the first sign-in, so no harness password stands in for it."""
    case = cases.CASES[name]
    assert case.harness_passwords is False
    cli.print_instructions(case, cli.Paths(tmp_path / "evals", name))
    printed = capsys.readouterr().out
    assert cases.SIGN_IN_PASSWORD not in printed
    assert "eval-ada-sign-in-0b7e" in printed and "eval-grace-sign-in-4c1d" in printed


def test_the_semantic_cases_keep_the_harness_password(tmp_path, capsys):
    case = cases.CASES["semantic-1"]
    assert case.harness_passwords is True
    cli.print_instructions(case, cli.Paths(tmp_path / "evals", "semantic-1"))
    assert cases.SIGN_IN_PASSWORD in capsys.readouterr().out


def test_the_instructions_name_the_root_steps_and_the_secrets(tmp_path, capsys):
    cli.main(["env", "guide-2", "--dir", str(tmp_path / "evals")])
    cli.print_instructions(cases.CASES["guide-2"], cli.Paths(tmp_path / "evals", "guide-2"))
    text = capsys.readouterr().out
    assert "sudo tee -a /etc/hosts" in text
    assert "127.0.0.1 mcp.data.acme.test warehouse.acme.test" in text
    assert "vecten secret set WAREHOUSE_ADMIN_PASSWORD" in text
    assert "provision-warehouse" in text
    assert cases.ADA_PASSWORD in text and cases.SIGN_IN_PASSWORD not in text


def test_a_case_without_servers_exits_after_writing_its_environment(tmp_path):
    result = subprocess.run(
        [sys.executable, str(HARNESS), "up", "guide-3", "--dir", str(tmp_path / "evals")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "evals" / "guide-3" / "env.sh").is_file()
    assert "nothing to keep running" in result.stdout


def test_up_serves_the_demo_api_until_interrupted(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            str(HARNESS),
            "up",
            "connector",
            "--dir",
            str(tmp_path / "evals"),
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        lines = []
        deadline = time.time() + 60
        while time.time() < deadline:
            line = process.stdout.readline()
            lines.append(line)
            if "serving" in line or process.poll() is not None:
                break
        assert any("serving" in line for line in lines), "".join(lines)
        ca = tmp_path / "evals" / "ca" / "ca.pem"
        context = ssl.create_default_context(cafile=str(ca))
        connection = HTTPSConnection("127.0.0.1", port, context=context)
        connection.sock = context.wrap_socket(
            socket.create_connection(("127.0.0.1", port)), server_hostname="api.demo.test"
        )
        try:
            connection.request(
                "GET",
                "/v1/deals?limit=1",
                headers={"Host": "api.demo.test", "Authorization": f"Bearer {demo_api.READ_TOKEN}"},
            )
            assert connection.getresponse().status == 200
        finally:
            connection.close()
    finally:
        process.send_signal(signal.SIGINT)
        out, _ = process.communicate(timeout=30)
    assert process.returncode == 0, out


def test_a_privileged_port_hint_names_the_way_each_system_allows_it():
    darwin = cli.privileged_port_hint(443, "Darwin")
    assert "--port 9443" in darwin and "pfctl" in darwin and "sysctl" not in darwin
    linux = cli.privileged_port_hint(443, "Linux")
    assert "net.ipv4.ip_unprivileged_port_start=443" in linux


def test_the_operate_notes_say_to_run_one_case_at_a_time():
    notes = " ".join(cases.OPERATE_NOTES)
    assert "one operate run at a time" in notes and "vecten down" in notes


RUNBOOK = Path(__file__).parents[3] / "docs/skill-evaluations.md"


def test_the_runbook_names_only_harness_commands_that_exist():
    section = RUNBOOK.read_text()
    parser_commands = cli.command_names()
    named = set(re.findall(r"eval_harness ([a-z][a-z-]+)", section))
    assert named, "the runbook names no harness command"
    assert named <= parser_commands, named - parser_commands
    for command in ("asks", "reply", "bundle", "brief", "teardown", "report"):
        assert f"eval_harness {command}" in section, command
