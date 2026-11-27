import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/live/combos.py"
MAP_LINE = f"map {SCRIPT.with_name('filemap.toml')}"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def mod():
    spec = importlib.util.spec_from_file_location("combos", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULES = mod().load_rules(REPO / "scripts/live/filemap.toml")
LEDGER = {
    "aws-postgres": "v1",
    "aws-snowflake": "v1",
    "scaleway-postgres": "v1",
    "scaleway-snowflake": "v1",
    "azure-postgres": "v1",
    "azure-snowflake": "v1",
}


@pytest.mark.parametrize(
    "path, scope",
    [
        ("terraform/aws/hosts.tf", "cloud:aws"),
        ("terraform/scaleway/modules/host/agent/apply.py", "cloud:scaleway"),
        ("terraform/azure/hosts.tf", "cloud:azure"),
        ("packages/vecten/src/vecten/deploy/azure/ship.py", "cloud:azure"),
        ("terraform/azure/postgres.tf", "cloud:azure"),
        ("packages/vecten/src/vecten/deploy/vm/apply.py", "shared"),
        ("packages/vecten-mcp/src/vecten_mcp/warehouse/snowflake.py", "warehouse:snowflake"),
        (
            "packages/vecten-shared/src/vecten_shared/sql/snowflake_access.js",
            "warehouse:snowflake",
        ),
        ("packages/vecten/src/vecten/scaffold/snowflake_root/main.tf", "warehouse:snowflake"),
        ("packages/vecten/src/vecten/scaffold/postgres-root/main.tf", "warehouse:postgres"),
        ("snowflake.py", "warehouse:snowflake"),
        ("terraform/aws/postgres.tf", "cloud:aws"),
        ("docs/mcp.md", "none"),
        ("README.md", "none"),
        ("packages/vecten/tests/test_up_e2e.py", "none"),
        ("images/mcp/Dockerfile", "shared"),
        ("a-new-directory/thing.py", "shared"),
    ],
)
def test_classify(path, scope):
    assert mod().classify(path, RULES) == scope


def changes(**by_tag):
    calls = []

    def changed(tag):
        calls.append(tag)
        return by_tag[tag]

    changed.calls = calls
    return changed


def test_docs_only_changes_owe_nothing():
    assert mod().owed(LEDGER, changes(v1=["docs/a.md", "scripts/x.py"]), RULES) == {}


def test_a_cloud_file_owes_both_of_that_clouds_combinations():
    owed = mod().owed(LEDGER, changes(v1=["terraform/aws/hosts.tf"]), RULES)
    assert owed == {
        "aws-postgres": ["terraform/aws/hosts.tf"],
        "aws-snowflake": ["terraform/aws/hosts.tf"],
    }


def test_a_warehouse_file_owes_both_of_that_warehouses_combinations():
    owed = mod().owed(LEDGER, changes(v1=["packages/x/snowflake.py"]), RULES)
    assert set(owed) == {"aws-snowflake", "scaleway-snowflake", "azure-snowflake"}


def test_an_azure_file_owes_only_the_azure_combinations():
    owed = mod().owed(LEDGER, changes(v1=["terraform/azure/hosts.tf"]), RULES)
    assert owed == {
        "azure-postgres": ["terraform/azure/hosts.tf"],
        "azure-snowflake": ["terraform/azure/hosts.tf"],
    }


def test_a_shared_change_owes_each_clouds_default():
    owed = mod().owed(LEDGER, changes(v1=["images/mcp/Dockerfile"]), RULES)
    assert owed == {
        "aws-snowflake": ["images/mcp/Dockerfile"],
        "scaleway-postgres": ["images/mcp/Dockerfile"],
        "azure-postgres": ["images/mcp/Dockerfile"],
    }


def test_an_unmapped_new_directory_owes_combinations_and_is_named():
    owed = mod().owed(LEDGER, changes(v1=["brand-new/thing.py"]), RULES)
    assert owed == {
        "aws-snowflake": ["brand-new/thing.py"],
        "scaleway-postgres": ["brand-new/thing.py"],
        "azure-postgres": ["brand-new/thing.py"],
    }


def test_a_shared_change_rides_on_a_combination_already_owed():
    owed = mod().owed(LEDGER, changes(v1=["packages/x/postgres.py", "images/Dockerfile"]), RULES)
    assert set(owed) == {"aws-postgres", "scaleway-postgres", "azure-postgres"}
    assert owed["aws-postgres"] == ["packages/x/postgres.py"]


def test_each_combination_is_measured_from_its_own_tag():
    ledger = {**LEDGER, "aws-snowflake": "v2", "scaleway-snowflake": "v2"}
    changed = changes(v1=["terraform/aws/a.tf"], v2=["docs/a.md"])
    owed = mod().owed(ledger, changed, RULES)
    assert sorted(changed.calls) == ["v1", "v2"]
    assert set(owed) == {"aws-postgres"}


def test_a_shared_change_before_the_default_ran_is_covered():
    ledger = {**LEDGER, "aws-snowflake": "v2", "scaleway-postgres": "v2", "azure-postgres": "v2"}
    owed = mod().owed(ledger, changes(v1=["images/Dockerfile"], v2=[]), RULES)
    assert owed == {}


def test_the_real_ledger_names_only_known_combinations():
    ledger = tomllib.loads((REPO / "scripts/live/ledger.toml").read_text())
    assert set(ledger) == set(mod().COMBOS)


def test_the_real_map_loads_in_order():
    assert [rule.scope for rule in RULES] == [
        "none",
        "cloud:aws",
        "cloud:scaleway",
        "cloud:azure",
        "warehouse:snowflake",
        "warehouse:postgres",
    ]


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **IDENTITY},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(cwd, name, text="x\n"):
    path = cwd / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", f"add {name}")


@pytest.fixture
def clone(tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    commit(repo, "README.md")
    git(repo, "tag", "v1")
    commit(repo, "docs/a.md")
    commit(repo, "terraform/scaleway/main.tf")
    return repo


def ledger_file(tmp_path, tag="v1"):
    path = tmp_path / "ledger.toml"
    path.write_text("# keep me\n" + "".join(f'{name} = "{tag}"\n' for name in mod().COMBOS))
    return path


def run(clone, ledger, *args):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            args[0],
            "--ledger",
            str(ledger),
            "--map",
            str(REPO / "scripts/live/filemap.toml"),
            *args[1:],
        ],
        cwd=clone,
        capture_output=True,
        text=True,
    )


def test_plan_prints_reasons_and_what_is_not_owed(clone, tmp_path):
    result = run(clone, ledger_file(tmp_path), "plan")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == f"ledger {tmp_path / 'ledger.toml'}, {MAP_LINE}"
    owed = [line for line in lines if line.startswith("scaleway-postgres")]
    assert len(owed) == 1
    assert "v1" in owed[0]
    assert "terraform/scaleway/main.tf" in owed[0]
    assert lines[-1].startswith("not owed")
    assert "aws-postgres" in lines[-1]
    assert "aws-snowflake" in lines[-1]


def test_plan_json_is_only_the_json(clone, tmp_path):
    result = run(clone, ledger_file(tmp_path), "plan", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "owed": {
            "scaleway-postgres": {"since": "v1", "files": ["terraform/scaleway/main.tf"]},
            "scaleway-snowflake": {"since": "v1", "files": ["terraform/scaleway/main.tf"]},
        },
        "not_owed": ["aws-postgres", "aws-snowflake", "azure-postgres", "azure-snowflake"],
    }


def test_plan_with_nothing_owed_exits_zero(clone, tmp_path):
    git(clone, "tag", "v2")
    result = run(clone, ledger_file(tmp_path, "v2"), "plan", "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["owed"] == {}


def test_renamed_files_count_by_old_and_new_paths(clone, tmp_path):
    git(clone, "tag", "v2")
    (clone / "terraform/aws").mkdir(parents=True)
    git(clone, "mv", "docs/a.md", "terraform/aws/a.tf")
    git(clone, "commit", "-m", "move")
    git(clone, "mv", "terraform/scaleway/main.tf", "docs/main.md")
    git(clone, "commit", "-m", "move out")
    result = run(clone, ledger_file(tmp_path, "v2"), "plan", "--json")
    owed = json.loads(result.stdout)["owed"]
    assert owed["scaleway-postgres"]["files"] == ["terraform/scaleway/main.tf"]
    assert owed["aws-postgres"]["files"] == ["terraform/aws/a.tf"]


def test_a_ledger_tag_the_clone_lacks_is_an_error_not_an_empty_plan(clone, tmp_path):
    result = run(clone, ledger_file(tmp_path, "v9"), "plan")
    assert result.returncode == 3
    assert "v9" in result.stderr
    assert result.stdout == ""


def test_a_combination_missing_from_the_ledger_is_refused(clone, tmp_path):
    ledger = tmp_path / "ledger.toml"
    ledger.write_text('aws-postgres = "v1"\n')
    result = run(clone, ledger, "plan")
    assert result.returncode == 3
    assert "aws-snowflake" in result.stderr


def test_record_changes_one_line(clone, tmp_path):
    ledger = ledger_file(tmp_path)
    before = ledger.read_text().splitlines()
    git(clone, "tag", "v3")
    result = run(clone, ledger, "record", "aws-snowflake", "v3")
    assert result.returncode == 0, result.stderr
    after = ledger.read_text().splitlines()
    assert len(after) == len(before)
    assert [b for a, b in zip(before, after, strict=True) if a != b] == ['aws-snowflake = "v3"']
    assert after[0] == "# keep me"


def test_record_refuses_an_unknown_combination(clone, tmp_path):
    result = run(clone, ledger_file(tmp_path), "record", "gcp-postgres", "v1")
    assert result.returncode == 3
    assert "gcp-postgres" in result.stderr


def test_record_refuses_a_tag_the_clone_lacks(clone, tmp_path):
    ledger = ledger_file(tmp_path)
    before = ledger.read_text()
    result = run(clone, ledger, "record", "aws-postgres", "v9")
    assert result.returncode == 3
    assert "v9" in result.stderr
    assert ledger.read_text() == before


@pytest.fixture
def candidates(tmp_path):
    """Two candidates cut as the runbook cuts them: each a bump commit on the release branch."""
    repo = tmp_path / "candidates"
    repo.mkdir()
    git(repo, "init", "--initial-branch=release")
    commit(repo, "README.md")
    commit(repo, "packages/vecten/src/vecten/cli.py")

    def cut(name, *fix):
        git(repo, "switch", "--quiet", "--create", f"rc/{name}", "release")
        for path in fix:
            commit(repo, path, f"fix for {name}\n")
        bumped = ("uv.lock", "packages/vecten-shared/src/vecten_shared/versions.toml")
        for path in bumped:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(f"{name}\n")
        git(repo, "add", *bumped)
        git(repo, "commit", "-m", f"chore(release): {name}")
        git(repo, "tag", "--annotate", "--message", name, f"v{name}")
        git(repo, "switch", "--quiet", "release")

    cut("0.1.2rc1")
    return repo, cut


def affected(repo, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "affected", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_affected_names_only_the_combinations_the_fix_touches(candidates):
    repo, cut = candidates
    cut("0.1.2rc2", "terraform/scaleway/main.tf")
    result = affected(repo, "v0.1.2rc1", "v0.1.2rc2", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "touched": {
            "scaleway-postgres": ["terraform/scaleway/main.tf"],
            "scaleway-snowflake": ["terraform/scaleway/main.tf"],
        },
        "untouched": ["aws-postgres", "aws-snowflake", "azure-postgres", "azure-snowflake"],
    }


def test_affected_prints_each_touched_combination_then_the_untouched(candidates):
    repo, cut = candidates
    cut("0.1.2rc2", "packages/x/snowflake.py", "images/mcp/Dockerfile")
    result = affected(repo, "v0.1.2rc1", "v0.1.2rc2")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines == [
        MAP_LINE,
        "aws-postgres         1 files: images/mcp/Dockerfile",
        "aws-snowflake        2 files: images/mcp/Dockerfile, packages/x/snowflake.py",
        "scaleway-postgres    1 files: images/mcp/Dockerfile",
        "scaleway-snowflake   2 files: images/mcp/Dockerfile, packages/x/snowflake.py",
        "azure-postgres       1 files: images/mcp/Dockerfile",
        "azure-snowflake      2 files: images/mcp/Dockerfile, packages/x/snowflake.py",
        "untouched: none",
    ]


def test_a_docs_only_fix_touches_nothing(candidates):
    repo, cut = candidates
    cut("0.1.2rc2", "docs/a.md")
    result = affected(repo, "v0.1.2rc1", "v0.1.2rc2")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        MAP_LINE,
        "untouched: aws-postgres, aws-snowflake, scaleway-postgres, scaleway-snowflake,"
        " azure-postgres, azure-snowflake",
    ]


def test_affected_refuses_a_tag_the_clone_lacks(candidates):
    repo, _ = candidates
    result = affected(repo, "v0.1.2rc1", "v0.1.2rc9")
    assert result.returncode == 3
    assert "v0.1.2rc9" in result.stderr
    assert result.stdout == ""


def tools_copy(tmp_path):
    """combos.py with its own ledger and map, as the tools copy holds them."""
    here = tmp_path / "tools/scripts/live"
    here.mkdir(parents=True)
    for name in ("combos.py", "ledger.toml", "filemap.toml"):
        shutil.copy2(SCRIPT.with_name(name), here / name)
    return here.resolve()


def from_tools(here, cwd, *args):
    return subprocess.run(
        [sys.executable, str(here / "combos.py"), *args], cwd=cwd, capture_output=True, text=True
    )


def every_combination_at(tag):
    return "".join(f'{name} = "{tag}"\n' for name in mod().COMBOS)


def test_a_ledger_in_the_checkout_worked_in_is_the_default(clone, tmp_path):
    here = tools_copy(tmp_path)
    beside = (here / "ledger.toml").read_text()
    local = clone.resolve() / "scripts/live"
    local.mkdir(parents=True)
    ledger = local / "ledger.toml"
    ledger.write_text(every_combination_at("v1"))
    (local / "filemap.toml").write_text(
        '[[rule]]\nscope = "none"\npaths = ["terraform/**", "docs/**"]\n'
    )
    planned = from_tools(here, clone, "plan")
    assert planned.returncode == 0, planned.stderr
    assert planned.stdout.splitlines() == [
        f"ledger {ledger}, map {local / 'filemap.toml'}",
        "not owed: aws-postgres, aws-snowflake, scaleway-postgres, scaleway-snowflake,"
        " azure-postgres, azure-snowflake",
    ]
    git(clone, "tag", "v3")
    recorded = from_tools(here, clone, "record", "aws-snowflake", "v3")
    assert recorded.returncode == 0, recorded.stderr
    assert recorded.stdout.splitlines() == [f"ledger {ledger}", "aws-snowflake = v3"]
    assert 'aws-snowflake = "v3"' in ledger.read_text()
    assert (here / "ledger.toml").read_text() == beside


def test_without_a_ledger_in_the_checkout_the_one_beside_the_script_is_read(clone, tmp_path):
    here = tools_copy(tmp_path)
    ledger = here / "ledger.toml"
    ledger.write_text(every_combination_at("v1"))
    planned = from_tools(here, clone, "plan")
    assert planned.returncode == 0, planned.stderr
    assert planned.stdout.splitlines()[0] == f"ledger {ledger}, map {here / 'filemap.toml'}"


def test_record_writes_no_ledger_but_the_checkouts_unless_named(clone, tmp_path):
    here = tools_copy(tmp_path)
    ledger = here / "ledger.toml"
    before = ledger.read_text()
    git(clone, "tag", "v3")
    refused = from_tools(here, clone, "record", "aws-snowflake", "v3")
    assert refused.returncode == 3
    assert refused.stdout == ""
    assert "scripts/live/ledger.toml" in refused.stderr
    assert ledger.read_text() == before
    assert not (clone / "scripts").exists()
    named = from_tools(here, clone, "record", "--ledger", str(ledger), "aws-snowflake", "v3")
    assert named.returncode == 0, named.stderr
    assert 'aws-snowflake = "v3"' in ledger.read_text()


def test_json_output_names_no_file(clone, tmp_path):
    here = tools_copy(tmp_path)
    (here / "ledger.toml").write_text(every_combination_at("v1"))
    planned = from_tools(here, clone, "plan", "--json")
    assert planned.returncode == 0, planned.stderr
    assert set(json.loads(planned.stdout)) == {"owed", "not_owed"}


@pytest.mark.parametrize(
    "value",
    [
        "--output={written}-and-then-some-more-text",
        "-pfoo",
        "v1 HEAD",
        "",
    ],
)
def test_a_ledger_value_that_is_no_tag_name_is_refused_before_git_reads_it(clone, tmp_path, value):
    written = tmp_path / "written-by-git"
    value = value.format(written=written)
    if value.startswith("-"):
        # `git tag` refuses such a name, but a ref of that name can still be written directly.
        git(clone, "update-ref", f"refs/tags/{value}", "HEAD")
    ledger = ledger_file(tmp_path)
    ledger.write_text(
        ledger.read_text().replace('aws-postgres = "v1"', f'aws-postgres = "{value}"')
    )
    result = run(clone, ledger, "plan")
    assert result.returncode == 3
    assert result.stdout == ""
    assert "aws-postgres" in result.stderr
    assert value[20:] == "" or value[20:] not in result.stderr
    assert not list(tmp_path.glob("written-by-git*"))


def test_revisions_reach_git_as_tag_refs_after_the_end_of_options(monkeypatch):
    m = mod()
    seen = []

    def run_git(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(m.subprocess, "run", run_git)
    m.has_tag("v1")
    m.git_changed("v1")
    m.changed_between("refs/tags/v1^", "refs/tags/v2^")
    diff = ["git", "diff", "--name-only", "--no-renames", "--end-of-options"]
    assert seen == [
        ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", "refs/tags/v1"],
        [*diff, "refs/tags/v1", "HEAD", "--"],
        [*diff, "refs/tags/v1^", "refs/tags/v2^", "--"],
    ]
