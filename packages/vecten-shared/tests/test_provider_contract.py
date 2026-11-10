"""The contract every provider passes.

The cases take a `made` fixture; a module that imports them and defines its own `made`
runs the same cases on another provider class.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from moto import mock_aws

from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import LogTarget, Provider, scope_for, secret_id
from vecten_shared.provider.aws import AwsRuntimeProvider
from vecten_shared.provider.local import LocalProvider
from vecten_shared.provider.scaleway import ScalewayRuntimeProvider, scaleway_resource_name


def _load_fixtures():
    # Every package's test directory is a package named `tests`, so a relative import
    # resolves against whichever one pytest imported first; load the helper by path.
    name = "vecten_shared_tests_provider_fixtures"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("provider_fixtures.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


fixtures = _load_fixtures()
aws_credentials = fixtures.aws_credentials
write_aws_project = fixtures.write_aws_project
write_config = fixtures.write_config
write_scaleway_project = fixtures.write_scaleway_project
scaleway_credentials = fixtures.scaleway_credentials
FakeSecretManager = fixtures.FakeSecretManager

REPO = Path(__file__).resolve().parents[3]
PACKAGES = REPO / "packages"

OPERATIONS = (
    "bootstrap",
    "provision",
    "destroy",
    "put_secret",
    "get_secret",
    "connection",
    "log_target",
    "ship",
    "list_secrets",
)


@dataclass(frozen=True)
class Made:
    provider: Provider
    env: str
    # A name this provider accepts in put_secret from the calling code.
    writable: str


def make_local(root: Path, monkeypatch: pytest.MonkeyPatch) -> Made:
    write_config(root)
    return Made(LocalProvider(root, inside_compose=False), "local", "HUBSPOT_API_KEY")


def make_aws(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    cls: type[AwsRuntimeProvider] = AwsRuntimeProvider,
) -> Made:
    write_aws_project(root)
    return Made(cls(root, "prod"), "prod", "MCP_IMPERSONATOR_PASSWORD")


def make_scaleway(root: Path, monkeypatch: pytest.MonkeyPatch) -> Made:
    scaleway_credentials(monkeypatch)
    write_scaleway_project(root)
    store = FakeSecretManager().install(monkeypatch)
    # Terraform creates the shell of every secret a VM writes; the VM only adds versions.
    store.add("/vecten/acme/prod/mcp", "MCP_IMPERSONATOR_PASSWORD")
    return Made(ScalewayRuntimeProvider(root, "prod"), "prod", "MCP_IMPERSONATOR_PASSWORD")


MAKERS = {"local": make_local, "aws": make_aws, "scaleway": make_scaleway}


@pytest.fixture(params=sorted(MAKERS))
def made(request, tmp_path, monkeypatch) -> Iterator[Made]:
    aws_credentials(monkeypatch, tmp_path)
    with mock_aws():
        yield MAKERS[request.param](tmp_path, monkeypatch)


def test_every_provider_offers_the_eight_operations_and_list_secrets(made):
    for operation in OPERATIONS:
        assert callable(getattr(made.provider, operation, None)), operation


def test_unknown_database_names_the_three_values(made):
    with pytest.raises(Failure) as exc:
        made.provider.connection(made.env, "nosuch")
    assert exc.value.code == ExitCode.USAGE
    assert "warehouse, airflow, mcp_state" in exc.value.message


def test_state_databases_share_a_host_each_with_its_own_database_and_role(made):
    airflow = made.provider.connection(made.env, "airflow")
    mcp_state = made.provider.connection(made.env, "mcp_state")
    assert airflow.database != mcp_state.database
    assert airflow.role != mcp_state.role
    assert airflow.host == mcp_state.host
    assert airflow.auth == mcp_state.auth == "password"


def test_log_target_is_a_log_target(made):
    assert isinstance(made.provider.log_target(made.env), LogTarget)


def test_secret_round_trips(made):
    made.provider.put_secret(made.writable, "first-value")
    assert made.provider.get_secret(made.writable) == "first-value"
    made.provider.put_secret(made.writable, "second-value")
    assert made.provider.get_secret(made.writable) == "second-value"
    assert made.writable in made.provider.list_secrets()


@pytest.mark.parametrize(
    ("name", "scope"),
    [
        ("MCP_PROVISIONER_PASSWORD", "mcp"),
        ("MCP_IMPERSONATOR_PASSWORD", "mcp"),
        ("DEPLOY_SNOWFLAKE_PRIVATE_KEY", "deploy"),
        ("ADMIN_JANE_PASSWORD", "admin"),
        ("HUBSPOT_API_KEY", "platform"),
        ("LOADER_PASSWORD", "platform"),
        ("MCPX_TOKEN", "platform"),
        ("SETUP_STATE_ADMIN_PASSWORD", "setup"),
        ("SETUP_WAREHOUSE_ADMIN_PASSWORD", "setup"),
    ],
)
def test_scope_follows_the_name_prefix(name, scope):
    assert scope_for(name) == scope


def test_secret_id_places_the_name_under_its_scope():
    assert secret_id("acme", "prod", "MCP_PROVISIONER_PASSWORD") == (
        "vecten/acme/prod/mcp/MCP_PROVISIONER_PASSWORD"
    )
    assert secret_id("acme", "prod", "HUBSPOT_API_KEY") == (
        "vecten/acme/prod/platform/HUBSPOT_API_KEY"
    )


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("HUBSPOT_API_KEY", "/vecten/acme/prod/platform"),
        ("MCP_PROVISIONER_PASSWORD", "/vecten/acme/prod/mcp"),
        ("SETUP_STATE_ADMIN_PASSWORD", "/vecten/acme/prod/setup"),
    ],
)
def test_scaleway_places_the_name_under_its_scope_path(name, path):
    assert scaleway_resource_name("acme", "prod", name) == f"{path}/{name}"
    assert secret_id("acme", "prod", name) == f"{path[1:]}/{name}"


def test_secret_id_refuses_a_name_outside_the_rule():
    with pytest.raises(Failure) as exc:
        secret_id("acme", "prod", "../MCP_X")
    assert exc.value.code == ExitCode.USAGE


CONTROL_PLANE = frozenset({"bootstrap", "provision", "destroy", "ship"})
# The one write runtime code may make: the MCP server rotating the impersonator's password.
IMPERSONATOR = "MCP_IMPERSONATOR_PASSWORD"
RUNTIME_ROOTS = (
    PACKAGES / "vecten-platform" / "src" / "vecten_platform",
    PACKAGES / "vecten-mcp" / "src" / "vecten_mcp",
    PACKAGES / "vecten-shared" / "src" / "vecten_shared",
    PACKAGES / "vecten" / "src" / "vecten" / "scaffold" / "template",
    PACKAGES / "vecten" / "src" / "vecten" / "connectors" / "templates",
)
_CALL = re.compile(
    r"\b(bootstrap|provision|destroy|ship|put_secret)\s*\(\s*(?:([\"'])([A-Za-z0-9_]*)\2)?"
)
_DEF = re.compile(r"\bdef\s+$")


def calls(path: Path) -> list[tuple[str, str | None]]:
    """Each call's function name, with its first positional argument when that is a string."""
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # A Jinja template that is not valid Python is scanned as text, skipping definitions.
        return [
            (m.group(1), m.group(3))
            for m in _CALL.finditer(text)
            if not _DEF.search(text[: m.start()])
        ]
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        first = node.args[0] if node.args else None
        literal = first.value if isinstance(first, ast.Constant) else None
        found.append((name, literal if isinstance(literal, str) else None))
    return found


def runtime_files(roots: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        for pattern in ("*.py", "*.py.j2", "*.py.jinja"):
            files.extend(root.rglob(pattern))
    return sorted(files)


def control_plane_calls(roots: tuple[Path, ...]) -> list[str]:
    found = []
    for path in runtime_files(roots):
        shown = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        for name, first in calls(path):
            if name in CONTROL_PLANE or (
                name == "put_secret" and first not in {IMPERSONATOR, "MCP_SIGNING_KEY"}
            ):
                found.append(f"{shown}: {name}")
    return found


def test_runtime_code_calls_no_control_plane_operation():
    for root in RUNTIME_ROOTS:
        assert root.is_dir(), root
    assert any(p.suffix == ".j2" for p in runtime_files(RUNTIME_ROOTS))
    assert control_plane_calls(RUNTIME_ROOTS) == []


def test_scan_catches_planted_calls(tmp_path):
    (tmp_path / "dag.py").write_text("def f(p):\n    p.ship('prod', 'platform', '.')\n")
    (tmp_path / "rotate.py").write_text("def g(p):\n    p.put_secret('LOADER_PASSWORD', 'x')\n")
    (tmp_path / "sync.py.j2").write_text("{% if x %}\nprovider.destroy('prod')\n{% endif %}\n")
    (tmp_path / "ok.py.j2").write_text("{% if x %}\ndef provision(self, env):\n{% endif %}\n")
    found = control_plane_calls((tmp_path,))
    assert sorted(f.rsplit(": ", 1)[1] for f in found) == ["destroy", "put_secret", "ship"]


def test_scan_allows_only_literal_rotation_writes(tmp_path):
    (tmp_path / "rotate.py").write_text(
        "def g(p, v):\n    p.put_secret('MCP_IMPERSONATOR_PASSWORD', v)\n"
    )
    (tmp_path / "rotate.py.j2").write_text(
        '{% if x %}\nprovider.put_secret("MCP_IMPERSONATOR_PASSWORD", v)\n{% endif %}\n'
    )
    assert control_plane_calls((tmp_path,)) == []
    (tmp_path / "indirect.py").write_text(
        "NAME = 'MCP_IMPERSONATOR_PASSWORD'\ndef h(p, v):\n    p.put_secret(NAME, v)\n"
    )
    (tmp_path / "keyword.py").write_text(
        "def k(p, v):\n    p.put_secret(name='MCP_IMPERSONATOR_PASSWORD', value=v)\n"
    )
    (tmp_path / "other.py.j2").write_text(
        "{% if x %}\nprovider.put_secret('MCP_PROVISIONER_PASSWORD', v)\n{% endif %}\n"
    )
    found = sorted(Path(f.rsplit(": ", 1)[0]).name for f in control_plane_calls((tmp_path,)))
    assert found == ["indirect.py", "keyword.py", "other.py.j2"]
