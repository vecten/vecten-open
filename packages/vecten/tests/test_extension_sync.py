"""`vecten extension sync`: the committed CLI lock installed into the environment running
Vecten, git sources from fetched copies checked against their recorded hash, and no record
changed."""

from __future__ import annotations

import json
import os

from vecten import locking

from . import test_git_extensions as git
from .test_git_extensions import NAME, _add, _leaks, _records

# The local git host, its token, a project and the stand-in uv, as `extension add` tests use them.
remote, uv, project, served = git.remote, git.uv, git.project, git.served


def _new_calls(served, before: int) -> list[dict]:
    return served.uv()[before:]


def _installs(calls: list[dict]) -> list[list[str]]:
    return [c["args"] for c in calls if c["args"][1:3] == ["pip", "install"]]


def test_sync_installs_the_lock_then_each_git_source_from_its_fetched_copy(invoke, served, capfd):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    records = _records(served.root)
    before = len(served.uv())
    served.server.authorizations.clear()
    r = invoke("extension", "sync", "--json")
    assert r.exit_code == 0, r.output
    calls = _new_calls(served, before)
    installs = _installs(calls)
    [lock] = [a for a in installs if "-r" in a]
    assert "--no-deps" in lock
    [source] = [a for a in installs if "--build-constraint" in a]
    assert "--no-deps" in source and "--editable" not in source
    assert not any(a[1:3] == ["pip", "compile"] for a in (c["args"] for c in calls))
    # The recorded commit was fetched again, with the credential.
    assert any(served.server.authorizations)
    assert _records(served.root) == records
    data = json.loads(r.stdout)
    commit = served.commits.commits["acme-resolution/v1.2.0"]
    assert data["sources"] == {NAME: commit}
    captured = capfd.readouterr()
    passed = [
        {"args": c["args"], "env": {k: v for k, v in c["env"].items() if os.environ.get(k) != v}}
        for c in calls
    ]
    for text in [r.output, captured.out, captured.err, *(json.dumps(c) for c in passed)]:
        assert _leaks(served.token, text) == []
    for call in calls:
        assert "GIT_CREDENTIALS" not in call["env"]


def test_sync_takes_the_credential_from_the_environment_when_env_holds_none(
    invoke, served, monkeypatch
):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    (served.root / ".env").write_text("")
    monkeypatch.setenv("GIT_CREDENTIALS", f"https://x-access-token:{served.token}@{served.host}")
    r = invoke("extension", "sync")
    assert r.exit_code == 0, r.output


def test_sync_refuses_stale_records_naming_vecten_lock_and_installs_nothing(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    toml = served.root / "vecten.toml"
    toml.write_text(
        toml.read_text().replace(
            f'extensions = ["{NAME}"]', f'extensions = ["{NAME}", "acme-demo"]'
        )
    )
    records = _records(served.root)
    before = len(served.uv())
    r = invoke("extension", "sync", "--json")
    assert r.exit_code != 0
    assert "vecten lock" in json.loads(r.stdout)["error"]
    assert _installs(_new_calls(served, before)) == []
    assert _records(served.root) == records


def test_sync_refuses_a_source_whose_hash_differs_and_installs_nothing(invoke, served):
    assert _add(invoke, served, "acme-resolution/v1.2.0").exit_code == 0
    path = served.root / locking.RECORD_FILE
    record = json.loads(path.read_text())
    recorded = record["extensions"][NAME]["source_sha256"]
    record["extensions"][NAME]["source_sha256"] = "e" * 64
    path.write_text(json.dumps(record))
    before = len(served.uv())
    r = invoke("extension", "sync", "--json")
    assert r.exit_code != 0
    error = json.loads(r.stdout)["error"]
    assert "e" * 64 in error and recorded in error and NAME in error
    assert _installs(_new_calls(served, before)) == []


def test_sync_installs_an_in_repo_extension_from_its_folder(invoke, project, uv):
    from .test_in_repo import make

    make(project)
    assert invoke("extension", "add", "./extensions/acme-portal").exit_code == 0
    before = len(uv())
    r = invoke("extension", "sync")
    assert r.exit_code == 0, r.output
    installs = _installs(uv()[before:])
    assert [a for a in installs if "--editable" in a] == [
        [
            "--no-config",
            "pip",
            "install",
            "--python",
            installs[0][installs[0].index("--python") + 1],
            "--no-deps",
            "--editable",
            str(project / "extensions" / "acme-portal"),
        ]
    ]


def test_sync_refuses_a_url_line_the_record_does_not_hold(invoke, project, uv, monkeypatch):
    from types import SimpleNamespace

    from vecten import extensions

    monkeypatch.setattr(
        extensions,
        "inspect_package",
        lambda package, root: extensions.PackageMetadata(
            "acme-demo", "1.0.0", (SimpleNamespace(group="vecten.dags"),), ()
        ),
    )
    assert invoke("extension", "add", "acme-demo==1.0.0").exit_code == 0
    lock = project / locking.LOCK_FILES["cli"]
    lock.write_text(lock.read_text() + "acme-demo @ git+https://example.com/x/y@" + "a" * 40 + "\n")
    before = len(uv())
    r = invoke("extension", "sync", "--json")
    assert r.exit_code != 0
    assert "vecten extension add" in json.loads(r.stdout)["error"]
    assert _installs(uv()[before:]) == []
