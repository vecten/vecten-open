"""`vecten extension new`: a one-time copy of a template into extensions/<name>/."""

import json
import tomllib

import pytest

from vecten import extension_new
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "extensions").mkdir(parents=True)
    return root


def template(tmp_path, requires=None):
    source = tmp_path / "template"
    package = source / "src" / "__vecten_package__"
    (package / "dbt" / "models").mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "__vecten_name__"\nversion = "0.1.0"\n'
        'description = "__vecten_title__ for Vecten Open __vecten_version__"\n'
    )
    (package / "__init__.py").write_text('NAME = "__vecten_name__"\n')
    (package / "dbt" / "models" / "orders.sql").write_text("select * from {{ ref('orders') }}\n")
    (package / "logo.png").write_bytes(b"\x89PNG\r\n\x00__vecten_name__\xff")
    for junk in (".git/HEAD", "__pycache__/x.pyc", ".venv/bin/python", "dist/x.whl", "build/x"):
        (source / junk).parent.mkdir(parents=True, exist_ok=True)
        (source / junk).write_text("junk")
    if requires is not None:
        (source / "vecten-template.toml").write_text(
            f'description = "A portal"\nrequires_vecten = "{requires}"\n'
        )
    return source


def test_the_built_in_skeleton_is_a_complete_extension(project):
    result = extension_new.new_extension(project, "acme-portal", add=False)
    target = project / "extensions" / "acme-portal"
    assert result["path"] == "extensions/acme-portal"
    doc = tomllib.loads((target / "pyproject.toml").read_text())
    assert doc["project"]["name"] == "acme-portal"
    assert doc["project"]["version"] == "0.1.0"
    assert doc["build-system"]["requires"][0].startswith("hatchling==")
    assert "vecten.skills" in doc["project"]["entry-points"]
    assert (target / "src" / "acme_portal" / "__init__.py").is_file()
    assert (target / "src" / "acme_portal" / "skills" / "acme-portal" / "SKILL.md").is_file()
    assert any((target / "tests").glob("test_*.py"))
    text = "".join(p.read_text() for p in target.rglob("*") if p.is_file())
    assert "__vecten_" not in text
    assert not [p for p in target.rglob("*") if "__vecten_" in p.name]


def test_a_template_has_its_placeholders_replaced_in_text_and_names(project, tmp_path):
    extension_new.new_extension(project, "acme-portal", template=str(template(tmp_path)), add=False)
    target = project / "extensions" / "acme-portal"
    assert (target / "src" / "acme_portal" / "__init__.py").read_text() == 'NAME = "acme-portal"\n'
    description = tomllib.loads((target / "pyproject.toml").read_text())["project"]["description"]
    assert description == f"Acme Portal for Vecten Open {VERSIONS.vecten}"


def test_jinja_and_binary_files_are_copied_unchanged(project, tmp_path):
    extension_new.new_extension(project, "acme-portal", template=str(template(tmp_path)), add=False)
    package = project / "extensions" / "acme-portal" / "src" / "acme_portal"
    assert (package / "dbt" / "models" / "orders.sql").read_text() == (
        "select * from {{ ref('orders') }}\n"
    )
    assert (package / "logo.png").read_bytes() == b"\x89PNG\r\n\x00__vecten_name__\xff"


def test_repository_and_build_leftovers_and_the_template_file_are_not_copied(project, tmp_path):
    source = template(tmp_path, requires=">=0.1")
    extension_new.new_extension(project, "acme-portal", template=str(source), add=False)
    target = project / "extensions" / "acme-portal"
    for name in (".git", "__pycache__", ".venv", "dist", "build", "vecten-template.toml"):
        assert not list(target.rglob(name)), name


def test_a_symbolic_link_in_the_template_is_refused(project, tmp_path):
    source = template(tmp_path)
    (source / "src" / "link").symlink_to(tmp_path)
    with pytest.raises(Failure, match="link"):
        extension_new.new_extension(project, "acme-portal", template=str(source), add=False)
    assert not (project / "extensions" / "acme-portal").exists()


def test_a_template_for_a_newer_release_is_refused(project, tmp_path):
    source = template(tmp_path, requires=">=99.0")
    with pytest.raises(Failure, match="99.0"):
        extension_new.new_extension(project, "acme-portal", template=str(source), add=False)


def test_an_existing_target_is_refused_and_left_alone(project):
    target = project / "extensions" / "acme-portal"
    target.mkdir()
    (target / "keep").write_text("mine")
    with pytest.raises(Failure) as caught:
        extension_new.new_extension(project, "acme-portal", add=False)
    assert caught.value.code == ExitCode.REFUSED
    assert (target / "keep").read_text() == "mine"


@pytest.mark.parametrize("name", ["Acme_Portal", "acme portal", "-acme", "acme..portal", ""])
def test_a_name_that_is_not_a_normalised_distribution_name_is_refused(project, name):
    with pytest.raises(Failure) as caught:
        extension_new.new_extension(project, name, add=False)
    assert caught.value.code == ExitCode.USAGE


def test_the_copy_is_registered_unless_told_not_to(project, monkeypatch):
    added = []
    monkeypatch.setattr(extension_new, "add_extension", lambda root, arg: added.append(arg) or {})
    extension_new.new_extension(project, "acme-portal")
    assert added == ["./extensions/acme-portal"]


def test_a_git_template_is_read_once_and_nothing_records_where_from(project, tmp_path, monkeypatch):
    source = template(tmp_path)
    clones = []

    def clone(url, ref, destination):
        clones.append((url, ref))
        import shutil

        shutil.copytree(source.parent, destination, dirs_exist_ok=True)

    monkeypatch.setattr(extension_new, "_clone", clone)
    spec = "git+https://github.com/example/templates@teams-bot/v1.0.0#subdirectory=template"
    extension_new.new_extension(project, "acme-bot", template=spec, add=False)
    assert clones == [("https://github.com/example/templates", "teams-bot/v1.0.0")]
    written = "".join(p.read_text(errors="ignore") for p in project.rglob("*") if p.is_file())
    assert "github.com" not in written and "teams-bot/v1.0.0" not in written


@pytest.mark.parametrize(
    "spec",
    [
        "git+https://user:token@github.com/example/templates@v1",
        "git+http://github.com/example/templates@v1",
        "git+ssh://git@github.com/example/templates@v1",
        "git+https://github.com/example/templates",
        "git+https://github.com/example/templates@v1#subdirectory=../escape",
    ],
)
def test_a_git_template_must_be_https_with_a_ref_and_no_credentials(project, spec):
    with pytest.raises(Failure) as caught:
        extension_new.new_extension(project, "acme-bot", template=spec, add=False)
    assert "token" not in caught.value.message


def test_the_cli_command_creates_and_reports(invoke, workdir, monkeypatch):
    assert invoke("init", "p", "--name", "demo", "--users", "2", "--source", "demo").exit_code == 0
    monkeypatch.chdir(workdir / "p")
    r = invoke("extension", "new", "acme-portal", "--no-add", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["path"] == "extensions/acme-portal"


def test_build_output_and_the_template_file_are_skipped_only_at_the_template_root(
    project, tmp_path
):
    source = template(tmp_path, requires=">=0.1")
    nested = source / "src" / "__vecten_package__" / "static" / "dist"
    nested.mkdir(parents=True)
    (nested / "app.js").write_text("x")
    (source / "src" / "__vecten_package__" / "vecten-template.toml").write_text("kept = true\n")
    extension_new.new_extension(project, "acme-portal", template=str(source), add=False)
    package = project / "extensions" / "acme-portal" / "src" / "acme_portal"
    assert (package / "static" / "dist" / "app.js").is_file()
    assert (package / "vecten-template.toml").is_file()
    assert not (project / "extensions" / "acme-portal" / "dist").exists()


def test_a_git_template_subdirectory_that_is_a_link_is_refused(project, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("credentials")

    def clone(url, ref, destination):
        destination.mkdir(parents=True)
        (destination / "template").symlink_to(outside)

    monkeypatch.setattr(extension_new, "_clone", clone)
    spec = "git+https://github.com/example/templates@v1#subdirectory=template"
    with pytest.raises(Failure, match="link"):
        extension_new.new_extension(project, "acme-bot", template=spec, add=False)
    assert not (project / "extensions" / "acme-bot").exists()


def test_a_failed_registration_removes_the_copy(project, monkeypatch):
    def refuse(root, arg):
        raise Failure(ExitCode.PREREQUISITE, "Docker is not reachable")

    monkeypatch.setattr(extension_new, "add_extension", refuse)
    with pytest.raises(Failure, match="Docker"):
        extension_new.new_extension(project, "acme-portal")
    assert not (project / "extensions" / "acme-portal").exists()


@pytest.mark.parametrize("name", ["3d-portal", "import", "class"])
def test_a_name_whose_package_is_not_a_python_identifier_is_refused(project, name):
    with pytest.raises(Failure) as caught:
        extension_new.new_extension(project, name, add=False)
    assert caught.value.code == ExitCode.USAGE
