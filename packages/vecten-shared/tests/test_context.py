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


def _service_project(project, monkeypatch, host="platform"):
    import tomllib

    from vecten_shared.services import parse_service

    declared = {
        "command": ["run"],
        "health": {"command": ["true"]},
        "components": ["platform", "mcp"],
    }
    service = parse_service("acme-resolution", "portal", declared).service
    record = {
        "acme-resolution": Extension(
            "1.0",
            (),
            secrets=("ACME_API_KEY", "MCP_ACME_TOKEN"),
            settings=SCHEMA,
            services={"portal": service},
        )
    }
    (project / "extensions.lock.json").write_text(record_text(record))
    doc = tomllib.loads((project / "vecten.toml").read_text())
    doc["services"] = {"acme-resolution": {"portal": {"host": host}}}
    (project / "vecten.toml").write_text(tomli_w.dumps(doc))
    monkeypatch.setenv("VECTEN_COMPONENT", "service")
    monkeypatch.setenv("VECTEN_SERVICE", "acme-resolution.portal")


def test_a_service_reads_its_secrets_from_its_environment_never_a_provider(project, monkeypatch):
    _service_project(project, monkeypatch)
    # A service holds no cloud identity, so a provider name in its environment changes nothing.
    monkeypatch.setenv("VECTEN_PROVIDER", "aws")
    monkeypatch.setenv("ACME_API_KEY", "from-compose")
    assert context.extension("acme-resolution").secret("ACME_API_KEY") == "from-compose"


def test_a_service_reads_only_what_its_host_receives(project, monkeypatch):
    _service_project(project, monkeypatch)
    monkeypatch.setenv("MCP_ACME_TOKEN", "x")
    with pytest.raises(Failure, match="MCP_ACME_TOKEN.*acme-resolution.portal"):
        context.extension("acme-resolution").secret("MCP_ACME_TOKEN")


def test_a_service_on_the_mcp_host_reads_mcp_names(project, monkeypatch):
    _service_project(project, monkeypatch, host="mcp")
    monkeypatch.setenv("MCP_ACME_TOKEN", "tool-token")
    assert context.extension("acme-resolution").secret("MCP_ACME_TOKEN") == "tool-token"


def test_a_service_asking_for_an_undeclared_secret_names_extension_and_secret(project, monkeypatch):
    _service_project(project, monkeypatch)
    monkeypatch.setenv("LOADER_PASSWORD", "stack-only")
    with pytest.raises(
        Failure, match="acme-resolution does not declare the secret LOADER_PASSWORD"
    ):
        context.extension("acme-resolution").secret("LOADER_PASSWORD")


def test_a_service_the_project_does_not_bind_is_refused(project, monkeypatch):
    _service_project(project, monkeypatch)
    monkeypatch.setenv("VECTEN_SERVICE", "acme-resolution.other")
    with pytest.raises(Failure, match="acme-resolution.other"):
        context.extension("acme-resolution")
