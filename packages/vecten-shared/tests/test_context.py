"""`vecten_shared.context`: settings and declared secrets, the same way in every runtime."""

import pytest
import tomli_w

from vecten_shared import context
from vecten_shared import provider as provider_module
from vecten_shared.extension_record import Extension, record_text
from vecten_shared.output import Failure

SCHEMA = {
    "properties": {
        "daily_cap": {"type": "integer", "default": 1000},
        "dry_run": {"type": "boolean", "default": False},
    }
}
RECORD = {
    "acme-resolution": Extension(
        "1.0", (), secrets=("ACME_API_KEY", "MCP_ACME_TOKEN"), settings=SCHEMA
    )
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    doc = {
        "users": 1,
        "project": {"name": "a", "scaffold_version": "0.1.0"},
        "extensions": ["acme-resolution"],
        "secrets": ["SLACK_TOKEN"],
        "settings": {
            "acme-resolution": {"env": {"staging": {"dry_run": True}}},
            "project": {"channel": "#data"},
        },
    }
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps(doc))
    (tmp_path / "extensions.lock.json").write_text(record_text(RECORD))
    (tmp_path / ".env").write_text("ACME_API_KEY=from-dotenv\nSLACK_TOKEN=slack\n")
    for name in ("VECTEN_COMPONENT", "VECTEN_PROVIDER", "VECTEN_INSIDE_COMPOSE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTEN_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VECTEN_ENV", "local")
    return tmp_path


def test_settings_are_the_effective_values_for_the_environment(project, monkeypatch):
    assert context.extension("acme-resolution").settings["dry_run"] is False
    monkeypatch.setenv("VECTEN_ENV", "staging")
    handle = context.extension("acme-resolution")
    assert handle.env == "staging"
    assert dict(handle.settings) == {"daily_cap": 1000, "dry_run": True}


def test_settings_are_read_only(project):
    with pytest.raises(TypeError):
        context.extension("acme-resolution").settings["daily_cap"] = 5


def test_outside_compose_a_declared_secret_comes_from_dotenv(project):
    assert context.extension("acme-resolution").secret("ACME_API_KEY") == "from-dotenv"


def test_inside_compose_a_declared_secret_comes_from_the_environment(project, monkeypatch):
    monkeypatch.setenv("VECTEN_COMPONENT", "platform")
    monkeypatch.setenv("ACME_API_KEY", "from-compose")
    assert context.extension("acme-resolution").secret("ACME_API_KEY") == "from-compose"


def test_an_undeclared_secret_is_refused_naming_the_extension(project):
    with pytest.raises(Failure) as caught:
        context.extension("acme-resolution").secret("HUBSPOT_API_KEY")
    assert "acme-resolution" in caught.value.message
    assert "HUBSPOT_API_KEY" in caught.value.message


def test_platform_code_cannot_read_an_mcp_secret(project, monkeypatch):
    monkeypatch.setenv("VECTEN_COMPONENT", "platform")
    monkeypatch.setenv("MCP_ACME_TOKEN", "would-leak")
    with pytest.raises(Failure, match="MCP server"):
        context.extension("acme-resolution").secret("MCP_ACME_TOKEN")


def test_mcp_code_reads_mcp_secrets_and_not_platform_ones(project, monkeypatch):
    monkeypatch.setenv("VECTEN_COMPONENT", "mcp")
    monkeypatch.setenv("MCP_ACME_TOKEN", "token")
    monkeypatch.setenv("ACME_API_KEY", "would-leak")
    handle = context.extension("acme-resolution")
    assert handle.secret("MCP_ACME_TOKEN") == "token"
    with pytest.raises(Failure, match="Airflow"):
        handle.secret("ACME_API_KEY")


def test_project_settings_and_secrets(project):
    handle = context.project()
    assert handle.settings["channel"] == "#data"
    assert handle.secret("SLACK_TOKEN") == "slack"
    with pytest.raises(Failure, match="project"):
        handle.secret("ACME_API_KEY")


def test_an_unlisted_extension_is_refused(project):
    with pytest.raises(Failure, match="acme-other"):
        context.extension("acme-other")


def test_invalid_settings_fail_on_first_access_in_that_environment_only(project, monkeypatch):
    doc = (project / "vecten.toml").read_text().replace("dry_run = true", 'dry_run = "yes"')
    (project / "vecten.toml").write_text(doc)
    monkeypatch.setenv("VECTEN_ENV", "staging")
    with pytest.raises(Failure, match="settings.acme-resolution.dry_run in staging"):
        context.extension("acme-resolution")
    monkeypatch.setenv("VECTEN_ENV", "local")
    assert context.extension("acme-resolution").settings["dry_run"] is False


def test_a_project_shipped_before_the_record_existed_says_so(project):
    (project / "extensions.lock.json").unlink()
    with pytest.raises(Failure, match="ship"):
        context.extension("acme-resolution")


class FakeCloud:
    reads: list = []

    def __init__(self, project_root, env):
        self.env = env

    def get_secret(self, name):
        FakeCloud.reads.append(name)
        return f"cloud-{name}"


@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_on_a_cloud_the_provider_reads_only_declared_names_of_the_component(
    project, monkeypatch, cloud
):
    FakeCloud.reads = []
    monkeypatch.setitem(provider_module._REGISTRY, cloud, FakeCloud)
    monkeypatch.setenv("VECTEN_PROVIDER", cloud)
    monkeypatch.setenv("VECTEN_ENV", "prod")
    monkeypatch.setenv("VECTEN_COMPONENT", "platform")
    handle = context.extension("acme-resolution")
    assert handle.secret("ACME_API_KEY") == "cloud-ACME_API_KEY"
    # A minimal host's identity could read the mcp scope; the API refuses before asking it.
    with pytest.raises(Failure, match="MCP server"):
        handle.secret("MCP_ACME_TOKEN")
    assert FakeCloud.reads == ["ACME_API_KEY"]
