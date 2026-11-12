"""`python -m vecten_shared.launch streamlit <entry>`: a file inside an installed package."""

import ast
import sys
from pathlib import Path

import pytest

from vecten_shared import launch


@pytest.fixture
def package(tmp_path, monkeypatch):
    site = tmp_path / "site"
    root = site / "acme_sales"
    (root / "pages").mkdir(parents=True)
    (root / "__init__.py").write_text("raise RuntimeError('the launcher must not import me')\n")
    (root / "app.py").write_text("")
    (root / "pages" / "detail.py").write_text("")
    (site / "loose.py").write_text("")
    (tmp_path / "outside.py").write_text("")
    monkeypatch.syspath_prepend(str(site))
    yield root
    sys.modules.pop("acme_sales", None)


def test_an_entry_resolves_to_its_file_inside_the_package(package):
    assert launch.resolve("acme_sales/app.py") == (package / "app.py").resolve()
    assert launch.resolve("acme_sales/pages/detail.py") == (package / "pages/detail.py").resolve()
    assert "acme_sales" not in sys.modules


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ("acme_sales/../outside.py", "inside"),
        ("/etc/passwd", "inside"),
        ("acme_sales/missing.py", "no file"),
        ("acme_sales/pages", "no file"),
        ("unknown_pkg/app.py", "no installed package"),
        ("loose/app.py", "no installed package"),
        ("acme-sales/app.py", "inside"),
        ("acme_sales", "inside"),
    ],
)
def test_an_entry_outside_the_package_or_missing_is_refused(package, entry, problem):
    with pytest.raises(launch.LaunchError, match=problem):
        launch.resolve(entry)


def test_a_symlink_out_of_the_package_is_refused(package, tmp_path):
    (package / "escape.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(launch.LaunchError, match="inside"):
        launch.resolve("acme_sales/escape.py")


def test_streamlit_runs_headless_on_port_with_usage_statistics_off(package):
    path = launch.resolve("acme_sales/app.py")
    assert launch.streamlit_argv(path, "8501") == [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(path),
        "--server.port",
        "8501",
        "--server.address",
        "0.0.0.0",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--server.enableXsrfProtection",
        "true",
        "--server.baseUrlPath",
        "",
    ]


def test_main_replaces_itself_with_streamlit(package, monkeypatch):
    seen = []
    monkeypatch.setenv("PORT", "8501")
    monkeypatch.setattr(launch.os, "execv", lambda path, argv: seen.append((path, argv)))
    launch.main(["streamlit", "acme_sales/app.py"])
    [(path, argv)] = seen
    assert path == sys.executable
    assert argv == launch.streamlit_argv(package / "app.py", "8501")


@pytest.mark.parametrize(
    ("args", "port", "message"),
    [
        (["dash", "acme_sales/app.py"], "8501", "runtime"),
        (["streamlit"], "8501", "usage"),
        (["streamlit", "acme_sales/app.py", "extra"], "8501", "usage"),
        (["streamlit", "acme_sales/app.py"], None, "PORT"),
        (["streamlit", "acme_sales/app.py"], "80; rm", "PORT"),
        (["streamlit", "acme_sales/../outside.py"], "8501", "inside"),
    ],
)
def test_main_refuses_with_a_message_and_execs_nothing(
    package, monkeypatch, capsys, args, port, message
):
    if port is None:
        monkeypatch.delenv("PORT", raising=False)
    else:
        monkeypatch.setenv("PORT", port)
    monkeypatch.setattr(launch.os, "execv", lambda *a: pytest.fail("exec'd"))
    assert launch.main(args) == 2
    assert message in capsys.readouterr().err


def test_the_launcher_imports_the_standard_library_only():
    tree = ast.parse(Path(launch.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
