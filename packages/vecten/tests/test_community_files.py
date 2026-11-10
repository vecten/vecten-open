from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
FORMS = REPO / ".github" / "ISSUE_TEMPLATE"


def test_community_files_exist():
    for name in ("CODE_OF_CONDUCT.md", "SECURITY.md", "CHANGELOG.md", "CONTRIBUTING.md"):
        assert (REPO / name).is_file(), name
    assert (REPO / ".github" / "pull_request_template.md").is_file()


def test_issue_forms_parse():
    for name in ("bug.yml", "feature.yml"):
        form = yaml.safe_load((FORMS / name).read_text())
        assert form["name"] and form["description"], name
        assert isinstance(form["body"], list) and form["body"], name
    config = yaml.safe_load((FORMS / "config.yml").read_text())
    assert config["blank_issues_enabled"] is False
    assert any("discussions" in link["url"] for link in config["contact_links"])


def test_code_of_conduct_names_a_contact():
    assert "@m-puchala" in (REPO / "CODE_OF_CONDUCT.md").read_text()


def test_changelog_has_the_first_release():
    assert "## [0.1.0]" in (REPO / "CHANGELOG.md").read_text()


def test_ci_declares_read_only_permissions():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
