"""Git-sourced extensions in the image layer, the bundle and the local stack: the source is
fetched on a laptop or in CI, checked against its recorded `source_sha256`, and built on the
host from `extensions/<name>/` like an in-repo extension."""

from __future__ import annotations

import pytest

from vecten import git_extensions, locking
from vecten.stack.compose import _build
from vecten.stack.images import write_project_layer
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.extension_record import Extension, GitOrigin
from vecten_shared.output import Failure

NAME = "acme-resolution"
COMMIT = "3f2c" + "0" * 34 + "e1"
URL = "https://git.example.com/acme/extensions"
DIGEST = "a" * 64
VERIFY = "python -m vecten_shared.git_sources verify /tmp/sources /tmp/extensions.lock.json"


def config(**extra) -> VectenConfig:
    return VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": [NAME],
            **extra,
        }
    )


def origin(commit: str = COMMIT, url: str = URL) -> GitOrigin:
    return GitOrigin(url, commit, f"extensions/{NAME}", f"{NAME}/v1.2.0", False)


def git_project(
    root,
    c: VectenConfig,
    found: GitOrigin | None = None,
    digest: str = DIGEST,
    others: dict[str, Extension] | None = None,
):
    """A project as `vecten lock` leaves it with one git-sourced extension, beside the in-repo
    extensions `others` names."""
    found = found or origin()
    save_config(root, c)
    record = {
        NAME: Extension(
            "1.2.0",
            ("platform",),
            git=found,
            source_sha256=digest,
            build_requires=("hatchling==1.26.3", "pluggy==1.5.0"),
        ),
        **(others or {}),
    }
    line = git_extensions.line_for(NAME, (), found)
    paths = [f"./{e.source_path}\n" for e in (others or {}).values()]
    (root / locking.PINS_FILE).write_text("".join(paths) + f"{line}\n")
    (root / locking.RECORD_FILE).write_text(locking.record_text(record))
    pins = locking.read_pins(root)
    for component in locking.components(c):
        roots = locking.roots(c, pins, record, component)
        digest = locking.inputs_hash(component, [*roots, *locking.metadata_markers(record)])
        stamp = locking.stamp_line(component, digest)
        (root / locking.LOCK_FILES[component]).write_text(f"{stamp}\n{line}\nhttpx==0.28.1\n")
    return line


# --- the image layer -------------------------------------------------------------------------


def test_the_layer_verifies_then_installs_a_git_source_like_an_in_repo_extension(tmp_path):
    c = config()
    git_project(tmp_path, c)
    stack = tmp_path / ".vecten" / "stack"
    write_project_layer(stack, c, None)
    build = stack / "build"
    assert (build / "build-constraints.txt").read_text() == "hatchling==1.26.3\npluggy==1.5.0\n"
    record = (tmp_path / locking.RECORD_FILE).read_bytes()
    assert (build / locking.RECORD_FILE).read_bytes() == record
    for kind in ("airflow", "mcp"):
        # The git line is installed from the bundled source, never fetched by pip.
        assert (build / f"requirements-{kind}.txt").read_text() == "httpx==0.28.1\n"
        text = (build / f"{kind}.Dockerfile").read_text()
        assert "--mount=type=bind,from=sources,target=/tmp/sources,rw" in text
        assert (
            "--mount=type=bind,source=extensions.lock.json,target=/tmp/extensions.lock.json"
        ) in text
        assert "from=extensions," not in text
        # The hash is checked before anything builds, outside the hidden pip log.
        assert f"{VERIFY} && python -c" in text
        assert (
            "--no-deps --build-constraint /tmp/build-constraints.txt "
            f"/tmp/sources/{NAME} && pip check"
        ) in text
        assert "COPY extensions.lock.json" not in text
        assert "git+" not in text


def test_in_repo_and_git_sources_install_in_one_step(tmp_path):
    from vecten import in_repo

    from .test_in_repo import make

    make(tmp_path)
    info = in_repo.check(tmp_path, "./extensions/acme-portal")
    portal = Extension(
        info.version,
        info.extras,
        source_path=info.path,
        metadata_sha256=info.metadata_sha256,
        build_requires=("hatchling==1.26.3",),
    )
    c = config(extensions=[NAME, "acme-portal"])
    git_project(tmp_path, c, others={"acme-portal": portal})
    stack = tmp_path / ".vecten" / "stack"
    write_project_layer(stack, c, None)
    text = (stack / "build" / "airflow.Dockerfile").read_text()
    assert "from=extensions,target=/tmp/extensions,rw" in text
    assert "from=sources,target=/tmp/sources,rw" in text
    assert f"/tmp/extensions/acme-portal /tmp/sources/{NAME} && pip check" in text


def test_a_url_line_the_record_does_not_hold_still_fails_the_layer(tmp_path):
    c = config()
    line = git_project(tmp_path, c)
    other = git_extensions.line_for(NAME, (), origin(commit="b" * 40))
    for name in locking.LOCK_FILES.values():
        path = tmp_path / name
        path.write_text(path.read_text().replace(line, other))
    with pytest.raises(Failure, match="vecten extension add"):
        write_project_layer(tmp_path / ".vecten" / "stack", c, None)


def test_a_project_without_git_sources_has_no_record_or_verify_in_its_build(tmp_path):
    from .test_extension_builds import _stamped_project

    c = config(extensions=["acme-demo"])
    _stamped_project(tmp_path, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))
    stack = tmp_path / ".vecten" / "stack"
    (stack / "build").mkdir(parents=True)
    (stack / "build" / locking.RECORD_FILE).write_text("left by an earlier render")
    write_project_layer(stack, c, None)
    assert not (stack / "build" / locking.RECORD_FILE).exists()
    for kind in ("airflow", "mcp"):
        text = (stack / "build" / f"{kind}.Dockerfile").read_text()
        assert "sources" not in text and "verify" not in text


# --- Compose -------------------------------------------------------------------------------------


def test_the_sources_context_is_the_bundles_extensions_on_a_vm_and_a_fetched_copy_locally(
    tmp_path,
):
    git_project(tmp_path, config())
    stack = tmp_path / ".vecten" / "stack"
    assert _build("vm", stack, "airflow.Dockerfile")["additional_contexts"] == {
        "sources": "../../extensions"
    }
    assert _build("local", stack, "airflow.Dockerfile")["additional_contexts"] == {
        "sources": str(stack / "sources")
    }


def test_compose_without_git_sources_has_no_sources_context(tmp_path):
    from .test_extension_builds import _stamped_project

    c = config(extensions=["acme-demo"])
    _stamped_project(tmp_path, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))
    stack = tmp_path / ".vecten" / "stack"
    for mode in ("vm", "local"):
        assert "additional_contexts" not in _build(mode, stack, "airflow.Dockerfile")
