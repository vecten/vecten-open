from pathlib import Path

import pytest

from vecten.connectors.verified import DltVerifiedSources, SourceFiles, get_verified_sources


class FakeVerified:
    def names(self):
        return {"hubspot"}

    def materialize(self, name, target):
        (target / "__init__.py").write_text(
            "import dlt\n@dlt.source\n"
            "def hubspot(api_key: str = dlt.secrets.value):\n    return []\n"
        )
        return SourceFiles(requirements=["requests-oauthlib>=1.3,<2"])


def test_injection_picks_the_named_backend(monkeypatch):
    monkeypatch.setenv("VECTEN_VERIFIED_SOURCES", f"{__name__}:FakeVerified")
    assert isinstance(get_verified_sources(), FakeVerified)


def test_default_backend_is_dlt(monkeypatch):
    monkeypatch.delenv("VECTEN_VERIFIED_SOURCES", raising=False)
    assert isinstance(get_verified_sources(), DltVerifiedSources)


def test_requirements_drop_dlt_itself():
    assert DltVerifiedSources.parse_requirements(
        "dlt[postgres]>=1.6.0\nrequests>=2\n\n# c\nhubspot-sdk==1.0\n"
    ) == ["hubspot-sdk==1.0", "requests>=2"]


def test_requirements_keep_packages_that_only_start_with_dlt():
    text = "dlt\ndlt>=1\ndlt [s3]\ndlt;python_version>'3'\ndltflow==0.2\ndlt-extras~=1\n"
    assert DltVerifiedSources.parse_requirements(text) == ["dlt-extras~=1", "dltflow==0.2"]


def test_listing_goes_through_the_installed_dlt_init_command(monkeypatch):
    from dlt._workspace.cli import DEFAULT_VERIFIED_SOURCES_REPO, _init_command

    def listing(repo):
        assert repo == DEFAULT_VERIFIED_SOURCES_REPO
        return {"hubspot": object()}

    monkeypatch.setattr(_init_command, "_list_verified_sources", listing)
    assert DltVerifiedSources().names() == {"hubspot"}


def test_materialize_goes_through_the_installed_dlt_init_command(monkeypatch, tmp_path):
    from dlt._workspace.cli import DEFAULT_VERIFIED_SOURCES_REPO, _init_command

    def init(name, destination, repo):
        assert (name, destination, repo) == ("hubspot", "postgres", DEFAULT_VERIFIED_SOURCES_REPO)
        Path("hubspot").mkdir()
        Path("hubspot/__init__.py").write_text("")
        Path("requirements.txt").write_text("dlt>=1.31\nrequests>=2\n")

    monkeypatch.setattr(_init_command, "init_command", init)
    files = DltVerifiedSources().materialize("hubspot", tmp_path / "sources" / "hubspot")
    assert (tmp_path / "sources" / "hubspot" / "__init__.py").is_file()
    assert files.requirements == ["requests>=2"]


@pytest.mark.network
def test_real_listing_knows_hubspot():
    assert "hubspot" in DltVerifiedSources().names()


@pytest.mark.network
def test_real_materialize_writes_the_source_package(tmp_path):
    files = DltVerifiedSources().materialize("hubspot", tmp_path / "source")
    assert (tmp_path / "source" / "__init__.py").is_file()
    target = tmp_path / "source"
    assert (target / "__init__.py").is_file()
    assert all(not r.startswith("dlt") for r in files.requirements)
    assert not list(target.rglob("*_pipeline.py"))
    assert not list(target.rglob("requirements.txt"))
    assert not list(target.rglob(".dlt"))
