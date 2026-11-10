"""What the agent of a run is told beside its skill-creator prompt, and what its grader is told."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import cases, cli, prompts, runs  # noqa: E402

ACTION_RULE = (
    "goes through the human helper even when the scripted file lists the reply that follows it"
)


def run_for(tmp_path, name):
    return runs.Run(tmp_path / "runs" / "r1", name)


def test_the_guide_2_prompt_names_the_run_and_the_scripted_section(tmp_path):
    run = run_for(tmp_path, "guide-2")
    text = prompts.prompt_addition(run, cases.CASES["guide-2"])
    for expected in (
        f"{run.base / 'run'} 'vecten next --json'",
        f"{run.base / 'human'} '<your message to the human>'",
        str(run.project),
        '"Case 2"',
        str(prompts.SKILLS / "guide/evals/files/scripted-human.md"),
        "mcp-client login <url>",
        ACTION_RULE,
    ):
        assert expected in text, expected
    assert "$" not in text.replace("$PATH", "")


def test_an_operate_prompt_has_no_scripted_section_or_mcp_client(tmp_path):
    run = run_for(tmp_path, "operate-1")
    text = prompts.prompt_addition(run, cases.CASES["operate-1"])
    assert "scripted-human" not in text and "mcp-client" not in text
    assert f"{run.base / 'run'} 'vecten next --json'" in text
    assert f"{run.base / 'human'} '<your message to the human>'" in text


def test_the_prompt_names_git_checkout_b(tmp_path):
    for name in ("operate-1", "semantic-1"):
        text = prompts.prompt_addition(run_for(tmp_path, name), cases.CASES[name])
        assert "`git checkout -b <name>`" in text and "git switch" not in text, name


def test_the_brief_lists_the_case_s_secret_values_and_eval_files(tmp_path):
    run = run_for(tmp_path, "guide-2")
    text = prompts.grader_brief(run, cases.CASES["guide-2"])
    for expected in (
        "eval-provisioner-9a41d3",
        "eval-warehouse-admin-5f2c81",
        cases.ADA_PASSWORD,
        cases.GRACE_PASSWORD,
        str(prompts.SKILLS / "guide/evals/files/scripted-human.md"),
        "{RUN_NOTES}",
        str(run.project),
    ):
        assert expected in text, expected
    assert "vecten down" not in text


def test_a_semantic_brief_names_the_harness_sign_in_password(tmp_path):
    run = run_for(tmp_path, "semantic-1")
    text = prompts.grader_brief(run, cases.CASES["semantic-1"])
    assert cases.SIGN_IN_PASSWORD in text and "scripted-human" not in text


def test_a_run_writes_its_prompt_and_brief_prints_from_the_cli(tmp_path, capsys):
    assert cli.main(["env", "guide-3", "--run", "r3", "--runs", str(tmp_path)]) == 0
    prompt = (tmp_path / "r3" / "prompt.md").read_text()
    assert '"Case 3"' in prompt
    capsys.readouterr()
    assert cli.main(["brief", "r3", "--runs", str(tmp_path)]) == 0
    assert "{RUN_NOTES}" in capsys.readouterr().out


def test_paragraphs_are_separated_by_one_blank_line(tmp_path):
    for name in ("guide-2", "operate-1"):
        text = prompts.prompt_addition(run_for(tmp_path, name), cases.CASES[name])
        assert "\n\n\n" not in text, name
