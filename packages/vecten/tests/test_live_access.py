"""The live-check wrapper and its guard; fixtures from documentation.

The probe outputs below follow each tool's documented output shape, with made-up IDs.
"""

import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/live/access.py"
ORG = "00000000-0000-0000-0000-000000000000"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

ALLOW_TOML = f"""\
[aws]
account_id = "111111111111"
[scaleway]
organization_id = "{ORG}"
[snowflake]
account = "EXAMPLE"
[github]
sandbox_org = "example-sandbox"
upstream_repo = "vecten/vecten-open"
"""
ALLOW = {
    "aws": {"account_id": "111111111111"},
    "scaleway": {"organization_id": ORG},
    "snowflake": {"account": "EXAMPLE"},
    "github": {"sandbox_org": "example-sandbox", "upstream_repo": "vecten/vecten-open"},
}
GOOD = {
    "aws": "111111111111",
    "scaleway": [ORG],
    "snowflake": "EXAMPLE",
    "github": ["example-sandbox/demo-aws-postgres"],
}

# aws sts get-caller-identity --query Account --output text
AWS_OUTPUT = "111111111111\n"
# scw account project list -o json
SCW_OUTPUT = f"""\
[
  {{
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "default",
    "organization_id": "{ORG}",
    "created_at": "2026-01-01T00:00:00.000000Z",
    "updated_at": "2026-01-01T00:00:00.000000Z",
    "description": ""
  }}
]
"""
# snow sql -c livecheck -q "select current_account()" --format json
SNOW_OUTPUT = """\
[
  {
    "CURRENT_ACCOUNT()": "EXAMPLE"
  }
]
"""
# gh api /installation/repositories --paginate --jq ".repositories[].full_name"
GH_SANDBOX_OUTPUT = "example-sandbox/demo-aws-postgres\nexample-sandbox/demo-scaleway-snowflake\n"
GH_UPSTREAM_OUTPUT = "vecten/vecten-open\n"

DRIVER = """\
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("access", sys.argv[1])
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)
overrides = json.loads(sys.argv[2])
real_home = pathlib.Path(overrides.pop("real_home"))
access.real_home = lambda: real_home
for name, value in overrides.items():
    setattr(access, name, value)
sys.exit(access.main(sys.argv[3:]))
"""
EXAMPLE_HOME = Path("/Users/example")
EXAMPLE_LIVE = EXAMPLE_HOME / ".config/vecten-livecheck"
EXAMPLE_TOOLS = EXAMPLE_HOME / "vecten-live-tools"
EXAMPLE_PYTHON = [Path("/opt/example-python/3.14"), EXAMPLE_TOOLS / ".venv"]
DENIED_TEXT = (
    "(version 1)(allow default)"
    '(deny file-read* (regex #"^/Users/example/(.*/)?\\.env(\\.[^/]+)?$"))'
    '(allow file-read* (subpath "/Users/example/vecten-sandbox"))'
    '(deny file-read* file-write* (subpath "/Users/example/.aws")'
    ' (subpath "/Users/example/.config/scw") (subpath "/Users/example/.snowflake")'
    ' (subpath "/Users/example/.config/snowflake") (subpath "/Users/example/.ssh")'
    ' (subpath "/Users/example/.config/gh") (subpath "/Users/example/.gnupg")'
    ' (subpath "/Users/example/.kube") (subpath "/Users/example/.config/gcloud")'
    ' (subpath "/Users/example/.terraform.d") (subpath "/Users/example/Library/Keychains")'
    ' (subpath "/Users/example/Library/Application Support/snowflake")'
    ' (subpath "/Users/example/.dbt") (subpath "/Users/example/.snowsql")'
    ' (subpath "/Users/example/.config/op")'
    ' (literal "/Users/example/.docker/config.json") (literal "/Users/example/.netrc")'
    ' (literal "/Users/example/.pypirc") (literal "/Users/example/.npmrc")'
    ' (literal "/Users/example/.git-credentials")'
    ' (literal "/Users/example/.config/git/credentials")'
    ' (literal "/Users/example/.terraformrc")'
    ' (literal "/Users/example/.claude/.credentials.json")'
    ' (literal "/Users/example/.pgpass"))'
    '(deny file-write* (subpath "/Users/example/.config/vecten-livecheck"))'
    '(allow file-write* (subpath "/Users/example/.config/vecten-livecheck/home")'
    ' (subpath "/Users/example/.config/vecten-livecheck/home-plain")'
    ' (subpath "/Users/example/.config/vecten-livecheck/snowflake/logs")'
    ' (subpath "/Users/example/.config/vecten-livecheck/github/gh"))'
    '(deny file-write* (literal "/Users/example/.zshrc") (literal "/Users/example/.zprofile")'
    ' (literal "/Users/example/.zshenv") (literal "/Users/example/.bashrc")'
    ' (literal "/Users/example/.bash_profile") (literal "/Users/example/.profile")'
    ' (literal "/Users/example/.gitconfig") (subpath "/Users/example/.config/git")'
    ' (subpath "/Users/example/.claude")'
    ' (subpath "/Users/example/vecten-live-tools")'
    ' (subpath "/opt/example-python/3.14")'
    ' (subpath "/Users/example/vecten-live-tools/.venv")'
    ' (regex #"^(.*/)?\\.claude'
    '(/(settings\\.json|settings\\.local\\.json|hooks|skills|commands|agents)(/.*)?)?$"))'
    '(deny file-write-unlink (literal "/Users/example") (literal "/Users")'
    ' (literal "/Users/example/.config") (literal "/Users/example/Library")'
    ' (literal "/Users/example/Library/Application Support")'
    ' (literal "/Users/example/.docker") (literal "/Users/example/.config/git")'
    ' (literal "/Users/example/.claude") (literal "/opt/example-python") (literal "/opt")'
    ' (literal "/Users/example/vecten-live-tools"))'
    "(deny network-outbound (remote unix-socket))"
    '(allow network-outbound (literal "/private/var/run/mDNSResponder"))'
)


def short_commit(root):
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True
    )
    return done.stdout.strip() if done.returncode == 0 else "unknown"


TOOLS_OK = f"tools ok: {REPO} at {short_commit(REPO)}"
CREDENTIAL_VARIABLES = {
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_PROFILE",
    "SCW_CONFIG_PATH",
    "SNOWFLAKE_HOME",
    "SNOWFLAKE_DEFAULT_CONNECTION_NAME",
    "VECTEN_LIVE",
}
LAUNCHER = REPO / "scripts/live/live"
IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
IDENTITY += ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


def mod():
    spec = importlib.util.spec_from_file_location("access", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def home(tmp_path):
    root = tmp_path / "home"
    files = {
        "allow.toml": ALLOW_TOML,
        "aws/config": "[profile livecheck]\nregion = eu-west-1\n",
        "aws/credentials": (
            "[livecheck]\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
            f"aws_secret_access_key = {AWS_SECRET}\n"
        ),
        "scw/config.yaml": "secret_key: 22222222-2222-2222-2222-222222222222\nregion: fr-par\n",
        "snowflake/config.toml": '[connections.livecheck]\npassword = "correct-horse-pony"\n',
        "github/sandbox.toml": 'app_id = "12345"\ninstallation_id = "987654"\nkey = "app.pem"\n',
        "github/upstream.toml": 'app_id = "12345"\ninstallation_id = "987655"\nkey = "app.pem"\n',
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "github/gh").mkdir()
    return root


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def printing(output):
    return f"cat <<'EOF'\n{output}EOF"


def fake_clis(tmp_path, **overrides):
    """Fake CLIs that answer as the allowed identities; overrides replace a script body."""
    bodies = {
        "aws": printing(AWS_OUTPUT),
        "scw": printing(SCW_OUTPUT),
        "snow": printing(SNOW_OUTPUT),
        "gh": (
            'case "$GH_TOKEN" in\n'
            f"  *upstream*) {printing(GH_UPSTREAM_OUTPUT)}\n;;\n"
            f"  *) {printing(GH_SANDBOX_OUTPUT)}\n;;\n"
            "esac"
        ),
        "vecten": "exit 0",
        **overrides,
    }
    bin_dir = tmp_path / "bin"
    for name, body in bodies.items():
        script(bin_dir / name, body)
    return bin_dir


def access(tmp_path, *args, timeout=30.0, env=None, stdin="", cwd=None, **overrides):
    token = script(tmp_path / "token", 'echo "ghs_fake_$(basename "$1" .toml)_token"')
    base = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "VECTEN_LIVE_HOME": str(tmp_path / "home"),
        **(env or {}),
    }
    settings = {
        "TOKEN_COMMAND": [str(token)],
        "PROBE_TIMEOUT": timeout,
        "PLATFORM": "linux",
        "real_home": str(tmp_path / "real"),
        **overrides,
    }
    return subprocess.run(
        [sys.executable, "-c", DRIVER, str(SCRIPT), json.dumps(settings), *args],
        env=base,
        cwd=cwd or tmp_path,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_inherited_credentials_never_reach_the_command(tmp_path):
    base = {
        "AWS_PROFILE": "personal",
        "AWS_ACCESS_KEY_ID": "AKIAPERSONAL",
        "AWS_SECRET_ACCESS_KEY": "y",
        "AWS_SESSION_TOKEN": "x",
        "AWS_DEFAULT_PROFILE": "personal",
        "GH_TOKEN": "ghp_personal",
        "GITHUB_TOKEN": "ghp_other",
        "GH_ENTERPRISE_TOKEN": "ghp_enterprise",
        "SCW_ACCESS_KEY": "a",
        "SCW_SECRET_KEY": "s",
        "SCW_PROFILE": "personal",
        "SNOWFLAKE_CONNECTIONS": "[personal]",
        "SNOWFLAKE_CONNECTIONS_PERSONAL_PASSWORD": "p",
        "AWS_ROLE_ARN": "arn:aws:iam::222222222222:role/personal",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/personal-token",
        "SNOWFLAKE_USER": "personal",
        "SNOWFLAKE_PASSWORD": "personal",
        "GH_HOST": "github.example.com",
        "GIT_CONFIG_PARAMETERS": "'credential.helper'='personal'",
        "GIT_CONFIG_KEY_7": "credential.helper",
        "GIT_CONFIG_VALUE_7": "personal",
        "GIT_ASKPASS": "/usr/local/bin/personal-askpass",
        "SSH_ASKPASS": "/usr/local/bin/personal-askpass",
        "UV_PUBLISH_TOKEN": "pypi-personal",
        "TF_TOKEN_app_terraform_io": "personal",
        "VECTEN_GH_RUNNER": "personal_module",
        "GIT_CONFIG_GLOBAL": "/Users/example/.gitconfig",
        "DOCKER_CONFIG": "/Users/example/.docker",
        "XDG_CONFIG_HOME": "/Users/example/.config",
        "HOME": str(tmp_path / "real"),
        "PATH": "/bin",
    }
    root = home(tmp_path)
    env = mod().tool_env(root, base, github_token="ghs_minted")
    assert env["AWS_PROFILE"] == "livecheck" and env["GH_TOKEN"] == "ghs_minted"
    assert (
        not {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_PROFILE",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "SCW_ACCESS_KEY",
            "SCW_SECRET_KEY",
            "SCW_PROFILE",
            "SNOWFLAKE_CONNECTIONS",
            "SNOWFLAKE_CONNECTIONS_PERSONAL_PASSWORD",
            "AWS_ROLE_ARN",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "SNOWFLAKE_USER",
            "SNOWFLAKE_PASSWORD",
            "GH_HOST",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_KEY_7",
            "GIT_CONFIG_VALUE_7",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "UV_PUBLISH_TOKEN",
            "TF_TOKEN_app_terraform_io",
            "VECTEN_GH_RUNNER",
            "XDG_CONFIG_HOME",
        }
        & env.keys()
    )
    assert env["HOME"] == str(root / "home")
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["DOCKER_CONFIG"] == str(root / "docker")
    assert env["PATH"] == "/bin"
    assert env["AWS_CONFIG_FILE"] == str(root / "aws/config")
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == str(root / "aws/credentials")
    assert env["SCW_CONFIG_PATH"] == str(root / "scw/config.yaml")
    assert env["SNOWFLAKE_HOME"] == str(root / "snowflake")
    assert env["SNOWFLAKE_DEFAULT_CONNECTION_NAME"] == "livecheck"
    assert env["GH_CONFIG_DIR"] == str(root / "github/gh")
    assert env["VECTEN_LIVE"] == "1"


def test_the_snowflake_probe_names_the_livecheck_connection():
    assert mod().PROBES["snowflake"][:4] == ["snow", "sql", "-c", "livecheck"]


def test_only_identity_free_variables_pass(tmp_path):
    passed = {
        "PATH": "/bin",
        "LANG": "en_GB.UTF-8",
        "LC_ALL": "en_GB.UTF-8",
        "TERM": "xterm",
        "https_proxy": "http://proxy.invalid:3128",
        "NO_PROXY": "localhost",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    }
    dropped = {"SOME_TOOL_TOKEN": "personal", "DOCKER_HOST": "unix:///var/run/docker.sock"}
    base = {**passed, **dropped, "HOME": str(tmp_path / "real")}
    env = mod().tool_env(home(tmp_path), base, github_token="ghs_minted")
    assert passed.items() <= env.items()
    assert not dropped.keys() & env.keys()


def test_the_real_home_ignores_the_home_variable(monkeypatch):
    m = mod()
    before = m.real_home()
    monkeypatch.setenv("HOME", "/nonexistent-root/elsewhere")
    assert m.real_home() == before != Path("/nonexistent-root/elsewhere")


def test_the_command_gets_its_own_home_and_tool_locations(tmp_path, monkeypatch):
    real = tmp_path / "real"
    root = home(tmp_path)
    m = mod()
    monkeypatch.setattr(m, "real_home", lambda: real)
    base = {"HOME": str(tmp_path / "elsewhere"), "PATH": "/bin"}
    env = m.tool_env(root, base, github_token="ghs_minted")
    assert env["HOME"] == str(root / "home")
    assert stat.S_IMODE((root / "home").stat().st_mode) == 0o700
    assert "DOCKER_HOST" not in env
    assert env["UV_CACHE_DIR"] == str(real / ".cache/uv")
    assert env["UV_PYTHON_INSTALL_DIR"] == str(real / ".local/share/uv/python")
    assert (env["GIT_CONFIG_NOSYSTEM"], env["GIT_TERMINAL_PROMPT"]) == ("1", "0")
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    (root / "git").mkdir()
    (root / "git/config").write_text("[user]\n\tname = Live Check\n")
    assert m.tool_env(root, base, "ghs_minted")["GIT_CONFIG_GLOBAL"] == str(root / "git/config")


def test_unconfigured_github_gets_a_sentinel_token_and_no_gh_helper(tmp_path):
    env = mod().tool_env(home(tmp_path), {"HOME": str(tmp_path), "PATH": "/bin"}, github_token="")
    assert env["GH_TOKEN"] == "livecheck-github-not-configured"
    assert "!gh auth git-credential" not in env.values()
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""


def test_run_without_github_gives_the_command_the_sentinel_token(tmp_path):
    allow = home(tmp_path) / "allow.toml"
    allow.write_text(ALLOW_TOML.partition("[github]")[0])
    fake_clis(tmp_path, vecten='echo "$GH_TOKEN"; env | grep -c "gh auth git-credential"; true')
    result = access(tmp_path, "run", "--", "vecten", env={"GH_TOKEN": "ghp_personal"})
    assert result.returncode == 0, result.stderr
    assert result.stdout == "livecheck-github-not-configured\n0\n"


def test_git_asks_only_the_app_token_for_github(tmp_path):
    personal_helper = (
        '[credential]\n\thelper = "!f() { echo username=me; echo password=personal; }; f"\n'
    )
    personal = tmp_path / "personal.gitconfig"
    personal.write_text(personal_helper)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    with (repo / ".git/config").open("a") as config:
        config.write(personal_helper)
    script(
        tmp_path / "bin/gh",
        'test "$*" = "auth git-credential get" || exit 1\n'
        'echo username=x-access-token; echo "password=$GH_TOKEN"',
    )
    base = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(personal),
    }
    env = mod().tool_env(home(tmp_path), base, github_token="ghs_minted")
    filled = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "password=ghs_minted" in filled.stdout.splitlines()
    for remote in (
        "git@github.com:example-sandbox/demo.git",
        "ssh://git@github.com/example-sandbox/demo.git",
    ):
        rewritten = subprocess.run(
            ["git", "ls-remote", "--get-url", remote],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert rewritten.stdout == "https://github.com/example-sandbox/demo.git\n"


def test_git_never_uses_ssh(tmp_path):
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_SSH_COMMAND": "ssh -i personal"}
    env = mod().tool_env(home(tmp_path), base, github_token="ghs_minted")
    refused = subprocess.run(
        ["git", "ls-remote", "ssh://git@git.example.invalid/sandbox/demo.git"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode != 0
    assert "access: git over SSH is refused" in refused.stderr


def test_another_aws_account_is_refused():
    assert mod().violations(ALLOW, {**GOOD, "aws": "222222222222"}, "sandbox") == [
        "aws: account 222222222222 is not the live-check account"
    ]


def test_a_probe_that_could_not_tell_is_refused():
    assert mod().violations(ALLOW, {**GOOD, "snowflake": None}, "sandbox") == [
        "snowflake: identity could not be read"
    ]


def test_an_identity_missing_from_the_findings_is_refused():
    found = {name: value for name, value in GOOD.items() if name != "scaleway"}
    assert mod().violations(ALLOW, found, "sandbox") == ["scaleway: identity could not be read"]


def test_an_identity_missing_from_allow_toml_is_not_probed():
    allow = {name: section for name, section in ALLOW.items() if name != "snowflake"}
    assert mod().violations(allow, {**GOOD, "snowflake": None}, "sandbox") == []


def test_another_scaleway_organization_is_refused():
    other = "99999999-9999-9999-9999-999999999999"
    assert mod().violations(ALLOW, {**GOOD, "scaleway": [ORG, other]}, "sandbox") == [
        f"scaleway: organization {other} is not the live-check organization"
    ]


def test_a_sandbox_token_that_reaches_another_owner_is_refused():
    found = {**GOOD, "github": ["example-sandbox/demo", "vecten/vecten-open"]}
    assert mod().violations(ALLOW, found, "sandbox") == [
        "github: the sandbox token reaches vecten/vecten-open, outside example-sandbox"
    ]
    assert mod().violations(ALLOW, {**GOOD, "github": []}, "sandbox") == []


def test_an_upstream_token_must_reach_exactly_the_one_repository():
    m = mod()
    assert m.violations(ALLOW, {**GOOD, "github": ["vecten/vecten-open"]}, "upstream") == []
    found = {**GOOD, "github": ["vecten/vecten-open", "vecten/other"]}
    assert m.violations(ALLOW, found, "upstream") == [
        "github: the upstream token reaches vecten/vecten-open, vecten/other,"
        " not exactly vecten/vecten-open"
    ]


def test_run_refuses_before_starting_the_command(tmp_path):
    home(tmp_path)
    marker = tmp_path / "started"
    fake_clis(tmp_path, aws="echo 222222222222", vecten=f"touch {marker}")
    result = access(tmp_path, "run", "--", "vecten", "deploy")
    assert result.returncode == 3
    assert "aws: account 222222222222 is not the live-check account" in result.stderr
    assert not marker.exists()


def test_run_refuses_an_executable_outside_the_list(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path)
    for refused in (["bash", "-c", "true"], ["docker", "run", "-v", "/:/host", "alpine"]):
        result = access(tmp_path, "run", "--", *refused)
        assert result.returncode == 2
        assert f"{refused[0]} is not allowed" in result.stderr


def test_run_returns_the_commands_exit_code(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, vecten="exit 7")
    assert access(tmp_path, "run", "--", "vecten", "status").returncode == 7


def test_the_command_sees_only_the_test_credentials(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, vecten='echo "$AWS_PROFILE ${AWS_ACCESS_KEY_ID:-unset} $GH_TOKEN"')
    personal = {"AWS_PROFILE": "personal", "AWS_ACCESS_KEY_ID": "AKIAPERSONAL", "GH_TOKEN": "p"}
    result = access(tmp_path, "run", "--github", "upstream", "--", "vecten", env=personal)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "livecheck unset ghs_fake_upstream_token\n"


def test_a_hanging_probe_is_a_refusal(tmp_path):
    home(tmp_path)
    marker = tmp_path / "started"
    fake_clis(tmp_path, aws="exec sleep 30", vecten=f"touch {marker}")
    result = access(tmp_path, "run", "--", "vecten", timeout=1.0)
    assert result.returncode == 3
    assert "aws: identity could not be read" in result.stderr
    assert not marker.exists()


def test_a_failing_probe_is_a_refusal(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, snow="echo 'connection refused' >&2; exit 1")
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 3
    assert "snowflake: identity could not be read" in result.stderr


def test_a_missing_cli_is_a_refusal(tmp_path):
    home(tmp_path)
    (fake_clis(tmp_path) / "snow").unlink()
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 3
    assert "snowflake: identity could not be read" in result.stderr
    assert "snowflake: snow could not start" in result.stderr


def test_an_identity_left_out_of_allow_toml_is_not_probed(tmp_path):
    allow = home(tmp_path) / "allow.toml"
    allow.write_text(ALLOW_TOML.replace('[snowflake]\naccount = "EXAMPLE"\n', ""))
    (fake_clis(tmp_path, vecten="exit 0") / "snow").unlink()
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 0, result.stderr


def test_relayed_probe_errors_are_redacted(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, aws=f"echo 'InvalidClientTokenId: {AWS_SECRET} rejected' >&2; exit 254")
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 3
    assert AWS_SECRET not in result.stderr + result.stdout
    assert "InvalidClientTokenId: *** rejected" in result.stderr


def test_a_refusal_never_repeats_a_secret_a_probe_printed(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, aws=f"echo '{AWS_SECRET}'")
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 3
    assert AWS_SECRET not in result.stderr
    assert "aws: account *** is not the live-check account" in result.stderr


def test_the_minted_token_is_redacted_too(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, gh='echo "bad credentials for $GH_TOKEN" >&2; exit 1')
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 3
    assert "ghs_fake_sandbox_token" not in result.stderr
    assert "bad credentials for ***" in result.stderr


def test_a_missing_credential_home_is_a_refusal(tmp_path):
    fake_clis(tmp_path)
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 3
    assert result.stderr.count("\n") == 1 and str(tmp_path / "home") in result.stderr


def test_a_missing_allow_list_is_a_refusal(tmp_path):
    (home(tmp_path) / "allow.toml").unlink()
    fake_clis(tmp_path)
    result = access(tmp_path, "run", "--", "vecten")
    assert result.returncode == 3
    assert result.stderr.count("\n") == 1 and str(tmp_path / "home/allow.toml") in result.stderr


def test_secret_values_skips_allow_toml_and_short_strings(tmp_path):
    root = home(tmp_path)
    secrets = mod().secret_values(root)
    assert AWS_SECRET in secrets
    assert "AKIAIOSFODNN7EXAMPLE" in secrets
    assert "22222222-2222-2222-2222-222222222222" in secrets
    assert "correct-horse-pony" in secrets
    assert ORG not in secrets and "vecten/vecten-open" not in secrets
    assert "fr-par" not in secrets and "12345" not in secrets and "app.pem" not in secrets
    assert all(len(value) >= 16 for value in secrets)


def test_secret_values_skips_hostnames_logs_and_gh_state(tmp_path):
    root = home(tmp_path)
    files = {
        "google/hostnames.txt": "mcp.aws-postgres.sandbox.example.com\n",
        "snowflake/logs/snowflake-cli.log": "2026-11-19 10:00:00 INFO connected to EXAMPLE\n",
        "github/gh/config.yml": "git_protocol: https-for-the-live-checks\n",
        "home/.cache/tool/state": "cached-state-of-some-tool-run\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    secrets = mod().secret_values(root)
    assert AWS_SECRET in secrets
    assert (
        not {
            "mcp.aws-postgres.sandbox.example.com",
            "2026-11-19 10:00:00 INFO connected to EXAMPLE",
            "https-for-the-live-checks",
            "git_protocol: https-for-the-live-checks",
            "cached-state-of-some-tool-run",
        }
        & secrets
    )


def test_secret_values_skips_the_git_identity_and_the_google_client_id(tmp_path):
    root = home(tmp_path)
    files = {
        "git/config": "[user]\n\tname = example-livecheck[bot]\n"
        "\temail = 12345+example-livecheck[bot]@users.noreply.github.com\n",
        "google/oidc-client-id": "123456789012-example.apps.googleusercontent.com\n",
        "google/oidc-client-secret": "GOCSPX-example-client-secret\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    secrets = mod().secret_values(root)
    assert "GOCSPX-example-client-secret" in secrets
    assert (
        not {
            "example-livecheck[bot]",
            "12345+example-livecheck[bot]@users.noreply.github.com",
            "123456789012-example.apps.googleusercontent.com",
        }
        & secrets
    )


def test_secret_values_takes_values_not_whole_lines(tmp_path):
    root = home(tmp_path)
    key_lines = [
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun",
        "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK",
        "AbcdefghijklmnopqrstuvwxyzABCDEF==",
        "-----END RSA PRIVATE KEY-----",
    ]
    (root / "github/app-private-key").write_text("\n".join(key_lines) + "\n")
    (root / "google/oidc-client-secret").parent.mkdir()
    (root / "google/oidc-client-secret").write_text("GOCSPX-madeUpClientSecret123\n")
    secrets = mod().secret_values(root)
    assert {AWS_SECRET, "AKIAIOSFODNN7EXAMPLE", "GOCSPX-madeUpClientSecret123"} <= secrets
    assert set(key_lines) <= secrets
    assert (
        not {
            "[livecheck]",
            "[profile livecheck]",
            "region = eu-west-1",
            f"aws_secret_access_key = {AWS_SECRET}",
            'app_id = "12345"',
            "[connections.livecheck]",
        }
        & secrets
    )


def test_secret_values_skips_identifiers_and_keeps_credentials(tmp_path):
    root = home(tmp_path)
    identifiers = {
        "EXAMPLEORG-EXAMPLEACCOUNT",
        "LIVECHECK_SERVICE_USER",
        "LIVECHECK_ROLE_EXAMPLE",
        "LIVECHECK_WAREHOUSE_EXAMPLE",
        "SNOWFLAKE_JWT_EXAMPLE",
        "/Users/example/.config/vecten-livecheck/snowflake/livecheck.p8",
        ORG,
        "11111111-1111-1111-1111-111111111111",
        "2026-11-19T10:00:00Z",
    }
    files = {
        "snowflake/connections.toml": (
            "[livecheck]\n"
            'account = "EXAMPLEORG-EXAMPLEACCOUNT"\n'
            'user = "LIVECHECK_SERVICE_USER"\n'
            'role = "LIVECHECK_ROLE_EXAMPLE"\n'
            'warehouse = "LIVECHECK_WAREHOUSE_EXAMPLE"\n'
            'Authenticator = "SNOWFLAKE_JWT_EXAMPLE"\n'
            'private_key_file = "/Users/example/.config/vecten-livecheck/snowflake/livecheck.p8"\n'
            'password = "an-example-snowflake-password"\n'
        ),
        "scw/config.yaml": (
            "access_key: SCWEXAMPLEACCESSKEY1\n"
            "secret_key: 22222222-2222-2222-2222-222222222222\n"
            f"default_organization_id: {ORG}\n"
            "default_project_id: 11111111-1111-1111-1111-111111111111\n"
            "default_region: fr-par\n"
            "default_zone: fr-par-1\n"
        ),
        "cache/sandbox.json": json.dumps(
            {"token": "ghs_exampleInstallationToken0001", "expires_at": "2026-11-19T10:00:00Z"}
        ),
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    secrets = mod().secret_values(root)
    assert not identifiers & secrets
    assert {
        "an-example-snowflake-password",
        "22222222-2222-2222-2222-222222222222",
        "SCWEXAMPLEACCESSKEY1",
        "ghs_exampleInstallationToken0001",
    } <= secrets


def test_redact_replaces_every_secret_of_eight_or_more_characters():
    text = "key abcdefgh and abcdefghijkl and short1"
    assert mod().redact(text, {"abcdefgh", "abcdefghijkl", "short1"}) == (
        "key *** and *** and short1"
    )


def test_doctor_reports_every_identity_and_fails_on_one(tmp_path):
    home(tmp_path)
    other = "99999999-9999-9999-9999-999999999999"
    fake_clis(tmp_path, scw=printing(SCW_OUTPUT.replace(ORG, other)))
    result = access(tmp_path, "doctor")
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        TOOLS_OK,
        "aws ok",
        f"scaleway FAIL: organization {other} is not the live-check organization",
        "snowflake ok",
        "github-sandbox ok",
        "github-upstream ok",
        "os-sandbox absent (not macOS)",
    ]


def test_doctor_passes_when_every_identity_is_allowed(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path)
    result = access(tmp_path, "doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == TOOLS_OK
    assert all(line.endswith(" ok") for line in lines[1:-1])
    assert lines[-1] == "os-sandbox absent (not macOS)"


def test_doctor_fails_an_identity_missing_from_allow_toml(tmp_path):
    allow = home(tmp_path) / "allow.toml"
    allow.write_text(ALLOW_TOML.replace(f'[scaleway]\norganization_id = "{ORG}"\n', ""))
    fake_clis(tmp_path)
    fake, _ = recording_sandbox(tmp_path)
    result = access(tmp_path, "doctor", PLATFORM="darwin", SANDBOX_EXEC=str(fake))
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        TOOLS_OK,
        "aws ok",
        "scaleway FAIL: not configured in allow.toml",
        "snowflake ok",
        "github-sandbox ok",
        "github-upstream ok",
        "os-sandbox ok",
    ]


def test_doctor_fails_when_the_os_sandbox_cannot_start(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path)
    fake = script(
        tmp_path / "sandbox-exec", f"echo 'sandbox_apply: denied {AWS_SECRET}' >&2; exit 71"
    )
    result = access(tmp_path, "doctor", PLATFORM="darwin", SANDBOX_EXEC=str(fake))
    assert result.returncode == 1
    assert (
        result.stdout.splitlines()[-1]
        == "os-sandbox FAIL: sandbox-exec exited 71: sandbox_apply: denied ***"
    )
    assert AWS_SECRET not in result.stdout


def test_doctor_redacts_a_value_it_refuses(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, aws=f"echo '{AWS_SECRET}'")
    result = access(tmp_path, "doctor")
    assert result.stdout.splitlines()[1] == "aws FAIL: account *** is not the live-check account"
    assert AWS_SECRET not in result.stdout


def test_the_sandbox_profile_denies_the_maintainers_credentials():
    profile = mod().sandbox_profile(EXAMPLE_HOME, EXAMPLE_LIVE, EXAMPLE_TOOLS, EXAMPLE_PYTHON)
    assert profile == DENIED_TEXT


def test_a_path_the_profile_cannot_quote_is_a_refusal():
    m = mod()
    for odd in ('/Users/ex"ample', "/Users/ex\\ample"):
        for paths in (
            (Path(odd), EXAMPLE_LIVE, EXAMPLE_TOOLS, EXAMPLE_PYTHON),
            (EXAMPLE_HOME, Path(odd), EXAMPLE_TOOLS, EXAMPLE_PYTHON),
            (EXAMPLE_HOME, EXAMPLE_LIVE, EXAMPLE_TOOLS, [Path(odd)]),
            (EXAMPLE_HOME, EXAMPLE_LIVE, Path(odd), EXAMPLE_PYTHON),
        ):
            with pytest.raises(m.Refused):
                m.sandbox_profile(*paths)


def test_the_env_file_pattern_escapes_the_home_and_matches_only_env_files():
    m = mod()
    pattern = re.compile(m.env_files_pattern("/Users/ex.ample+1"))
    for denied in ("proj/.env", ".env", "a/b/.env.local", "proj/.env.production"):
        assert pattern.match(f"/Users/ex.ample+1/{denied}"), denied
    for allowed in ("proj/.envrc", "proj/x.env", "proj/.env/inside", "proj/.env."):
        assert not pattern.match(f"/Users/ex.ample+1/{allowed}"), allowed
    assert not pattern.match("/Users/exXample+1/proj/.env")
    assert not pattern.match("/Users/other/proj/.env")


def recording_sandbox(tmp_path):
    """A fake sandbox-exec that appends its argv and environment to records, then runs the
    command."""
    record = tmp_path / "sandbox-argv"
    body = (
        f'printf "%s\\n" "$@" --- >> {record}\n'
        f"env >> {record}.env; echo --- >> {record}.env\n"
        'shift 2\nexec "$@"'
    )
    return script(tmp_path / "sandbox-exec", body), record


def invocations(record):
    return [call.splitlines() for call in record.read_text().split("---\n") if call]


def environments(record):
    calls = Path(f"{record}.env").read_text().split("---\n")
    return [dict(line.split("=", 1) for line in call.splitlines()) for call in calls if call]


def interpreter():
    return [Path(prefix).resolve() for prefix in (sys.base_prefix, sys.prefix)]


def test_on_macos_probes_and_command_run_inside_the_os_sandbox(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, vecten="exit 7")
    fake, record = recording_sandbox(tmp_path)
    result = access(
        tmp_path, "run", "--", "vecten", "status", PLATFORM="darwin", SANDBOX_EXEC=str(fake)
    )
    assert result.returncode == 7, result.stderr
    profile = mod().sandbox_profile(tmp_path / "real", tmp_path / "home", REPO, interpreter())
    calls = invocations(record)
    assert all(call[:2] == ["-p", profile] for call in calls)
    assert [call[2] for call in calls] == ["aws", "scw", "snow", "gh", "vecten"]
    assert calls[-1][2:] == ["vecten", "status"]


def test_a_sandbox_that_cannot_start_is_a_refusal(tmp_path):
    home(tmp_path)
    marker = tmp_path / "started"
    fake_clis(tmp_path, vecten=f"touch {marker}")
    fake = script(tmp_path / "sandbox-exec", "echo 'sandbox_apply: not permitted' >&2; exit 71")
    result = access(tmp_path, "run", "--", "vecten", PLATFORM="darwin", SANDBOX_EXEC=str(fake))
    assert result.returncode == 3
    for name in ("aws", "scaleway", "snowflake", "github"):
        assert f"{name}: identity could not be read" in result.stderr
    assert not marker.exists()


def test_elsewhere_run_executes_the_command_directly(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, vecten="exit 7")
    fake, record = recording_sandbox(tmp_path)
    result = access(tmp_path, "run", "--", "vecten", PLATFORM="linux", SANDBOX_EXEC=str(fake))
    assert result.returncode == 7, result.stderr
    assert not record.exists()


def os_sandbox_works():
    try:
        trial = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"]
        return subprocess.run(trial, capture_output=True).returncode == 0
    except OSError:
        return False


def listening(path):
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(8)
    return server


@pytest.mark.skipif(not os_sandbox_works(), reason="needs a usable macOS sandbox-exec")
def test_the_os_sandbox_denies_the_credential_files_and_sockets(tmp_path):
    real = tmp_path / "real"
    (real / ".aws").mkdir(parents=True)
    (real / ".aws/credentials").write_text("[default]\n")
    (real / ".netrc").write_text("machine example.invalid\n")
    (real / "notes.txt").write_text("readable\n")
    (real / ".pgpass").write_text("localhost:5432:*:example:example\n")
    for project in ("proj", "vecten-sandbox/proj"):
        (real / project).mkdir(parents=True)
        (real / project / ".env").write_text("A=1\n")
    (real / ".claude").mkdir()
    (real / ".claude/settings.json").write_text("{}\n")
    (real / ".gitconfig").write_text("")
    (real / ".config/git").mkdir(parents=True)
    live = tmp_path / "live"
    (live / "cache").mkdir(parents=True)
    (live / "home").mkdir()
    (live / "allow.toml").write_text("")
    tools = tmp_path / "tools"
    (tools / "scripts/live").mkdir(parents=True)
    (tools / "scripts/live/access.py").write_text("")
    python = tmp_path / "python"
    (python / "lib").mkdir(parents=True)
    # A unix socket path must stay under about 100 bytes, so the sockets live in a short dir.
    sockets = Path(tempfile.mkdtemp(dir="/tmp"))
    server = listening(sockets / "agent.sock")
    try:
        profile = mod().sandbox_profile(real, live, tools, [python])

        def sandboxed(*argv):
            command = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
            return subprocess.run(command, capture_output=True).returncode

        def appended(path):
            return sandboxed("/bin/sh", "-c", 'printf x >> "$1"', "sh", str(path))

        assert sandboxed("/bin/cat", str(real / ".aws/credentials")) != 0
        assert sandboxed("/bin/cat", str(real / ".netrc")) != 0
        assert sandboxed("/bin/cat", str(real / "notes.txt")) == 0
        assert sandboxed("/bin/cat", str(real / ".pgpass")) != 0
        assert sandboxed("/bin/cat", str(real / "proj/.env")) != 0
        assert sandboxed("/bin/cat", str(real / "vecten-sandbox/proj/.env")) == 0
        assert appended(live / "allow.toml") != 0
        assert (live / "allow.toml").read_text() == ""
        assert appended(live / "home/state") == 0
        assert appended(live / "cache/token.json") != 0
        assert appended(real / ".claude/settings.json") != 0
        assert appended(tools / "scripts/live/access.py") != 0
        assert appended(tools / "new-file") != 0
        assert (tools / "scripts/live/access.py").read_text() == ""
        assert not (tools / "new-file").exists()
        assert appended(python / "lib/sitecustomize.py") != 0
        assert appended(real / ".gitconfig") != 0
        assert appended(real / ".config/git/config") != 0
        connect = [
            sys.executable,
            "-c",
            "import socket, sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])",
            str(sockets / "agent.sock"),
        ]
        assert subprocess.run(connect, capture_output=True).returncode == 0
        assert sandboxed(*connect) != 0
    finally:
        server.close()
        shutil.rmtree(sockets)


def test_secrets_check_names_the_tracked_files_holding_a_secret(tmp_path):
    home(tmp_path)
    project = tmp_path / "project"
    (project / "infra").mkdir(parents=True)
    git(project, "init", "-q")
    (project / "infra/leak.tf").write_text(f'secret = "{AWS_SECRET}"\n')
    (project / "README.md").write_text("# Sandbox\n")
    (project / "untracked.txt").write_text(AWS_SECRET)
    git(project, "add", "infra/leak.tf", "README.md")
    result = access(tmp_path, "secrets-check", str(project))
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "infra/leak.tf  (in the working tree, staged)",
        "untracked.txt  (in the working tree)",
    ]
    assert AWS_SECRET not in result.stdout + result.stderr


def test_secrets_check_is_silent_on_a_clean_project(tmp_path):
    home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    (project / "README.md").write_text("# Sandbox\n")
    git(project, "add", "README.md")
    result = access(tmp_path, "secrets-check", str(project))
    assert (result.returncode, result.stdout) == (0, "")


def test_the_default_token_command_runs_github_app_beside_the_wrapper():
    command = mod().TOKEN_COMMAND
    assert command == [sys.executable, "-I", str(SCRIPT.with_name("github_app.py")), "token"]


def test_run_cwd_moves_the_command_but_not_the_probes(tmp_path):
    home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    log = tmp_path / "probe-cwd"
    fake_clis(tmp_path, aws=f"pwd -P > {log}\n{printing(AWS_OUTPUT)}", vecten="pwd -P")
    result = access(tmp_path, "run", "--cwd", str(project), "--", "vecten", "deploy")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{project.resolve()}\n"
    assert log.read_text() == f"{tmp_path.resolve()}\n"


def test_run_cwd_must_exist(tmp_path):
    home(tmp_path)
    marker = tmp_path / "started"
    fake_clis(tmp_path, vecten=f"touch {marker}")
    result = access(tmp_path, "run", "--cwd", str(tmp_path / "absent"), "--", "vecten")
    assert result.returncode == 3
    assert len(result.stderr.strip().splitlines()) == 1
    assert "absent" in result.stderr
    assert not marker.exists()


def test_run_refuses_an_allowed_name_given_as_a_path(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path)
    marker = tmp_path / "started"
    script(tmp_path / "cli/bin/vecten", f"touch {marker}")
    script(tmp_path / "vecten", f"touch {marker}")
    for path in (str(tmp_path / "cli/bin/vecten"), "./vecten"):
        result = access(tmp_path, "run", "--cwd", str(tmp_path), "--", path, "version")
        assert result.returncode == 2, path
        assert len(result.stderr.strip().splitlines()) == 1
        assert "path" in result.stderr
    assert not marker.exists()


def test_run_stdin_file_feeds_the_command_without_printing_it(tmp_path):
    root = home(tmp_path)
    secret = "GOCSPX-example-client-secret"
    (root / "google").mkdir()
    (root / "google/oidc-client-secret").write_text(f"{secret}\n")
    received = tmp_path / "received"
    fake_clis(tmp_path, vecten=f"cat > {received}")
    result = access(
        tmp_path, "run", "--stdin-file", "google/oidc-client-secret", "--", "vecten", "secret"
    )
    assert result.returncode == 0, result.stderr
    assert received.read_text() == f"{secret}\n"
    assert secret not in result.stdout + result.stderr


def test_run_stdin_file_must_stay_inside_the_credential_home(tmp_path):
    root = home(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not in the credential home\n")
    (root / "google").mkdir()
    (root / "google/link").symlink_to(outside)
    marker = tmp_path / "started"
    fake_clis(tmp_path, vecten=f"touch {marker}")
    for path in ("../outside.txt", str(outside), "google/link", "google/absent"):
        result = access(tmp_path, "run", "--stdin-file", path, "--", "vecten")
        assert result.returncode == 3, path
        assert len(result.stderr.strip().splitlines()) == 1
        assert "not in the credential home" not in result.stdout + result.stderr
    assert not marker.exists()


def test_probes_get_no_standard_input(tmp_path):
    home(tmp_path)
    seen = tmp_path / "probe-stdin"
    fake_clis(tmp_path, aws=f"cat > {seen}\n{printing(AWS_OUTPUT)}", vecten="cat")
    result = access(tmp_path, "run", "--", "vecten", stdin="piped value\n")
    assert result.returncode == 0, result.stderr
    assert seen.read_text() == ""
    assert result.stdout == "piped value\n"


def show_home(tmp_path):
    root = home(tmp_path)
    (root / "google").mkdir()
    (root / "google/hostnames.txt").write_text(
        "aws-postgres aws-postgres.aws.example.test\n"
        "scaleway-postgres scaleway-postgres.scw.example.test\n"
    )
    (root / "google/oidc-client-id").write_text("123456789012-example.apps.googleusercontent.com\n")
    (root / "google/admin.toml").write_text('email = "admin@example.test"\nfirstname = "Ada"\n')
    return root


@pytest.mark.parametrize(
    ("args", "value"),
    [
        (["sandbox-org"], "example-sandbox"),
        (["upstream-repo"], "vecten/vecten-open"),
        (["hostname", "scaleway-postgres"], "scaleway-postgres.scw.example.test"),
        (["oidc-client-id"], "123456789012-example.apps.googleusercontent.com"),
        (["admin-email"], "admin@example.test"),
        (["admin-firstname"], "Ada"),
    ],
)
def test_show_prints_exactly_one_value(tmp_path, args, value):
    show_home(tmp_path)
    result = access(tmp_path, "show", *args)
    assert (result.returncode, result.stdout, result.stderr) == (0, f"{value}\n", "")


def test_show_refuses_without_printing_file_content(tmp_path):
    root = show_home(tmp_path)
    cases = [
        ["password"],
        ["hostname"],
        ["hostname", "aws-snowflake"],
        ["sandbox-org", "aws-postgres"],
    ]
    for args in cases:
        result = access(tmp_path, "show", *args)
        assert result.returncode == 3, args
        assert result.stdout == ""
        assert len(result.stderr.strip().splitlines()) == 1
        assert "example.test" not in result.stderr and "Ada" not in result.stderr
    (root / "google/admin.toml").write_text('email = "admin@example.test"\n')
    result = access(tmp_path, "show", "admin-firstname")
    assert (result.returncode, result.stdout) == (3, "")
    assert "admin@example.test" not in result.stderr
    (root / "google/oidc-client-id").unlink()
    result = access(tmp_path, "show", "oidc-client-id")
    assert (result.returncode, result.stdout) == (3, "")
    assert len(result.stderr.strip().splitlines()) == 1


def test_secret_values_skips_the_admin_identity(tmp_path):
    root = show_home(tmp_path)
    (root / "google/admin.toml").write_text(
        'email = "first.admin@example-company.test"\nfirstname = "Bartholomew-Alexander"\n'
    )
    secrets = mod().secret_values(root)
    assert not {"first.admin@example-company.test", "Bartholomew-Alexander"} & secrets


def test_show_never_prints_a_credential_value(tmp_path):
    root = show_home(tmp_path)
    secret = "GOCSPX-example-client-secret"
    (root / "google/oidc-client-secret").write_text(f"{secret}\n")
    (root / "google/oidc-client-id").write_text(f"{secret}\n")
    result = access(tmp_path, "show", "oidc-client-id")
    assert (result.returncode, result.stdout) == (3, "")
    assert len(result.stderr.strip().splitlines()) == 1
    assert secret not in result.stderr


def test_a_refused_stdin_file_starts_no_probe(tmp_path):
    home(tmp_path)
    probed = tmp_path / "probed"
    fake_clis(tmp_path, aws=f"touch {probed}\n{printing(AWS_OUTPUT)}")
    result = access(tmp_path, "run", "--stdin-file", "google/absent", "--", "vecten")
    assert result.returncode == 3
    assert not probed.exists()


def test_stdin_file_reaches_the_command_when_descriptor_0_starts_closed(tmp_path):
    root = home(tmp_path)
    (root / "google").mkdir()
    (root / "google/oidc-client-secret").write_text("GOCSPX-example-client-secret\n")
    received = tmp_path / "received"
    fake_clis(tmp_path, vecten=f"cat > {received}")
    token = script(tmp_path / "token", 'echo "ghs_fake_$(basename "$1" .toml)_token"')
    settings = {
        "TOKEN_COMMAND": [str(token)],
        "PLATFORM": "linux",
        "real_home": str(tmp_path / "real"),
    }
    args = ["run", "--stdin-file", "google/oidc-client-secret", "--", "vecten"]
    result = subprocess.run(
        [sys.executable, "-c", DRIVER, str(SCRIPT), json.dumps(settings), *args],
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "VECTEN_LIVE_HOME": str(root),
        },
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        preexec_fn=lambda: os.close(0),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert received.read_text() == "GOCSPX-example-client-secret\n"


def tools_copy(tmp_path, python=True):
    """A copy of the launcher in a made-up tools copy whose interpreter prints its arguments."""
    root = tmp_path / "tools"
    launcher = root / "scripts/live/live"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(LAUNCHER, launcher)
    if python:
        script(root / ".venv/bin/python", 'printf "%s\\n" "$@"')
    return root.resolve(), launcher


def launch(path, *args, env=None):
    return subprocess.run(
        [str(path), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        cwd=path.parent.parent,
        timeout=30,
    )


def test_the_launcher_is_checked_in_executable():
    assert os.access(LAUNCHER, os.X_OK)
    assert LAUNCHER.read_text().startswith("#!/bin/sh")


def test_the_launcher_runs_the_wrapper_beside_it_with_its_own_interpreter(tmp_path):
    root, launcher = tools_copy(tmp_path)
    result = launch(launcher, "doctor")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == f"-I\n{root}/scripts/live/access.py\ndoctor\n"
    spaced = launch(launcher, "run", "--", "git", "commit", "-m", "a b  c")
    assert spaced.stdout.splitlines()[2:] == ["run", "--", "git", "commit", "-m", "a b  c"]


def test_the_launcher_resolves_a_symlink_to_itself(tmp_path):
    root, launcher = tools_copy(tmp_path)
    expected = f"-I\n{root}/scripts/live/access.py\ndoctor\n"
    (tmp_path / "bin").mkdir()
    absolute = tmp_path / "bin/live"
    absolute.symlink_to(launcher)
    relative = tmp_path / "bin/relative-live"
    relative.symlink_to(os.path.relpath(launcher, relative.parent))
    chained = tmp_path / "chained-live"
    chained.symlink_to(relative)
    for link in (absolute, relative, chained):
        result = launch(link, "doctor")
        assert (result.returncode, result.stdout) == (0, expected), link


def test_the_launcher_ignores_shell_functions_from_the_environment(tmp_path):
    root, launcher = tools_copy(tmp_path)
    hijack = "() { echo hijacked; }"
    env = {f"BASH_FUNC_{name}%%": hijack for name in ("cd", "pwd", "dirname", "readlink")}
    result = launch(launcher, "doctor", env={**env, "CDPATH": str(tmp_path)})
    assert result.stdout == f"-I\n{root}/scripts/live/access.py\ndoctor\n"


def test_the_launcher_without_an_interpreter_says_how_to_make_one(tmp_path):
    root, launcher = tools_copy(tmp_path, python=False)
    result = launch(launcher, "doctor")
    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1
    assert "uv sync --locked --all-packages" in result.stderr
    assert str(root) in result.stderr


@pytest.mark.parametrize(
    ("args", "script_name"),
    [
        (["combos", "plan", "--json"], "combos.py"),
        (["project", "release", "/elsewhere/proj"], "sandbox_project.py"),
        (["cost", "estimate", "--combinations", "aws-postgres", "--hours", "1"], "cost.py"),
        (["cost", "spent", "--created", "created.json"], "cost.py"),
        (["combos", "--made-up-option"], "combos.py"),
    ],
)
def test_a_sibling_without_credentials_runs_with_the_wrappers_interpreter_and_no_credential(
    tmp_path, monkeypatch, args, script_name
):
    m = mod()
    started = []

    def execve(path, argv, env):
        started.append((path, argv, env))
        raise SystemExit(0)

    monkeypatch.setattr(m.os, "execve", execve)
    monkeypatch.setattr(m, "PLATFORM", "linux")
    monkeypatch.setenv("VECTEN_LIVE_HOME", str(home(tmp_path)))
    monkeypatch.setenv("VECTEN_LIVE", "1")
    monkeypatch.setenv("AWS_PROFILE", "personal")
    monkeypatch.setenv("SOME_SETTING", "dropped")
    with pytest.raises(SystemExit):
        m.main(args)
    [(path, argv, env)] = started
    assert path == sys.executable
    assert argv == [sys.executable, "-I", str(SCRIPT.with_name(script_name)), *args[1:]]
    assert not CREDENTIAL_VARIABLES & env.keys()
    assert "SOME_SETTING" not in env
    assert env["GH_TOKEN"] == "livecheck-github-not-configured"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["HOME"] == str(tmp_path / "home/home-plain")


def throwaway_repository(root, ledger_tag="v1"):
    root.mkdir(parents=True)
    git(root, "init", "-q", "--initial-branch=main")
    (root / "README.md").write_text("x\n")
    git(root, "add", "README.md")
    git(root, *IDENTITY, "commit", "-q", "-m", "first")
    git(root, *IDENTITY, "tag", "v1")
    (root / "terraform/scaleway").mkdir(parents=True)
    (root / "terraform/scaleway/main.tf").write_text("x\n")
    git(root, "add", "terraform")
    git(root, *IDENTITY, "commit", "-q", "-m", "second")
    (root / "scripts/live").mkdir(parents=True)
    combos = ("aws-postgres", "aws-snowflake", "scaleway-postgres", "scaleway-snowflake")
    ledger = "".join(f'{name} = "{ledger_tag}"\n' for name in combos)
    (root / "scripts/live/ledger.toml").write_text(ledger)
    return root


def test_combos_runs_from_the_tools_copy_on_the_checkout_it_is_run_in(tmp_path):
    repo = throwaway_repository(tmp_path / "checkout")
    result = access(tmp_path, "combos", "plan", cwd=repo)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == (
        f"ledger {repo.resolve()}/scripts/live/ledger.toml, map {SCRIPT.with_name('filemap.toml')}"
    )
    assert lines[1].startswith("scaleway-postgres")
    stale = throwaway_repository(tmp_path / "stale", ledger_tag="v9")
    refused = access(tmp_path, "combos", "plan", cwd=stale)
    assert refused.returncode == 3
    assert "combos: " in refused.stderr and "v9" in refused.stderr


def test_cost_estimate_runs_from_the_tools_copy(tmp_path):
    estimate = ["cost", "estimate", "--combinations", "aws-postgres", "--json", "--hours"]
    cheap = access(tmp_path, *estimate, "1")
    assert cheap.returncode == 0, cheap.stderr
    assert json.loads(cheap.stdout)["over_ceiling"] is False
    dear = access(tmp_path, *estimate, "100000")
    assert dear.returncode == 3, dear.stderr
    assert json.loads(dear.stdout)["over_ceiling"] is True
    unknown = access(tmp_path, "cost", "estimate", "--combinations", "gcp-x", "--hours", "1")
    assert unknown.returncode == 2
    assert "gcp-x" in unknown.stderr


def test_the_estimate_cost_runs_is_the_vecten_of_its_own_interpreter():
    spec = importlib.util.spec_from_file_location("cost", SCRIPT.with_name("cost.py"))
    cost = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cost)
    assert cost.ESTIMATE == [sys.executable, "-I", "-m", "vecten.cli.app", "estimate"]


def test_project_check_public_runs_from_the_tools_copy(tmp_path):
    home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    (project / "README.md").write_text("# Sandbox\n")
    git(project, "add", "README.md")
    clean = access(tmp_path, "project", "check-public", str(project))
    assert (clean.returncode, clean.stdout) == (0, ""), clean.stderr
    (project / ".env").write_text("A=1\n")
    git(project, "add", ".env")
    leaking = access(tmp_path, "project", "check-public", str(project))
    expected = ".env  (file name; in the working tree, staged)\n"
    assert (leaking.returncode, leaking.stdout) == (1, expected)


def test_check_public_runs_no_command_the_projects_git_configuration_names(tmp_path):
    home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    (project / "README.md").write_text("# Sandbox\n")
    git(project, "add", "README.md")
    marker = tmp_path / "ran"
    git(project, "config", "core.fsmonitor", f"touch {marker}; true")
    subprocess.run(["git", "ls-files"], cwd=project, capture_output=True)
    assert marker.exists()
    marker.unlink()
    for args in (["project", "check-public", str(project)], ["secrets-check", str(project)]):
        result = access(tmp_path, *args)
        assert result.returncode == 0, result.stderr
    assert not marker.exists()


def recording_cli(record, probe_output):
    """A fake CLI that answers its identity probe and records what any other call sees."""
    return (
        'case "$*" in\n'
        f"  sts*|account*) {printing(probe_output)}\n;;\n"
        f'  *) echo "$VECTEN_LIVE $GH_TOKEN $*" >> {record}; echo refused >&2; exit 1\n;;\n'
        "esac"
    )


@pytest.mark.parametrize(
    "args",
    [["cost", "sweep", "--created", "created.json"], ["candidate", "cut", "--version", "0.1.2"]],
)
def test_credentialed_siblings_start_only_past_the_guard(tmp_path, args):
    home(tmp_path)
    record = tmp_path / "record"
    fake_clis(
        tmp_path,
        aws=recording_cli(record, "222222222222\n"),
        git=f'echo "$*" >> {record}; exit 1',
    )
    (tmp_path / "created.json").write_text('{"environments": []}')
    result = access(tmp_path, *args)
    assert result.returncode == 3
    assert "aws: account 222222222222 is not the live-check account" in result.stderr
    assert result.stdout == ""
    assert not record.exists()


def test_cost_sweep_runs_with_the_test_credentials(tmp_path):
    home(tmp_path)
    record = tmp_path / "record"
    fake_clis(
        tmp_path,
        aws=recording_cli(record, AWS_OUTPUT),
        scw=recording_cli(record, SCW_OUTPUT),
    )
    (tmp_path / "created.json").write_text('{"environments": []}')
    result = access(tmp_path, "cost", "sweep", "--created", "created.json")
    assert result.returncode == 4, result.stderr
    calls = record.read_text().splitlines()
    assert calls
    assert all(call.startswith("1 ghs_fake_sandbox_token ") for call in calls)


def test_candidate_cut_runs_with_the_upstream_token(tmp_path):
    home(tmp_path)
    record = tmp_path / "record"
    fake_clis(tmp_path, git=f'echo "$VECTEN_LIVE $GH_TOKEN $*" >> {record}; exit 1')
    result = access(tmp_path, "candidate", "cut", "--version", "0.1.2")
    assert result.returncode == 3
    assert "candidate: git status failed" in result.stderr
    assert record.read_text().splitlines() == ["1 ghs_fake_upstream_token status --porcelain"]


def test_every_candidate_command_passes_the_guard(tmp_path):
    home(tmp_path)
    record = tmp_path / "record"
    fake_clis(tmp_path, aws="echo 222222222222", git=f'echo "$*" >> {record}; exit 1')
    for args in (["--help"], ["--", "cut", "--version", "0.1.2", "--dry-run"], ["made-up"]):
        result = access(tmp_path, "candidate", *args)
        assert result.returncode == 3, args
        assert "aws: account 222222222222" in result.stderr
        assert result.stdout == ""
    assert not record.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--", "vecten", "status"],
        ["cost", "sweep", "--created", "created.json"],
        ["candidate", "cut", "--version", "0.1.2"],
    ],
)
def test_nothing_credentialed_runs_inside_the_tools_copy(tmp_path, args):
    home(tmp_path)
    probed = tmp_path / "probed"
    fake_clis(tmp_path, aws=f"touch {probed}\n{printing(AWS_OUTPUT)}")
    for where in (REPO, REPO / "scripts/live"):
        result = access(tmp_path, *args, cwd=where)
        assert result.returncode == 3, where
        assert len(result.stderr.strip().splitlines()) == 1
        assert "tools copy" in result.stderr
        assert "working checkout" in result.stderr
    assert not probed.exists()


def test_run_cwd_inside_the_tools_copy_is_refused(tmp_path):
    home(tmp_path)
    probed = tmp_path / "probed"
    fake_clis(tmp_path, aws=f"touch {probed}\n{printing(AWS_OUTPUT)}")
    (tmp_path / "into-tools").symlink_to(REPO / "docs")
    for where in (REPO, REPO / "docs", tmp_path / "into-tools"):
        result = access(tmp_path, "run", "--cwd", str(where), "--", "vecten", "status")
        assert result.returncode == 3, where
        assert len(result.stderr.strip().splitlines()) == 1
        assert "tools copy" in result.stderr
    assert not probed.exists()
    fake_clis(tmp_path, vecten="pwd -P")
    outside = access(tmp_path, "run", "--cwd", str(tmp_path), "--", "vecten")
    assert (outside.returncode, outside.stdout) == (0, f"{tmp_path.resolve()}\n")


def test_doctor_fails_when_run_from_inside_the_tools_copy(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path)
    result = access(tmp_path, "doctor", cwd=REPO / "scripts")
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines[0] == "tools FAIL: running from the directory being worked in"
    assert all(line.endswith(" ok") for line in lines[1:-1])


def test_doctor_prints_the_tools_line_before_it_needs_the_credential_home(tmp_path):
    result = access(tmp_path, "doctor")
    assert result.returncode == 3
    assert result.stdout.splitlines() == [TOOLS_OK]
    assert str(tmp_path / "home") in result.stderr


def test_the_tools_copy_is_the_repository_holding_the_wrapper():
    assert mod().tools_root() == REPO


def test_the_wrapped_command_cannot_write_the_tools_copy(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path, vecten="exit 0")
    fake, record = recording_sandbox(tmp_path)
    result = access(tmp_path, "run", "--", "vecten", PLATFORM="darwin", SANDBOX_EXEC=str(fake))
    assert result.returncode == 0, result.stderr
    profiles = {call[1] for call in invocations(record)}
    assert len(profiles) == 1
    last_file_deny = profiles.pop().split("(deny file-write*")[-1]
    assert f'(subpath "{REPO}")' in last_file_deny
    for prefix in interpreter():
        assert f'(subpath "{prefix}")' in last_file_deny


def project_with_workflow(tmp_path, files=None):
    project = tmp_path / "project"
    (project / ".github/workflows").mkdir(parents=True)
    workflow = project / ".github/workflows/ship.yml"
    workflow.write_text('run: uv tool install "vecten==1.2.3"\n')
    for name, text in (files or {}).items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    git(project, "init", "-q")
    git(project, "add", ".")
    return project, workflow


def uncredentialed(tmp_path, case):
    """The arguments, working directory and script of each sibling call needing no credential."""
    repo = throwaway_repository(tmp_path / "checkout")
    project, _ = project_with_workflow(tmp_path)
    (tmp_path / "created.json").write_text('{"environments": []}')
    estimate = ["cost", "estimate", "--combinations", "aws-postgres", "--hours", "1"]
    return {
        "combos plan": (["combos", "plan"], repo, "combos.py"),
        "combos record": (["combos", "record", "aws-postgres", "v1"], repo, "combos.py"),
        "cost estimate": (estimate, tmp_path, "cost.py"),
        "cost spent": (["cost", "spent", "--created", "created.json"], tmp_path, "cost.py"),
        "project candidate": (
            ["project", "candidate", str(project), "1.2.3"],
            tmp_path,
            "sandbox_project.py",
        ),
        "project check-public": (
            ["project", "check-public", str(project)],
            tmp_path,
            "sandbox_project.py",
        ),
    }[case]


UNCREDENTIALED = (
    "combos plan",
    "combos record",
    "cost estimate",
    "cost spent",
    "project candidate",
    "project check-public",
)


def no_credential_in(env):
    assert not CREDENTIAL_VARIABLES & env.keys()
    assert env["GH_TOKEN"] == "livecheck-github-not-configured"
    assert "!gh auth git-credential" not in env.values()
    assert env["GIT_NO_LAZY_FETCH"] == "1"


@pytest.mark.parametrize("case", UNCREDENTIALED)
def test_an_uncredentialed_sibling_runs_under_the_os_sandbox_without_credentials(tmp_path, case):
    home(tmp_path)
    probed = tmp_path / "probed"
    fake_clis(tmp_path, aws=f"touch {probed}\n{printing(AWS_OUTPUT)}")
    fake, record = recording_sandbox(tmp_path)
    args, cwd, script_name = uncredentialed(tmp_path, case)
    personal = {"VECTEN_LIVE": "1", "AWS_PROFILE": "personal"}
    result = access(
        tmp_path, *args, cwd=cwd, env=personal, PLATFORM="darwin", SANDBOX_EXEC=str(fake)
    )
    assert result.returncode == 0, result.stderr
    assert not probed.exists()
    calls = invocations(record)
    profile = mod().sandbox_profile(tmp_path / "real", tmp_path / "home", REPO, interpreter())
    assert all(call[:2] == ["-p", profile] for call in calls)
    sibling = [sys.executable, "-I", str(SCRIPT.with_name(script_name)), *args[1:]]
    assert calls[-1][2:] == sibling
    for env in environments(record):
        no_credential_in(env)


@pytest.mark.parametrize("case", UNCREDENTIALED)
def test_an_uncredentialed_sibling_fails_closed_without_the_os_sandbox(tmp_path, case):
    home(tmp_path)
    fake_clis(tmp_path)
    fake = script(tmp_path / "sandbox-exec", "echo 'sandbox_apply: not permitted' >&2; exit 71")
    args, cwd, _ = uncredentialed(tmp_path, case)
    watched = [tmp_path / "checkout/scripts/live/ledger.toml"]
    watched.append(tmp_path / "project/.github/workflows/ship.yml")
    before = [path.read_text() for path in watched]
    result = access(tmp_path, *args, cwd=cwd, PLATFORM="darwin", SANDBOX_EXEC=str(fake))
    assert result.returncode != 0
    assert result.stdout == ""
    assert [path.read_text() for path in watched] == before


def test_the_secret_check_lists_files_under_the_os_sandbox(tmp_path):
    home(tmp_path)
    project, _ = project_with_workflow(tmp_path, {"infra/leak.tf": f'secret = "{AWS_SECRET}"\n'})
    fake, record = recording_sandbox(tmp_path)
    result = access(
        tmp_path, "secrets-check", str(project), PLATFORM="darwin", SANDBOX_EXEC=str(fake)
    )
    assert (result.returncode, result.stdout) == (
        1,
        "infra/leak.tf  (in the working tree, staged)\n",
    )
    calls = invocations(record)
    assert [call[2:5] for call in calls] == [["git", "-c", "core.fsmonitor=false"]] * 5
    assert [call[5] for call in calls] == [
        "ls-files",
        "ls-files",
        "rev-parse",
        "rev-list",
        "cat-file",
    ]
    for env in environments(record):
        no_credential_in(env)
    closed = script(tmp_path / "closed-sandbox-exec", "exit 71")
    failed = access(
        tmp_path, "secrets-check", str(project), PLATFORM="darwin", SANDBOX_EXEC=str(closed)
    )
    assert (failed.returncode, failed.stdout) == (2, "")
    assert "cannot read what" in failed.stderr


def test_doctor_reads_the_tools_commit_under_the_os_sandbox(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path)
    fake, record = recording_sandbox(tmp_path)
    result = access(tmp_path, "doctor", PLATFORM="darwin", SANDBOX_EXEC=str(fake))
    assert result.stdout.splitlines()[0] == TOOLS_OK
    calls = invocations(record)
    [index] = [i for i, call in enumerate(calls) if call[2:3] == ["git"]]
    assert calls[index][2:] == ["git", "rev-parse", "--short", "HEAD"]
    no_credential_in(environments(record)[index])


def test_check_public_through_the_wrapper_runs_both_checks(tmp_path):
    home(tmp_path)
    files = {".env": "A=1\n", "infra/main.tf": f'x = "{AWS_SECRET}"\n'}
    project, _ = project_with_workflow(tmp_path, files)
    expected = [
        ".env  (file name; in the working tree, staged)",
        "infra/main.tf  (credential value; in the working tree, staged)",
    ]
    forged = {"VECTEN_LIVE_SECRET_HITS": "[]"}
    fake, _ = recording_sandbox(tmp_path)
    for platform in ("linux", "darwin"):
        result = access(
            tmp_path,
            "project",
            "check-public",
            str(project),
            env=forged,
            PLATFORM=platform,
            SANDBOX_EXEC=str(fake),
        )
        assert (result.returncode, result.stdout.splitlines()) == (1, expected), result.stderr
        assert AWS_SECRET not in result.stdout + result.stderr


def test_check_public_through_the_wrapper_is_refused_where_it_cannot_list(tmp_path):
    home(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    result = access(tmp_path, "project", "check-public", str(plain))
    assert (result.returncode, result.stdout) == (3, "")
    assert "cannot read what" in result.stderr


@pytest.mark.parametrize("args", [["--", "sweep"], [], ["made-up"], ["--help"]])
def test_every_cost_call_but_estimate_and_spent_passes_the_guard(tmp_path, args):
    home(tmp_path)
    fake_clis(tmp_path, aws="echo 222222222222")
    result = access(tmp_path, "cost", *args)
    assert result.returncode == 3
    assert "aws: account 222222222222 is not the live-check account" in result.stderr
    assert result.stdout == ""


def test_doctor_fails_an_interpreter_the_agent_can_write(tmp_path):
    home(tmp_path)
    fake_clis(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    sandbox_python = tmp_path / "real/vecten-sandbox/python"
    for prefix, cwd in ((work / "python", work), (sandbox_python, work)):
        prefix.mkdir(parents=True)
        result = access(tmp_path, "doctor", cwd=cwd, INTERPRETER=[str(prefix), str(prefix)])
        assert result.returncode == 1
        first = result.stdout.splitlines()[0]
        assert first.startswith(f"tools FAIL: its interpreter {prefix.resolve()} is inside")


@pytest.mark.skipif(not os_sandbox_works(), reason="needs a usable macOS sandbox-exec")
def test_a_sibling_run_through_the_wrapper_cannot_write_the_tools_copy(tmp_path):
    tools = tmp_path / "tools/scripts/live"
    tools.mkdir(parents=True)
    for name in ("access.py", "combos.py", "ledger.toml", "filemap.toml"):
        shutil.copy2(SCRIPT.with_name(name), tools / name)
    ledger = tools / "ledger.toml"
    before = ledger.read_text()
    repo = throwaway_repository(tmp_path / "checkout")
    settings = {"PLATFORM": "darwin", "real_home": str(tmp_path / "real")}
    args = ["combos", "record", "--ledger", str(ledger), "aws-postgres", "v1"]
    result = subprocess.run(
        [sys.executable, "-c", DRIVER, str(tools / "access.py"), json.dumps(settings), *args],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "VECTEN_LIVE_HOME": str(home(tmp_path)),
        },
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr
    assert ledger.read_text() == before


def test_secret_values_follows_no_link(tmp_path):
    root = home(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("a-value-outside-the-credential-home\n")
    (root / "cache").mkdir()
    (root / "cache/sandbox.json").symlink_to(outside / "notes.txt")
    (root / "cache/linked-directory").symlink_to(outside, target_is_directory=True)
    secrets = mod().secret_values(root)
    assert AWS_SECRET in secrets
    assert "a-value-outside-the-credential-home" not in secrets


@pytest.mark.parametrize(
    "args",
    [
        ["--", "check-public", "{project}"],
        ["check-public", "--", "{project}"],
        ["check-public", "{project}", "--"],
    ],
)
def test_check_public_in_any_other_spelling_is_refused(tmp_path, args):
    home(tmp_path)
    project, _ = project_with_workflow(tmp_path, {".env": "A=1\n"})
    given = [arg.format(project=project) for arg in args]
    result = access(tmp_path, "project", *given)
    assert (result.returncode, result.stdout) == (2, "")
    assert len(result.stderr.strip().splitlines()) == 1
    assert "check-public <project>" in result.stderr


def test_runs_without_credentials_have_a_home_of_their_own(tmp_path):
    root = home(tmp_path)
    m = mod()
    plain = m.tool_env(root, {"PATH": "/bin"}, "", credentials=False)
    credentialed = m.tool_env(root, {"PATH": "/bin"}, "ghs_minted")
    assert plain["HOME"] == str(root / "home-plain")
    assert credentialed["HOME"] == str(root / "home")
    assert stat.S_IMODE((root / "home-plain").stat().st_mode) == 0o700


@pytest.mark.parametrize("name", ["home", "home-plain"])
def test_a_home_that_is_not_a_real_directory_is_refused(tmp_path, name):
    root = home(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / name).symlink_to(elsewhere, target_is_directory=True)
    marker = tmp_path / "started"
    fake_clis(tmp_path, vecten=f"touch {marker}")
    estimate = ["cost", "estimate", "--combinations", "aws-postgres", "--hours", "1"]
    result = access(tmp_path, *(["run", "--", "vecten"] if name == "home" else estimate))
    assert result.returncode == 3
    assert f"{root / name} is not a directory" in result.stderr
    assert not marker.exists()
    assert list(elsewhere.iterdir()) == []


def test_the_cache_is_not_among_the_places_a_wrapped_command_writes():
    assert "cache" not in mod().LIVE_WRITABLE


@pytest.mark.skipif(not os_sandbox_works(), reason="needs a usable macOS sandbox-exec")
def test_the_os_sandbox_holds_against_renames_and_any_checkouts_agent_files(tmp_path):
    real = tmp_path / "real"
    checkout = real / "elsewhere/checkout"
    (checkout / ".claude/hooks").mkdir(parents=True)
    (checkout / ".claude/settings.local.json").write_text("{}\n")
    (checkout / ".claude/worktrees").mkdir()
    (real / ".claude/plugins").mkdir(parents=True)
    live = tmp_path / "live"
    live.mkdir()
    tools = tmp_path / "anc/b/tools"
    tools.mkdir(parents=True)
    profile = mod().sandbox_profile(real, live, tools, [tmp_path / "python"])
    work = tmp_path / "work"
    work.mkdir()

    def sandboxed(script):
        command = ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", script]
        return subprocess.run(command, cwd=work, capture_output=True, text=True)

    anc = tmp_path / "anc"
    renamed = sandboxed(
        f"mv {anc}/b {anc}/x && mkdir -p {anc}/x/tools && echo planted > {anc}/x/tools/f"
        f" && mv {anc}/x {anc}/b"
    )
    assert renamed.returncode != 0
    assert "Operation not permitted" in renamed.stderr
    assert not (tools / "f").exists()
    assert (anc / "b/tools").is_dir()
    for target in (
        checkout / ".claude/settings.local.json",
        checkout / ".claude/settings.json",
        checkout / ".claude/hooks/pre.sh",
        real / ".claude/plugins/hook.sh",
        real / ".claude/settings.json",
    ):
        written = sandboxed(f'printf x >> "{target}"')
        assert written.returncode != 0, target
    assert (checkout / ".claude/settings.local.json").read_text() == "{}\n"
    moved = sandboxed(f'mv "{checkout}/.claude" "{checkout}/claude-moved"')
    assert moved.returncode != 0
    assert (checkout / ".claude/settings.local.json").exists()
    assert sandboxed(f'printf x >> "{checkout}/.claude/worktrees/notes"').returncode == 0
    away = tmp_path / "away"
    away.mkdir()
    relocated = sandboxed(
        f'mv "{checkout}" "{away}/checkout"'
        f' && printf x >> "{away}/checkout/.claude/settings.local.json"; written=$?;'
        f' mv "{away}/checkout" "{checkout}"; exit $written'
    )
    assert relocated.returncode != 0
    assert "Operation not permitted" in relocated.stderr
    assert (checkout / ".claude/settings.local.json").read_text() == "{}\n"


def test_the_control_file_pattern_matches_by_name_anywhere():
    pattern = re.compile(mod().CLAUDE_CONTROL)
    for denied in (
        "/private/tmp/proj/.claude/settings.local.json",
        "/private/tmp/proj/.claude/settings.json",
        "/private/tmp/proj/.claude",
        "/private/tmp/proj/.claude/hooks",
        "/Users/example/src/checkout/.claude/hooks/pre.sh",
        "/var/folders/x/.claude/skills/a/SKILL.md",
        "/opt/proj/.claude/commands/go.md",
        "/opt/proj/.claude/agents/reviewer.md",
    ):
        assert pattern.match(denied), denied
    for allowed in (
        "/private/tmp/proj/.claude-plugin/plugin.json",
        "/private/tmp/proj/.claude/worktrees/x/f",
        "/private/tmp/proj/.claude/live-checks-0.1.2.md",
        "/private/tmp/proj/x.claude/settings.json",
        "/private/tmp/proj/.claude/settings.json.bak",
        "/private/tmp/proj/.claude/hooksmith/x",
    ):
        assert not pattern.match(allowed), allowed


REWRITE = "removing the file is not enough: rewrite the history or render the project afresh"


def publishable(tmp_path):
    """A project with one clean commit on main, as `vecten github init` leaves it."""
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q", "--initial-branch=main")
    (project / "README.md").write_text("# Sandbox\n")
    git(project, "add", "README.md")
    git(project, *IDENTITY, "commit", "-q", "-m", "first")
    return project


def commit(project, message):
    git(project, "add", "--all")
    git(project, *IDENTITY, "commit", "-q", "-m", message)


def both_checks(tmp_path, project):
    return (
        access(tmp_path, "secrets-check", str(project)),
        access(tmp_path, "project", "check-public", str(project)),
    )


def test_a_credential_still_staged_after_the_file_was_cleaned_is_found(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    (project / "leak.tf").write_text(f'secret = "{AWS_SECRET}"\n')
    git(project, "add", "leak.tf")
    (project / "leak.tf").write_text('secret = "none"\n')
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (1, "leak.tf  (staged)\n")
    assert (public.returncode, public.stdout) == (1, "leak.tf  (credential value; staged)\n")
    assert AWS_SECRET not in secrets.stdout + secrets.stderr + public.stdout + public.stderr


def test_a_credential_committed_and_then_removed_is_found(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    (project / "leak.tf").write_text(f'secret = "{AWS_SECRET}"\n')
    commit(project, "leak")
    git(project, "rm", "-q", "leak.tf")
    commit(project, "remove the leak")
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (1, f"leak.tf  (in a commit: {REWRITE})\n")
    assert (public.returncode, public.stdout) == (
        1,
        f"leak.tf  (credential value; in a commit: {REWRITE})\n",
    )


def test_a_credential_in_an_untracked_file_add_all_would_stage_is_found(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    (project / "notes.txt").write_text(AWS_SECRET)
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (1, "notes.txt  (in the working tree)\n")
    assert public.returncode == 1
    assert public.stdout == "notes.txt  (credential value; in the working tree)\n"


def test_a_credential_only_in_an_ignored_file_passes(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    (project / ".gitignore").write_text("deploy.secret\n")
    commit(project, "ignore")
    (project / "deploy.secret").write_text(AWS_SECRET)
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (0, "")
    assert (public.returncode, public.stdout) == (0, ""), public.stderr


def test_a_credential_on_another_branch_is_found(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    git(project, "switch", "-q", "-c", "other")
    (project / "leak.tf").write_text(f'secret = "{AWS_SECRET}"\n')
    commit(project, "leak on another branch")
    git(project, "switch", "-q", "main")
    assert not (project / "leak.tf").exists()
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (1, f"leak.tf  (in a commit: {REWRITE})\n")
    assert public.returncode == 1
    assert public.stdout == f"leak.tf  (credential value; in a commit: {REWRITE})\n"


def test_a_forbidden_file_name_only_in_an_earlier_commit_is_found(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    (project / "deploy.key").write_text("not a credential of the home\n")
    commit(project, "key")
    git(project, "rm", "-q", "deploy.key")
    commit(project, "remove the key")
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (0, "")
    assert (public.returncode, public.stdout) == (
        1,
        f"deploy.key  (file name; in a commit: {REWRITE})\n",
    )


def test_a_clean_project_with_several_commits_passes(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    for number in range(3):
        (project / f"file{number}.txt").write_text(f"content {number}\n")
        commit(project, f"commit {number}")
    (project / "file0.txt").write_text("changed, not staged\n")
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (0, "")
    assert (public.returncode, public.stdout) == (0, ""), public.stderr


def test_a_project_with_no_commit_yet_is_checked(tmp_path):
    home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    (project / "README.md").write_text("# Sandbox\n")
    (project / "leak.tf").write_text(AWS_SECRET)
    git(project, "add", "--all")
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (1, "leak.tf  (in the working tree, staged)\n")
    assert public.returncode == 1


def test_a_git_that_fails_listing_the_history_is_a_refusal(tmp_path):
    home(tmp_path)
    project = publishable(tmp_path)
    real_git = shutil.which("git")
    script(
        tmp_path / "bin/git",
        'case " $* " in *" rev-list "*) echo "fatal: history unreadable" >&2; exit 128;; esac\n'
        f'exec "{real_git}" "$@"',
    )
    secrets, public = both_checks(tmp_path, project)
    assert (secrets.returncode, secrets.stdout) == (2, "")
    assert "git rev-list failed: fatal: history unreadable" in secrets.stderr
    assert (public.returncode, public.stdout) == (3, "")
    assert "git rev-list failed" in public.stderr
