"""scripts/live/sandbox_project.py: point a sandbox project at a candidate, check it before push."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vecten.scaffold.render import render_project
from vecten_shared.versions import VERSIONS

from .scaffold_matrix import MATRIX

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/live/sandbox_project.py"
CANDIDATE = VERSIONS.vecten
SECRET = "correct-horse-battery-staple-9"


def mod():
    spec = importlib.util.spec_from_file_location("sandbox_project", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deployed(cloud):
    return next(
        p
        for p in MATRIX
        if p.cloud == cloud
        and p.render_deploy
        and not p.has_warehouse
        and p.profile == "standard"
        and p.components == ["platform", "mcp"]
    )


@pytest.fixture(scope="module", params=["aws", "scaleway"])
def rendered(request, tmp_path_factory):
    target = tmp_path_factory.mktemp("sandbox") / "proj"
    render_project(target, deployed(request.param))
    workflows = target / ".github" / "workflows"
    return {p.name: p.read_bytes().decode() for p in sorted(workflows.iterdir())}


def run(*args, home=None):
    env = {**os.environ, "VECTEN_LIVE_HOME": str(home)} if home else None
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, env=env
    )


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def project(tmp_path, files):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    if files:
        git(root, "add", *files)
    return root


def credential_home(tmp_path):
    home = tmp_path / "home"
    (home / "aws").mkdir(parents=True)
    (home / "aws/credentials").write_text(f"[livecheck]\naws_secret_access_key = {SECRET}\n")
    return home


def stand_in(tmp_path, body):
    path = tmp_path / "stand_in.py"
    path.write_text(body)
    module = mod()
    module.ACCESS = path
    return module


def test_every_install_line_of_every_rendered_workflow_is_patched(rendered):
    sp = mod()
    seen = 0
    for text in rendered.values():
        for line in sp.patch(text, CANDIDATE).splitlines():
            if "vecten==" in line:
                seen += 1
                for package in sp.PACKAGES:
                    assert f"--find-links https://test.pypi.org/simple/{package}/" in line
    assert seen >= 5


def test_options_follow_the_pin_for_uv_tool_and_follow_uvx_for_uvx(rendered):
    sp = mod()
    patched = "".join(sp.patch(t, CANDIDATE) for t in rendered.values())
    assert f'uv tool install "vecten=={CANDIDATE}" {sp.FIND_LINKS}\n' in patched
    assert f'uvx {sp.FIND_LINKS} --with "vecten=={CANDIDATE}" vecten mcp validate' in patched
    assert f'uv tool install "vecten==$VECTEN_VERSION" {sp.FIND_LINKS}\n' in patched


def test_patch_twice_is_patch_once(rendered):
    sp = mod()
    for text in rendered.values():
        once = sp.patch(text, CANDIDATE)
        assert sp.patch(once, CANDIDATE) == once


def test_unpatch_restores_the_rendered_text_byte_for_byte(rendered):
    sp = mod()
    for text in rendered.values():
        assert sp.unpatch(sp.patch(text, CANDIDATE)) == text
        assert sp.unpatch(text) == text


def test_unpatch_keeps_line_endings_and_trailing_whitespace():
    sp = mod()
    text = (
        'a\r\n  - run: uv tool install "vecten==1.2.3"  \r\n'
        '  - run: uvx --with "vecten==1.2.3"\t\r\n'
    )
    assert sp.unpatch(sp.patch(text, "1.2.3")) == text
    assert sp.patch(text, "1.2.3").count("\r\n") == 3


def test_a_workflow_without_a_pin_is_unchanged():
    sp = mod()
    text = "name: x\non: push\njobs: {}\n"
    assert sp.patch(text, "1.2.3") == text


def test_a_pin_for_another_version_is_refused():
    sp = mod()
    with pytest.raises(sp.Refused, match="1.2.2"):
        sp.patch('run: uv tool install "vecten==1.2.2"\n', "1.2.3")


def test_candidate_command_patches_every_workflow_and_release_restores_them(rendered, tmp_path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    for name, text in rendered.items():
        (workflows / name).write_bytes(text.encode())
    assert run("candidate", tmp_path, CANDIDATE).returncode == 0
    for name, text in rendered.items():
        assert (workflows / name).read_bytes().decode() == mod().patch(text, CANDIDATE)
    assert run("release", tmp_path).returncode == 0
    for name, text in rendered.items():
        assert (workflows / name).read_bytes().decode() == text


def test_candidate_command_names_file_and_version_and_changes_nothing_on_a_mismatch(tmp_path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    good = 'run: uv tool install "vecten==1.2.3"\n'
    (workflows / "a.yml").write_text(good)
    (workflows / "b.yml").write_text('run: uv tool install "vecten==1.0.0"\n')
    result = run("candidate", tmp_path, "1.2.3")
    assert result.returncode == 3
    assert "b.yml" in result.stderr and "1.0.0" in result.stderr
    assert (workflows / "a.yml").read_text() == good


def test_candidate_command_refuses_a_project_with_no_workflows(tmp_path):
    assert run("candidate", tmp_path, "1.2.3").returncode == 3


def test_check_public_refuses_a_tracked_env_file(tmp_path):
    root = project(tmp_path, {".env": "A=1\n", "app/.env.local": "B=2\n", "README.md": "x\n"})
    result = run("check-public", root, home=credential_home(tmp_path))
    assert result.returncode == 1
    assert ".env" in result.stdout and "app/.env.local" in result.stdout
    assert "README.md" not in result.stdout


@pytest.mark.parametrize(
    "name",
    [
        "tls/server.pem",
        "tls/server.key",
        ".env.production",
        "tls/KEY.PEM",
        "TLS/Server.Key",
        ".ENV",
    ],
)
def test_check_public_refuses_key_and_pem_file_names(tmp_path, name):
    root = project(tmp_path, {name: "x\n"})
    result = run("check-public", root, home=credential_home(tmp_path))
    assert (result.returncode, name in result.stdout) == (1, True)


def test_check_public_refuses_a_credential_value_in_a_tracked_file(tmp_path):
    root = project(tmp_path, {"infra/main.tf": f'x = "{SECRET}"\n', "README.md": "x\n"})
    result = run("check-public", root, home=credential_home(tmp_path))
    assert result.returncode == 1
    assert "infra/main.tf" in result.stdout and "README.md" not in result.stdout
    assert SECRET not in result.stdout + result.stderr


def test_check_public_lists_every_path_with_the_check_that_caught_it(tmp_path):
    root = project(tmp_path, {".env": f"K={SECRET}\n", "a.txt": SECRET})
    result = run("check-public", root, home=credential_home(tmp_path))
    lines = result.stdout.splitlines()
    assert result.returncode == 1
    assert any(x.startswith(".env") and "file name" in x for x in lines)
    assert any(x.startswith(".env") and "credential" in x for x in lines)
    assert any(x.startswith("a.txt") and "credential" in x for x in lines)


def test_check_public_passes_a_clean_project(tmp_path):
    root = project(tmp_path, {"README.md": "# Sandbox\n", "src/a.py": "x = 1\n"})
    result = run("check-public", root, home=credential_home(tmp_path))
    assert (result.returncode, result.stdout) == (0, "")


def test_check_public_is_not_clean_when_the_credential_home_is_missing(tmp_path):
    root = project(tmp_path, {"README.md": "# Sandbox\n"})
    result = run("check-public", root, home=tmp_path / "absent")
    assert result.returncode == 3
    assert "credential home" in result.stderr


def test_check_public_is_not_clean_when_the_secret_check_cannot_run(tmp_path):
    root = project(tmp_path, {"README.md": "x\n"})
    for body in ("import sys; sys.exit(2)", "raise SystemExit(7)"):
        assert stand_in(tmp_path, body).main(["check-public", str(root)]) == 3
    missing = mod()
    missing.ACCESS = tmp_path / "absent.py"
    assert missing.main(["check-public", str(root)]) == 3


def test_a_name_hit_still_exits_nonzero_when_the_secret_check_cannot_run(tmp_path):
    root = project(tmp_path, {".env": "A=1\n"})
    assert stand_in(tmp_path, "raise SystemExit(2)").main(["check-public", str(root)]) != 0


def test_check_public_relays_the_secret_check_message(tmp_path, capsys):
    root = project(tmp_path, {"README.md": "x\n"})
    body = "import sys; print('access: no credential home at X', file=sys.stderr); sys.exit(3)"
    assert stand_in(tmp_path, body).main(["check-public", str(root)]) == 3
    assert "no credential home at X" in capsys.readouterr().err


def test_check_public_refuses_a_non_repository_and_an_empty_index(tmp_path):
    home = credential_home(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run("check-public", plain, home=home).returncode == 3
    assert run("check-public", project(tmp_path, {}), home=home).returncode == 3


@pytest.mark.parametrize(
    "body",
    [
        "import sys; print('Traceback', file=sys.stderr); sys.exit(1)",
        "print('Traceback (most recent call last):'); raise SystemExit(1)",
        "print('README.md'); raise SystemExit(0)",
    ],
)
def test_check_public_refuses_a_secret_check_that_cannot_be_read_as_a_verdict(tmp_path, body):
    root = project(tmp_path, {"README.md": "x\n"})
    assert stand_in(tmp_path, body).main(["check-public", str(root)]) == 3


def test_check_public_names_the_last_stderr_line_when_the_secret_check_crashes(tmp_path, capsys):
    root = project(tmp_path, {"README.md": "x\n"})
    body = "import sys; print('a\\nImportError: boom', file=sys.stderr); sys.exit(1)"
    assert stand_in(tmp_path, body).main(["check-public", str(root)]) == 3
    assert "without naming a path: ImportError: boom" in capsys.readouterr().err


@pytest.mark.parametrize(
    "line",
    [
        "run: uv tool install vecten==1.2.3",
        'run: uv tool install "vecten==${{ steps.v.outputs.version }}"',
        'run: uv tool install "vecten==1.2.3[extra]"',
        'run: pip install "vecten==1.2.3"',
    ],
)
def test_a_pin_the_patterns_cannot_patch_is_refused(line):
    sp = mod()
    with pytest.raises(sp.Refused):
        sp.patch(line + "\n", "1.2.3")


def test_only_the_version_variable_of_the_rendered_workflows_is_exempt_from_the_version_check():
    sp = mod()
    for ok in ("$VECTEN_VERSION", "${VECTEN_VERSION}"):
        sp.patch(f'run: uv tool install "vecten=={ok}"\n', "1.2.3")
    with pytest.raises(sp.Refused, match="OTHER"):
        sp.patch('run: uv tool install "vecten==$OTHER"\n', "1.2.3")
