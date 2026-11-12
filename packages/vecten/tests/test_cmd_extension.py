import json
import tomllib
from types import SimpleNamespace

import pytest

from vecten_shared.extensions import GROUPS
from vecten_shared.output import ExitCode, Failure


@pytest.fixture
def project(invoke, workdir, monkeypatch):
    assert (
        invoke(
            "init", "p", "--name", "demo", "--users", "2", "--source", "demo", "--json"
        ).exit_code
        == 0
    )
    root = workdir / "p"
    monkeypatch.chdir(root)
    return root


def installer(monkeypatch, groups=GROUPS, fail=False):
    from vecten import extensions

    calls = []

    def install(req, root):
        calls.append(req)
        if fail:
            raise Failure(ExitCode.TOOL_FAILED, "package installation failed")
        return extensions.PackageMetadata(
            name="acme-demo",
            version="1.2.0",
            entry_points=tuple(SimpleNamespace(group=g) for g in groups),
        )

    monkeypatch.setattr(extensions, "inspect_package", install)
    monkeypatch.setattr(extensions, "install_lock", lambda *a: None)
    monkeypatch.setattr(extensions, "base_constraints", lambda kind: "")
    monkeypatch.setattr(extensions, "resolve_lock", lambda reqs, root, **kw: "\n".join(reqs) + "\n")
    return calls


def lock_body(path):
    """A lock's pins without its inputs line."""
    return "".join(
        line + "\n" for line in path.read_text().splitlines() if not line.startswith("#")
    )


def test_add_records_all_groups_and_repeat(invoke, project, monkeypatch):
    calls = installer(monkeypatch)
    for _ in range(2):
        r = invoke("extension", "add", "acme-demo>=1", "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert set(data["groups"]) == set(GROUPS)
    doc = tomllib.loads((project / "vecten.toml").read_text())
    assert doc["extensions"] == ["acme-demo"]
    assert (project / "requirements.txt").read_text() == "acme-demo==1.2.0\n"
    assert "acme-demo==1.2.0\n" in lock_body(project / "requirements.lock")
    assert len(calls) == 2


@pytest.mark.parametrize("groups,fail", [((), False), (GROUPS, True)])
def test_refusal_changes_no_project_files(invoke, project, monkeypatch, groups, fail):
    installer(monkeypatch, groups, fail)
    before = (project / "vecten.toml").read_bytes()
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code != 0
    assert (project / "vecten.toml").read_bytes() == before
    assert not (project / "requirements.txt").exists()


def test_unknown_group_reported(invoke, project, monkeypatch):
    installer(monkeypatch, ["vecten.skills", "vecten.dashboards"])
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["unknown_groups"] == ["vecten.dashboards"]


def test_invalid_input_never_calls_installer(invoke, project, monkeypatch):
    calls = installer(monkeypatch)
    r = invoke("extension", "add", "x @ https://token@example.test/a.whl", "--json")
    assert r.exit_code == 2
    assert "token" not in r.output
    assert not calls


def test_write_failure_restores_all_project_records(tmp_path, monkeypatch):
    from vecten.extensions import write_project_files
    from vecten_shared.records import write_text_atomic

    one, two = tmp_path / "one", tmp_path / "two"
    one.write_text("original")

    def write(path, text):
        if path == two:
            raise OSError("cannot write")
        write_text_atomic(path, text)

    monkeypatch.setattr("vecten.extensions.write_text_atomic", write)
    with pytest.raises(Failure):
        write_project_files({one: "changed", two: "new"})
    assert one.read_text() == "original"
    assert not two.exists()


def test_upgrade_changes_only_dependency_records(invoke, project, monkeypatch):
    from vecten import extensions

    installer(monkeypatch)
    assert invoke("extension", "add", "acme-demo==1.2.0").exit_code == 0
    before = (project / "vecten.toml").read_bytes()
    monkeypatch.setattr(
        extensions,
        "inspect_package",
        lambda *args: extensions.PackageMetadata(
            name="acme-demo", version="2.0.0", entry_points=(SimpleNamespace(group="vecten.dags"),)
        ),
    )
    assert invoke("extension", "add", "acme-demo==2.0.0").exit_code == 0
    assert (project / "vecten.toml").read_bytes() == before
    assert (project / "requirements.txt").read_text() == "acme-demo==2.0.0\n"
    assert "acme-demo==2.0.0\n" in lock_body(project / "requirements.lock")


def test_resolution_and_installation_keep_release_constraints(tmp_path, monkeypatch):
    from pathlib import Path

    from vecten.extensions import resolve_lock
    from vecten_shared.versions import VERSIONS

    calls = []

    def uv(args, root, cwd=None):
        calls.append(args)
        assert "--constraint" in args
        constraints = Path(args[args.index("--constraint") + 1]).read_text()
        for name in ("vecten", "vecten-shared", "vecten-platform", "vecten-mcp"):
            assert f"{name}=={VERSIONS.vecten}\n" in constraints
        if "compile" in args:
            Path(args[args.index("--output-file") + 1]).write_text("acme-demo==1.0\n")

    monkeypatch.setattr("vecten.extensions._uv", uv)
    assert resolve_lock(["acme-demo==1.0"], tmp_path) == "acme-demo==1.0\n"
    assert len(calls) == 1


def test_unknown_package_is_inspected_without_installing_into_cli(tmp_path, monkeypatch):
    from pathlib import Path

    from vecten.extensions import inspect_package

    def uv(args, root, cwd=None):
        assert "--target" in args and "--no-deps" in args
        assert (
            "--python" not in args
            or args[args.index("--python") + 1] != __import__("sys").executable
        )
        site = Path(args[args.index("--target") + 1])
        info = site / "acme_demo-1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Name: acme-demo\nVersion: 1.0\n")

    monkeypatch.setattr("vecten.extensions._uv", uv)
    assert inspect_package("acme-demo", tmp_path).name == "acme-demo"


def test_lock_failure_never_installs_into_cli(invoke, project, monkeypatch):
    from vecten import extensions

    installer(monkeypatch)
    installed = []
    monkeypatch.setattr(extensions, "install_lock", lambda *a: installed.append(a), raising=False)
    monkeypatch.setattr(
        extensions,
        "resolve_lock",
        lambda *a, **kw: (_ for _ in ()).throw(Failure(ExitCode.REFUSED, "incompatible lock")),
    )
    assert invoke("extension", "add", "acme-demo").exit_code != 0
    assert installed == []


def test_compile_targets_runtime_python_and_does_not_install(tmp_path, monkeypatch):
    from pathlib import Path

    from vecten.extensions import resolve_lock
    from vecten_shared.versions import VERSIONS

    calls = []

    def uv(args, root, cwd=None):
        calls.append(args)
        assert args[:2] == ["pip", "compile"]
        assert args[args.index("--python-version") + 1] == VERSIONS.python
        Path(args[args.index("--output-file") + 1]).write_text("acme-demo==1.0\n")

    monkeypatch.setattr("vecten.extensions._uv", uv)
    resolve_lock(["acme-demo==1.0"], tmp_path)
    assert len(calls) == 1


def test_each_runtime_lock_uses_its_own_base_constraints(invoke, project, monkeypatch):
    from vecten import extensions

    installer(monkeypatch)
    seen = {}
    monkeypatch.setattr(extensions, "base_constraints", lambda kind: f"base-{kind}==1.0\n")

    def resolve(reqs, root, *, constraints="", runtime=False, previous=None, upgrade=(), **kw):
        seen[constraints] = runtime
        return "acme-demo==1.2.0\n"

    monkeypatch.setattr(extensions, "resolve_lock", resolve)
    assert invoke("extension", "add", "acme-demo").exit_code == 0
    assert seen == {"": False, "base-airflow==1.0\n": True, "base-mcp==1.0\n": True}


@pytest.mark.parametrize(
    ("lines", "extras"),
    [
        (
            "Provides-Extra: platform\nProvides-Extra: mcp\nProvides-Extra: CLI\n",
            ("cli", "mcp", "platform"),
        ),
        ("", ()),
    ],
)
def test_inspection_reads_the_extras_a_wheel_provides(tmp_path, monkeypatch, lines, extras):
    from pathlib import Path

    from vecten.extensions import inspect_package

    def uv(args, root, cwd=None):
        info = Path(args[args.index("--target") + 1]) / "acme_demo-1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.4\nName: acme-demo\nVersion: 1.0\n{lines}"
        )

    monkeypatch.setattr("vecten.extensions._uv", uv)
    assert inspect_package("acme-demo", tmp_path).extras == extras
