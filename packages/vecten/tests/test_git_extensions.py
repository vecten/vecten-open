"""`extension add` and `vecten lock` with extensions from a git tag or commit, against a local
`git http-backend`; uv is a stand-in that records its arguments and environment and resolves a
git source only from the copy its git configuration points at."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecten import extensions, locking
from vecten_shared.config import load_config
from vecten_shared.git_sources import SOURCE_REF, checkout, source_sha256

NAME = "acme-resolution"
REPOSITORY = "acme/extensions.git"
SUBDIRECTORY = f"extensions/{NAME}"
PORTAL = "acme-portal"

PYPROJECT = """[project]
name = "{name}"
{version}
dependencies = ["httpx>=0.27"]

[project.optional-dependencies]
platform = ["polars>=1"]

[project.entry-points."vecten.dags"]
resolution = "acme_resolution:dags"

[build-system]
requires = ["hatchling>=1.26"]
build-backend = "hatchling.build"
"""


def _git(*args: str, cwd: Path) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _token() -> str:
    # Letters that are no hex digit and no letter of the words a failure prints.
    return "".join(secrets.choice("GHJKMNPQRSTUVWXYZ") for _ in range(32))


def _leaks(token: str, text: str) -> list[str]:
    return [token[i : i + 4] for i in range(len(token) - 3) if token[i : i + 4] in text]


def _load_git_http():
    name = "vecten_shared_tests_git_http"
    if name not in sys.modules:
        path = Path(__file__).parents[2] / "vecten-shared" / "tests" / "git_http.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class Remote:
    """The served monorepo `acme/extensions.git`, a public `public/tools.git`, by tag."""

    def __init__(self, root: Path, work: Path):
        self.root, self.work, self.commits = root, work, {}
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        (work / "README.md").write_text("shared extensions\n")
        self.release("acme-resolution/v1.2.0", version='version = "1.2.0"')
        self.release("acme-resolution/v1.3.0", version='version = "1.2.9"')
        self.release("acme-resolution/v-dynamic", version='dynamic = ["version"]')
        self.release("acme-resolution/v-link", version='version = "1.2.0"', link=True)
        self.release("acme-resolution/v-renamed", version='version = "1.2.0"', name="acme-other")
        self.release("acme-resolution/v1.4.0", version='version = "1.4.0"')
        self.release(
            "acme-portal/v0.3.0",
            version='version = "0.3.0"',
            name=PORTAL,
            directory=f"extensions/{PORTAL}",
        )
        self.release("moving", version='version = "1.2.0"')
        _git("branch", "release-branch", cwd=work)
        self.bare = root / REPOSITORY
        self.bare.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "-q", "--mirror", str(work), str(self.bare), cwd=root)
        tools = work.parent / "tools"
        tools.mkdir()
        _git("init", "-q", "-b", "main", cwd=tools)
        self._package(tools, "acme-tools", 'version = "0.1.0"')
        _git("add", "-A", cwd=tools)
        _git("commit", "-qm", "tools", cwd=tools)
        _git("tag", "v0.1.0", cwd=tools)
        self.commits["v0.1.0"] = _git("rev-parse", "HEAD", cwd=tools)
        (root / "public").mkdir()
        _git("clone", "-q", "--mirror", str(tools), str(root / "public/tools.git"), cwd=root)

    @staticmethod
    def _package(directory: Path, name: str, version: str, link: bool = False) -> None:
        shutil.rmtree(directory / "src", ignore_errors=True)
        package = directory / "src" / name.replace("-", "_")
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"dags = []\nVERSION = {version!r}\n")
        (directory / "pyproject.toml").write_text(PYPROJECT.format(name=name, version=version))
        script = directory / "bin" / "check"
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        if link:
            (package / "shared").symlink_to("../../..")

    def release(
        self,
        tag: str,
        version: str,
        link: bool = False,
        name: str = NAME,
        directory: str = SUBDIRECTORY,
    ) -> str:
        self._package(self.work / directory, name, version, link)
        _git("add", "-A", cwd=self.work)
        _git("commit", "-qm", tag, cwd=self.work)
        _git("tag", "-a", tag, "-m", tag, cwd=self.work)
        self.commits[tag] = _git("rev-parse", "HEAD", cwd=self.work)
        return self.commits[tag]

    def move(self, tag: str, to: str) -> None:
        _git("tag", "-f", "-a", tag, to, "-m", "moved", cwd=self.bare)
        self.commits[tag] = to

    def hash_of(self, tag: str, tmp: Path) -> str:
        """The source hash of `tag`'s extension directory, checked out with plain git."""
        bare = tmp / f"check-{secrets.token_hex(4)}.git"
        _git("clone", "-q", "--bare", str(self.bare), str(bare), cwd=tmp)
        _git("update-ref", SOURCE_REF, self.commits[tag], cwd=bare)
        return source_sha256(checkout(bare, tmp / f"copy-{bare.stem}", SUBDIRECTORY))


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    base = tmp_path_factory.mktemp("git-extensions")
    root = base / "root"
    root.mkdir()
    commits = Remote(root, base / "work")
    token = _token()
    server = (
        _load_git_http()
        .GitServer(root=root, users={"x-access-token": token}, public={"public/tools.git"})
        .start(base / "tls")
    )
    yield commits, server, token
    server.stop()


UV = """#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
compiled = open(args[3]).read() if args[1:3] == ["pip", "compile"] else None
with open({log!r}, "a") as log:
    log.write(json.dumps({{"args": args, "env": dict(os.environ), "input": compiled}}) + "\\n")
if args[1:3] != ["pip", "compile"]:
    sys.exit(0)
env = os.environ
out = []
for line in open(args[3]).read().splitlines():
    if " @ " in line:
        name, url = line.split(" @ ")
        location, _, fragment = url[len("git+"):].partition("#")
        repository, _, commit = location.rpartition("@")
        # uv's git, with uv's environment, fetches the remote's HEAD and branches, so the
        # commit must be among them wherever git's rewrite of the URL leads.
        listed = subprocess.run(
            ["git", "ls-remote", repository, "HEAD", "refs/heads/*"],
            capture_output=True, text=True, cwd=os.path.dirname({log!r}),
        )
        if commit not in {{row.split()[0] for row in listed.stdout.splitlines()}}:
            sys.exit("uv: no copy of " + repository + " holds " + commit)
        out.append(name.split("[")[0] + " @ " + url)
    elif line.startswith("./"):
        out.append(line.split("[")[0])
    elif "==" in line:
        out.append(line)
    elif line:
        out.append(line.split(">")[0].split("[")[0] + "==9.9")
out.append("helper==1.0")
output = args[args.index("--output-file") + 1]
open(output, "w").write("\\n".join(out) + "\\n")
"""


@pytest.fixture
def uv(tmp_path, monkeypatch):
    """A stand-in uv on PATH; returns the calls it saw."""
    directory = tmp_path / "fake-bin"
    directory.mkdir()
    log = tmp_path / "uv-calls.jsonl"
    script = directory / "uv"
    script.write_text(UV.format(python=sys.executable, log=str(log)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(extensions, "base_constraints", lambda kind: f"base-{kind}==1.0\n")

    def calls():
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


@pytest.fixture
def project(invoke, workdir, monkeypatch):
    assert invoke("init", "p", "--name", "demo", "--users", "2", "--source", "demo").exit_code == 0
    root = workdir / "p"
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def served(remote, project, uv, monkeypatch, tmp_path):
    commits, server, token = remote
    monkeypatch.setenv("GIT_SSL_CAINFO", str(server.ca_path))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GIT_CREDENTIALS", raising=False)
    server.authorizations.clear()
    host = server.url.removeprefix("https://")
    with (project / ".env").open("a") as env:
        env.write(f"GIT_CREDENTIALS=https://x-access-token:{token}@{host}/acme\n")
    return SimpleNamespace(
        commits=commits, server=server, token=token, host=host, root=project, uv=uv, tmp=tmp_path
    )


def _source(served, ref: str, repository: str = REPOSITORY, name: str = NAME) -> str:
    fragment = f"#subdirectory={SUBDIRECTORY}" if repository == REPOSITORY else ""
    return f"{name} @ git+{served.server.url}/{repository}@{ref}{fragment}"


def _line(served, tag: str) -> str:
    commit = served.commits.commits[tag]
    return (
        f"{NAME} @ git+{served.server.url}/{REPOSITORY}@{commit}#subdirectory={SUBDIRECTORY}"
        f"  # tag {tag}"
    )


def _add(invoke, served, ref: str, **kw):
    return invoke("extension", "add", _source(served, ref, **kw), "--json")


def _records(root: Path) -> dict[str, bytes]:
    names = ["vecten.toml", locking.PINS_FILE, locking.RECORD_FILE, *locking.LOCK_FILES.values()]
    return {n: (root / n).read_bytes() for n in names if (root / n).exists()}


def _lock_lines(root: Path, name: str) -> list[str]:
    return [line for line in (root / name).read_text().splitlines() if " @ " in line]


# --- add ------------------------------------------------------------------------------------


def test_add_from_a_prefixed_tag_in_a_monorepo_records_the_commit_everywhere(invoke, served):
    r = _add(invoke, served, "acme-resolution/v1.2.0")
    assert r.exit_code == 0, r.output
    root, commit = served.root, served.commits.commits["acme-resolution/v1.2.0"]
    assert tomllib.loads((root / "vecten.toml").read_text())["extensions"] == [NAME]
    line = _line(served, "acme-resolution/v1.2.0")
    assert (root / "requirements.txt").read_text() == f"{line}\n"
    for name in locking.LOCK_FILES.values():
        assert _lock_lines(root, name) == [line], name
    entry = json.loads((root / locking.RECORD_FILE).read_text())["extensions"][NAME]
    assert entry["source"] == {
        "url": f"{served.server.url}/{REPOSITORY}",
        "commit": commit,
        "subdirectory": SUBDIRECTORY,
        "tag": "acme-resolution/v1.2.0",
        "public": False,
    }
    assert entry["source_sha256"] == served.commits.hash_of("acme-resolution/v1.2.0", served.tmp)
    # The stand-in pins every range at 9.9 and adds `helper` to every resolution.
    assert entry["build_requires"] == ["hatchling==9.9", "helper==1.0"]
    assert (entry["version"], entry["extras"]) == ("1.2.0", ["platform"])
    data = json.loads(r.stdout)
    assert data["source"] == entry["source"]
    assert data["source_sha256"] == entry["source_sha256"]
    assert data["previous"] is None
    assert data["groups"] == ["vecten.dags"]
    assert locking.lock_problems(root, load_config(root)) == []
    assert locking.git_problems(root, load_config(root)) == []


def test_two_extensions_of_one_repository_at_different_commits_lock_together(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    portal = (
        f"{PORTAL} @ git+{served.server.url}/{REPOSITORY}@acme-portal/v0.3.0"
        f"#subdirectory=extensions/{PORTAL}"
    )
    r = invoke("extension", "add", portal, "--json")
    assert r.exit_code == 0, r.output
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    commits = {served.commits.commits[t] for t in ("acme-resolution/v1.2.0", "acme-portal/v0.3.0")}
    assert len(commits) == 2
    for name in locking.LOCK_FILES.values():
        assert {line.split("@")[2].split("#")[0] for line in _lock_lines(served.root, name)} == (
            commits
        )


def test_the_token_reaches_no_argument_record_output_or_build_environment(
    invoke, served, monkeypatch, capfd
):
    for variable in ("GIT_CREDENTIALS", "MCP_GIT_CREDENTIALS"):
        monkeypatch.setenv(variable, f"https://x-access-token:{served.token}@{served.host}")
    r = _add(invoke, served, "acme-resolution/v1.2.0")
    assert r.exit_code == 0, r.output
    captured = capfd.readouterr()
    calls = served.uv()
    assert calls, "uv was never run"
    # What the test runner's own environment holds is not vecten's doing; what vecten passed
    # to uv is: its arguments, its input and every variable it set or changed.
    passed = [
        {
            "args": c["args"],
            "input": c["input"],
            "env": {k: v for k, v in c["env"].items() if os.environ.get(k) != v},
        }
        for c in calls
    ]
    shown = [r.output, captured.out, captured.err, *(json.dumps(c) for c in passed)]
    shown += [text.decode() for text in _records(served.root).values()]
    for text in shown:
        assert _leaks(served.token, text) == []
    for call in calls:
        assert not {"GIT_CREDENTIALS", "MCP_GIT_CREDENTIALS"} & set(call["env"])


def test_uv_resolves_each_git_source_from_the_fetched_copy_with_githubs_fast_path_off(
    invoke, served
):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    compiles = [c for c in served.uv() if c["args"][1:3] == ["pip", "compile"]]
    with_source = [c for c in compiles if " @ " in c["input"]]
    # The stand-in fails any compile whose rewrite does not reach a copy holding the commit.
    assert len(with_source) == len(locking.LOCK_FILES)
    for call in with_source:
        env = call["env"]
        assert env["UV_NO_GITHUB_FAST_PATH"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_the_cli_installs_the_lock_without_the_source_line_then_the_fetched_copy(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    installs = [c["args"] for c in served.uv() if c["args"][1:3] == ["pip", "install"]]
    from_copy = [a for a in installs if "--build-constraint" in a]
    assert len(from_copy) == 1
    args = from_copy[0]
    assert "--no-deps" in args and "--editable" not in args
    assert not args[-1].startswith(str(served.root))
    cli_lock = [a for a in installs if "-r" in a]
    assert len(cli_lock) == 1


def test_a_public_repository_needs_no_credential(invoke, served):
    (served.root / ".env").write_text("")
    r = invoke("extension", "add", _source(served, "v0.1.0", "public/tools.git", "acme-tools"))
    assert r.exit_code == 0, r.output
    entry = json.loads((served.root / locking.RECORD_FILE).read_text())["extensions"]["acme-tools"]
    assert entry["source"]["public"] is True
    assert entry["source"]["subdirectory"] is None
    assert set(served.server.authorizations) == {None}


def test_the_credential_comes_from_the_environment_when_env_holds_none(invoke, served, monkeypatch):
    (served.root / ".env").write_text("")
    monkeypatch.setenv("GIT_CREDENTIALS", f"https://x-access-token:{served.token}@{served.host}")
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0


@pytest.mark.parametrize(
    ("ref", "says"),
    [
        ("acme-resolution/v1.3.0", ["1.3.0", "1.2.9"]),
        ("release-branch", ["does not pin a version", "tag or a full commit"]),
        ("acme-resolution/v-dynamic", ["static version"]),
        ("acme-resolution/v-link", ["src/acme_resolution/shared", "symbolic link"]),
        ("acme-resolution/v-renamed", ["acme-other", NAME]),
        ("acme-resolution/v9.9.9", ["no tag"]),
    ],
    ids=["version-differs", "branch", "dynamic-version", "symlink", "other-name", "missing"],
)
def test_a_refused_source_changes_no_file(invoke, served, ref, says):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    before = _records(served.root)
    r = _add(invoke, served, ref)
    assert r.exit_code != 0
    error = json.loads(r.stdout)["error"]
    for text in says:
        assert text in error
    assert _records(served.root) == before


def test_credentials_in_the_url_are_refused_without_echoing_them(invoke, served):
    url = served.server.url.replace("https://", "https://x:secret@")
    r = invoke("extension", "add", f"{NAME} @ git+{url}/{REPOSITORY}@acme-resolution/v1.2.0")
    assert r.exit_code != 0
    assert "GIT_CREDENTIALS" in r.output
    assert "secret" not in r.output
    assert REPOSITORY not in r.output


def test_a_wrong_token_names_the_host_the_repository_and_the_variable(invoke, served):
    wrong = _token()
    (served.root / ".env").write_text(
        f"GIT_CREDENTIALS=https://x-access-token:{wrong}@{served.host}\n"
    )
    r = _add(invoke, served, "acme-resolution/v1.2.0")
    assert r.exit_code != 0
    for text in (served.host, REPOSITORY, "authentication refused", "GIT_CREDENTIALS"):
        assert text in r.output
    assert _leaks(wrong, r.output) == []


# --- upgrades -------------------------------------------------------------------------------


def test_upgrade_to_a_new_tag_records_the_new_commit_and_names_both(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    toml = (served.root / "vecten.toml").read_bytes()
    r = _add(invoke, served, "acme-resolution/v1.4.0")
    assert r.exit_code == 0, r.output
    old, new = (
        served.commits.commits[t] for t in ("acme-resolution/v1.2.0", "acme-resolution/v1.4.0")
    )
    root = served.root
    assert (root / "requirements.txt").read_text() == _line(served, "acme-resolution/v1.4.0") + "\n"
    for name in locking.LOCK_FILES.values():
        assert _lock_lines(root, name) == [_line(served, "acme-resolution/v1.4.0")]
    entry = json.loads((root / locking.RECORD_FILE).read_text())["extensions"][NAME]
    assert entry["source"]["commit"] == new
    assert entry["source_sha256"] == served.commits.hash_of("acme-resolution/v1.4.0", served.tmp)
    assert (root / "vecten.toml").read_bytes() == toml
    data = json.loads(r.stdout)
    assert data["previous"]["commit"] == old
    assert data["source"]["commit"] == new
    r = invoke("extension", "add", _source(served, "acme-resolution/v1.2.0"))
    assert old in r.output and new in r.output


def test_re_adding_an_unchanged_tag_changes_nothing(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    before = _records(served.root)
    r = _add(invoke, served, "acme-resolution/v1.2.0")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["changed"] is False
    assert _records(served.root) == before


def test_re_adding_a_moved_tag_records_the_new_commit_and_says_it_moved(invoke, served):
    assert _add(invoke, served, "moving").exit_code == 0
    old = served.commits.commits["moving"]
    served.commits.move("moving", served.commits.commits["acme-resolution/v1.4.0"])
    try:
        r = invoke("extension", "add", _source(served, "moving"))
        assert r.exit_code == 0, r.output
        assert "moved" in r.output
        assert old in r.output
        record = json.loads((served.root / locking.RECORD_FILE).read_text())
        assert record["extensions"][NAME]["source"]["commit"] == served.commits.commits["moving"]
    finally:
        served.commits.move("moving", old)


def test_a_repository_that_turned_private_is_not_reported_as_a_moved_tag(invoke, served):
    served.server.public.add(REPOSITORY)
    try:
        assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    finally:
        served.server.public.discard(REPOSITORY)
    r = invoke("extension", "add", _source(served, "acme-resolution/v1.2.0"))
    assert r.exit_code == 0, r.output
    assert "moved" not in r.output
    assert "nothing changed" not in r.output
    assert "credential" in r.output
    entry = json.loads((served.root / locking.RECORD_FILE).read_text())["extensions"][NAME]
    assert entry["source"]["public"] is False


def test_adding_extras_to_a_git_source_is_not_reported_as_unchanged(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    extras = _source(served, "acme-resolution/v1.2.0").replace(NAME, f"{NAME}[cli]", 1)
    r = invoke("extension", "add", extras, "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["changed"] is True
    r = invoke("extension", "add", extras)
    assert "nothing changed" in r.output


def test_switching_between_sources_is_reported(invoke, served, monkeypatch):
    monkeypatch.setattr(
        extensions,
        "inspect_package",
        lambda package, root: extensions.PackageMetadata(
            NAME, "1.5.0", (SimpleNamespace(group="vecten.dags"),), ("platform",)
        ),
    )
    assert invoke("extension", "add", f"{NAME}==1.5.0").exit_code == 0
    r = invoke("extension", "add", _source(served, "acme-resolution/v1.2.0"))
    assert "switched from the index (version 1.5.0) to git" in r.output
    r = invoke("extension", "add", f"{NAME}==1.5.0")
    commit = served.commits.commits["acme-resolution/v1.2.0"]
    assert f"switched from git (commit {commit}) to the index" in r.output


def test_a_moved_tag_does_not_change_a_relock(invoke, served):
    assert _add(invoke, served, "moving").exit_code == 0
    old = served.commits.commits["moving"]
    line = _line(served, "moving")
    served.commits.move("moving", served.commits.commits["acme-resolution/v1.4.0"])
    try:
        doc = (served.root / "vecten.toml").read_text()
        (served.root / "vecten.toml").write_text(
            doc + '\n[dependencies.platform]\npython = ["polars==1.0"]\n'
        )
        r = invoke("lock")
        assert r.exit_code == 0, r.output
        assert (served.root / "requirements.txt").read_text() == f"{line}\n"
        entry = json.loads((served.root / locking.RECORD_FILE).read_text())["extensions"][NAME]
        assert entry["source"]["commit"] == old
    finally:
        served.commits.move("moving", old)


def test_a_bare_name_for_a_git_sourced_extension_is_refused(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    before = _records(served.root)
    r = invoke("extension", "add", NAME, "--json")
    assert r.exit_code != 0
    error = json.loads(r.stdout)["error"]
    assert "tag" in error and "version" in error
    assert _records(served.root) == before


def test_switching_from_git_to_an_index_version_is_reported(invoke, served, monkeypatch):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    monkeypatch.setattr(
        extensions,
        "inspect_package",
        lambda package, root: extensions.PackageMetadata(
            NAME, "1.5.0", (SimpleNamespace(group="vecten.dags"),), ("platform",)
        ),
    )
    r = invoke("extension", "add", f"{NAME}==1.5.0", "--json")
    assert r.exit_code == 0, r.output
    assert (
        json.loads(r.stdout)["previous"]["commit"]
        == served.commits.commits["acme-resolution/v1.2.0"]
    )
    assert (served.root / "requirements.txt").read_text() == f"{NAME}==1.5.0\n"
    entry = json.loads((served.root / locking.RECORD_FILE).read_text())["extensions"][NAME]
    assert "source" not in entry


# --- lock and the record checks -------------------------------------------------------------


def test_lock_refuses_a_source_whose_hash_differs_from_the_record(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    path = served.root / locking.RECORD_FILE
    body = json.loads(path.read_text())
    recorded = body["extensions"][NAME]["source_sha256"]
    body["extensions"][NAME]["source_sha256"] = "0" * 64
    path.write_text(json.dumps(body))
    r = invoke("lock", "--json")
    assert r.exit_code != 0
    error = json.loads(r.stdout)["error"]
    commit = served.commits.commits["acme-resolution/v1.2.0"]
    for text in (NAME, commit, "0" * 64, recorded):
        assert text in error


def test_lock_follows_a_commit_edited_in_requirements(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    new = served.commits.commits["acme-resolution/v1.4.0"]
    line = (
        f"{NAME} @ git+{served.server.url}/{REPOSITORY}@{new}#subdirectory={SUBDIRECTORY}"
        "  # tag acme-resolution/v1.4.0"
    )
    (served.root / "requirements.txt").write_text(line + "\n")
    assert locking.git_problems(served.root, load_config(served.root))
    r = invoke("lock")
    assert r.exit_code == 0, r.output
    entry = json.loads((served.root / locking.RECORD_FILE).read_text())["extensions"][NAME]
    assert entry["source"]["commit"] == new
    for name in locking.LOCK_FILES.values():
        assert _lock_lines(served.root, name) == [line]
    assert locking.git_problems(served.root, load_config(served.root)) == []


@pytest.mark.parametrize(
    ("commit_of", "tag", "says"),
    [
        ("acme-resolution/v1.4.0", "acme-resolution/v1.2.0", "points at"),
        ("acme-resolution/v1.3.0", "acme-resolution/v1.3.0", "1.2.9"),
    ],
    ids=["tag-elsewhere", "version-differs"],
)
def test_lock_refuses_an_edited_commit_its_tag_does_not_vouch_for(
    invoke, served, commit_of, tag, says
):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    commit = served.commits.commits[commit_of]
    line = (
        f"{NAME} @ git+{served.server.url}/{REPOSITORY}@{commit}#subdirectory={SUBDIRECTORY}"
        f"  # tag {tag}"
    )
    (served.root / "requirements.txt").write_text(line + "\n")
    before = _records(served.root)
    r = invoke("lock", "--json")
    assert r.exit_code != 0
    error = json.loads(r.stdout)["error"]
    assert says in error and tag in error
    assert _records(served.root) == before


def test_lock_adopts_an_edited_commit_named_without_a_tag(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    new = served.commits.commits["acme-resolution/v1.4.0"]
    line = f"{NAME} @ git+{served.server.url}/{REPOSITORY}@{new}#subdirectory={SUBDIRECTORY}"
    (served.root / "requirements.txt").write_text(line + "\n")
    assert invoke("lock").exit_code == 0
    entry = json.loads((served.root / locking.RECORD_FILE).read_text())["extensions"][NAME]
    assert (entry["source"]["commit"], entry["source"]["tag"]) == (new, None)


@pytest.mark.parametrize(
    "line",
    [
        "{name} @ git+{url}@acme-resolution/v1.2.0#subdirectory={sub}",
        "{name} @ git+{url}@{short}#subdirectory={sub}",
        "{name} @ git+http://{host}/{repo}@{commit}#subdirectory={sub}",
        "{name} @ git+https://x:tok@{host}/{repo}@{commit}#subdirectory={sub}",
    ],
    ids=["tag", "short-commit", "http", "user-information"],
)
def test_lock_refuses_a_requirements_line_that_is_not_a_pinned_git_source(invoke, served, line):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    commit = served.commits.commits["acme-resolution/v1.2.0"]
    text = line.format(
        name=NAME,
        url=f"{served.server.url}/{REPOSITORY}",
        host=served.host,
        repo=REPOSITORY,
        commit=commit,
        short=commit[:12],
        sub=SUBDIRECTORY,
    )
    (served.root / "requirements.txt").write_text(text + "\n")
    before = _records(served.root)
    r = invoke("lock", "--json")
    assert r.exit_code != 0
    assert "tok" not in r.output
    assert _records(served.root) == before


def _problems(root: Path) -> list[str]:
    return locking.git_problems(root, load_config(root))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda root, line: (root / "requirements-mcp.lock").write_text(
            (root / "requirements-mcp.lock")
            .read_text()
            .replace(line, line.replace("1.2.0", "1.4.0"))
        ),
        lambda root, line: (root / "requirements.lock").write_text(
            (root / "requirements.lock").read_text().replace(line + "\n", "")
        ),
        lambda root, line: (root / "requirements-airflow.lock").write_text(
            (root / "requirements-airflow.lock").read_text()
            + "stranger @ git+https://example.com/x/y@"
            + "a" * 40
            + "\n"
        ),
        lambda root, line: (root / "requirements.txt").write_text(f"{line}\n{line}\n"),
        lambda root, line: (root / locking.RECORD_FILE).write_text(
            (root / locking.RECORD_FILE)
            .read_text()
            .replace('"tag": "acme-resolution/v1.2.0"', '"tag": "acme-resolution/v1.4.0"')
        ),
    ],
    ids=["lock-differs", "lock-lacks-line", "unregistered-url", "twice", "record-differs"],
)
def test_records_that_disagree_are_refused_naming_extension_add(invoke, served, tamper):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    assert _problems(served.root) == []
    tamper(served.root, _line(served, "acme-resolution/v1.2.0"))
    problems = _problems(served.root)
    assert problems
    from vecten.stack.images import write_project_layer

    stack = served.root / ".vecten" / "stack"
    stack.mkdir(parents=True, exist_ok=True)
    with pytest.raises(extensions.Failure) as caught:
        write_project_layer(stack, load_config(served.root), None)
    assert "vecten extension add" in caught.value.message


def test_a_locked_project_without_git_sources_has_no_git_problems_until_a_url_appears(
    invoke, project, uv, monkeypatch
):
    monkeypatch.setattr(
        extensions,
        "inspect_package",
        lambda package, root: extensions.PackageMetadata(
            "acme-demo", "1.0.0", (SimpleNamespace(group="vecten.dags"),), ()
        ),
    )
    assert invoke("extension", "add", "acme-demo==1.0.0").exit_code == 0
    assert locking.git_problems(project, load_config(project)) == []
    lock = project / "requirements-mcp.lock"
    lock.write_text(lock.read_text() + "acme-demo @ git+https://example.com/a/b@" + "a" * 40 + "\n")
    assert locking.git_problems(project, load_config(project)) == [
        "requirements-mcp.lock: acme-demo comes from git, but extensions.lock.json says not"
    ]


def test_the_record_check_reads_no_network(invoke, served, monkeypatch):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    served.server.authorizations.clear()
    assert _problems(served.root) == []
    assert served.server.authorizations == []


def test_inputs_lines_change_with_the_commit(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    root = served.root
    stamps = {n: (root / n).read_text().split("\n", 1)[0] for n in locking.LOCK_FILES.values()}
    assert _add(invoke, served, "acme-resolution/v1.4.0").exit_code == 0
    for name, stamp in stamps.items():
        assert (root / name).read_text().split("\n", 1)[0] != stamp


def test_uv_gets_the_environment_unchanged_in_a_project_without_git_sources(project, monkeypatch):
    monkeypatch.setenv("GIT_CREDENTIALS", "https://u:t@example.com")
    monkeypatch.setenv("NETRC", "/somewhere/.netrc")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
    assert extensions.uv_environment(project) == extensions.package_environment(project)


def test_every_uv_call_in_a_project_with_git_sources_runs_without_the_credential(
    invoke, served, monkeypatch
):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    for variable in ("GIT_CREDENTIALS", "MCP_GIT_CREDENTIALS"):
        monkeypatch.setenv(variable, f"https://x-access-token:{served.token}@{served.host}")
    monkeypatch.setattr(
        extensions,
        "inspect_package",
        lambda package, root: extensions.PackageMetadata(
            "acme-demo", "1.0.0", (SimpleNamespace(group="vecten.dags"),), ()
        ),
    )
    first = len(served.uv())
    assert invoke("extension", "add", "acme-demo==1.0.0").exit_code == 0
    assert invoke("lock").exit_code == 0
    calls = served.uv()[first:]
    assert calls
    for call in calls:
        assert not {"GIT_CREDENTIALS", "MCP_GIT_CREDENTIALS"} & set(call["env"]), call["args"]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("acme @ git+https://github.com/acme/x@" + "a" * 40 + "  # tag v1", True),
        ("acme[cli] @ git+https://github.com/acme/x@v1#subdirectory=acme", True),
        ("acme @ git+http://github.com/acme/x@v1", True),
        ("acme @ https://example.com/acme-1.0.whl", True),
        ("acme@git+https://github.com/acme/x@v1", True),
        ("httpx==0.27.0  # mirrored from https://example.com/httpx", False),
        ("# see https://example.com", False),
        ("./extensions/acme  # https://example.com", False),
        ("httpx==0.27.0", False),
    ],
)
def test_a_source_line_is_told_apart_by_its_requirement_not_its_comment(line, expected):
    from vecten import git_extensions

    assert git_extensions.is_source(line) is expected


def test_an_index_pin_with_a_url_in_its_comment_is_not_read_as_a_source(project):
    (project / "requirements.txt").write_text("acme-demo==1.2.0  # from https://example.com\n")
    # As before git sources, a pin with a comment is not one `vecten lock` writes or reads.
    assert locking.read_pins(project) == {}
    assert locking.git_problems(project, load_config(project)) == []
