import pytest

from vecten.stack.images import project_requirements, write_project_layer
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS


def cfg(**kw):
    return VectenConfig.model_validate(
        {
            "users": 5,
            "components": ["platform", "mcp"],
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "warehouse": {"kind": "postgres"},
            **kw,
        }
    )


def test_requirements_from_extensions_and_connector_dependencies(tmp_path):
    c = cfg(
        extensions=["vecten-ext-entity-resolution==1.2.0"],
        connectors=[{"name": "hubspot", "source": "dlt", "dependencies": ["dlt[hubspot]"]}],
    )
    write_project_layer(tmp_path, c, registry=None)
    assert (tmp_path / "build" / "requirements.txt").read_text() == (
        "dlt[hubspot]\nvecten-ext-entity-resolution==1.2.0\n"
    )
    for kind in ("airflow", "mcp"):
        df = (tmp_path / "build" / f"{kind}.Dockerfile").read_text()
        assert df.startswith(f"FROM vecten-{kind}:{VERSIONS.vecten}\n")
        assert "COPY requirements.txt /tmp/requirements.txt" in df
        assert "--constraint /tmp/vecten-base-constraints.txt" in df
        assert "cat /tmp/vecten-pip.log" in df
        if kind == "airflow":
            assert f'"apache-airflow=={VERSIONS.airflow_version}"' in df


def test_empty_requirements_skip_install(tmp_path):
    write_project_layer(tmp_path, cfg(), registry="ghcr.io/vecten")
    df = (tmp_path / "build" / "mcp.Dockerfile").read_text()
    assert df == f"FROM ghcr.io/vecten/vecten-mcp:{VERSIONS.vecten}\n"
    assert (tmp_path / "build" / "requirements.txt").read_text() == ""


@pytest.mark.parametrize(
    "kw",
    [
        {"extensions": ["--index-url=https://evil.example/simple"]},
        {"connectors": [{"name": "x", "source": "dlt", "dependencies": ["-e", "./local"]}]},
    ],
)
def test_requirement_that_looks_like_a_pip_option_is_refused(tmp_path, kw):
    with pytest.raises(Failure) as exc:
        project_requirements(cfg(**kw))
    assert exc.value.code == ExitCode.USAGE and "looks like a pip option" in exc.value.message
