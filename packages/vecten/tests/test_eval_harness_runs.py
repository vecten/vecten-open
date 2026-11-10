"""One evaluation run: its folder, the logged `run` wrapper an agent's commands go through, the
`human` channel to the person, and teardown."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import cases, cli, runs  # noqa: E402


def make_run(tmp_path, case="guide-1"):
    run = runs.Run(tmp_path / "runs dir" / "r1", case)
    run.env_file.parent.mkdir(parents=True)
    run.env_file.write_text("export EVAL_MARK=1\n")
    runs.write_helpers(run, cases.CASES[case])
    return run


def wrapped(run, command, cwd, **kwargs):
    return subprocess.run(
        [str(run.base / "run"), command], cwd=cwd, capture_output=True, text=True, **kwargs
    )


def test_run_logs_each_command_its_output_and_exit_code(tmp_path):
    run = make_run(tmp_path)
    first = wrapped(run, "echo 'a b' | tr a x && exit 4", tmp_path)
    assert first.returncode == 4
    assert "x b" in first.stdout and "[exit 4]" in first.stdout
    assert (run.log / "001.cmd").read_text() == "echo 'a b' | tr a x && exit 4\n"
    assert "x b" in (run.log / "001.out").read_text()
    assert (run.log / "001.rc").read_text().strip() == "4"
    second = wrapped(run, "pwd; echo $EVAL_MARK", tmp_path)
    assert second.returncode == 0
    lines = (run.log / "002.out").read_text().splitlines()
    assert Path(lines[0]).resolve() == run.project.resolve() and lines[1] == "1"


def test_run_gives_the_command_no_standard_input(tmp_path):
    run = make_run(tmp_path)
    result = wrapped(run, "cat", tmp_path, input="hi")
    assert result.returncode == 0
    assert (run.log / "001.out").read_text() == ""


def test_guide_1_copies_the_demo_document_once_the_project_exists(tmp_path):
    run = make_run(tmp_path)
    wrapped(run, "true", tmp_path)
    assert not (run.project / "docs" / "demo-api.md").exists()
    wrapped(run, "mkdir -p docs && touch vecten.toml", tmp_path)
    copied = run.project / "docs" / "demo-api.md"
    assert copied.read_text() == runs.DEMO_API_DOC.read_text()
    assert "docs/demo-api.md" in (run.log / "harness.log").read_text()
    assert "demo-api" not in (run.log / "002.out").read_text()


def test_other_cases_leave_the_docs_to_the_agent(tmp_path):
    run = make_run(tmp_path, "guide-2")
    wrapped(run, "mkdir -p docs && touch vecten.toml", tmp_path)
    assert not (run.project / "docs" / "demo-api.md").exists()


def test_rewriting_helpers_keeps_the_log(tmp_path):
    run = make_run(tmp_path)
    wrapped(run, "true", tmp_path)
    runs.write_helpers(run, cases.CASES["guide-1"])
    assert (run.log / "001.cmd").is_file()


def test_env_with_run_lays_out_the_run(tmp_path, capsys):
    assert cli.main(["env", "guide-3", "--run", "r2", "--runs", str(tmp_path)]) == 0
    base = tmp_path / "r2"
    for name in ("run", "human", "prompt.md", "bin/mcp-client", "guide-3/env.sh"):
        assert (base / name).is_file(), name
    for name in ("log", "inbox", "bin", "guide-3/proj"):
        assert (base / name).is_dir(), name
    assert str(base / "run") in capsys.readouterr().out


def test_a_run_and_a_base_are_exclusive(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["env", "guide-3", "--dir", str(tmp_path), "--run", "r2"])
    assert stopped.value.code == 2


def test_runs_default_to_a_folder_the_checkout_ignores():
    assert runs.DEFAULT_RUNS == runs.CHECKOUT / ".eval-runs"
    ignored = (runs.CHECKOUT / ".gitignore").read_text().splitlines()
    assert ".eval-runs/" in ignored


def wait_for(path, timeout=10):
    deadline = time.time() + timeout
    while not path.exists():
        assert time.time() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def test_human_waits_for_the_reply(tmp_path):
    run = make_run(tmp_path)
    asking = subprocess.Popen(
        [str(run.base / "human"), "Please run the provisioner script"],
        stdout=subprocess.PIPE,
        text=True,
    )
    wait_for(run.inbox / "001.ask")
    assert (run.inbox / "001.ask").read_text() == "Please run the provisioner script\n"
    runs.reply(run, "001", "our administrator ran it")
    out, _ = asking.communicate(timeout=10)
    assert asking.returncode == 0 and out == "our administrator ran it\n"
    second = subprocess.Popen([str(run.base / "human"), "again"], stdout=subprocess.DEVNULL)
    wait_for(run.inbox / "002.ask")
    second.kill()
    second.wait()


def test_human_names_the_wait_command_when_no_reply_comes(tmp_path):
    run = make_run(tmp_path)
    env = {**os.environ, "EVAL_HARNESS_HUMAN_POLLS": "1"}
    first = subprocess.run(
        [str(run.base / "human"), "hello"], capture_output=True, text=True, env=env
    )
    assert first.returncode == 3
    assert f"{run.base / 'human'} --wait 001" in first.stdout
    runs.reply(run, "001", "done")
    again = subprocess.run(
        [str(run.base / "human"), "--wait", "001"], capture_output=True, text=True, env=env
    )
    assert again.returncode == 0 and again.stdout == "done\n"


def ask(run, number, text, reply=None):
    (run.inbox / f"{number}.ask").write_text(text + "\n")
    if reply is not None:
        runs.reply(run, number, reply)


def test_asks_lists_unanswered_messages_across_runs(tmp_path, capsys):
    one = make_run(tmp_path)
    two = runs.Run(one.base.parent / "r2", "guide-2")
    two.env_file.parent.mkdir(parents=True)
    runs.write_helpers(two, cases.CASES["guide-2"])
    ask(one, "001", "first\nquestion", reply="yes")
    ask(two, "001", "second")
    found = runs.asks(one.base.parent)
    assert [(a.run, a.number, a.reply) for a in found] == [
        ("r1", "001", "yes\n"),
        ("r2", "001", None),
    ]
    assert cli.main(["asks", "--runs", str(one.base.parent)]) == 0
    assert capsys.readouterr().out == "ASK r2 001: second\n"


def test_follow_emits_each_ask_once(tmp_path):
    run = make_run(tmp_path)
    ask(run, "001", "first")
    seen = []

    def sleep(_seconds):
        if not (run.inbox / "002.ask").exists():
            ask(run, "002", "second")

    runs.follow_asks(run.base.parent, seen.append, rounds=3, sleep=sleep)
    assert [a.number for a in seen] == ["001", "002"]


def test_reply_to_an_unknown_message_is_refused(tmp_path):
    run = make_run(tmp_path)
    with pytest.raises(ValueError):
        runs.reply(run, "007", "done")


def test_teardown_runs_down_with_volumes_outside_the_log(tmp_path):
    run = make_run(tmp_path)
    stand_in = tmp_path / "bin"
    stand_in.mkdir()
    calls = tmp_path / "calls"
    (stand_in / "vecten").write_text(f'#!/bin/sh\necho "$*" >> {calls}\necho stopped\n')
    (stand_in / "vecten").chmod(0o755)
    environ = {**os.environ, "PATH": f"{stand_in}:{os.environ['PATH']}"}
    assert runs.teardown(run, environ) == 0
    assert not calls.exists()
    (run.project / "vecten.toml").touch()
    assert runs.teardown(run, environ) == 0
    assert calls.read_text() == "down --volumes\n"
    assert "stopped" in (run.log / "teardown.out").read_text()
    assert not list(run.log.glob("*.cmd"))
