import subprocess

import pytest

from vecten.stack.compose import generate_compose
from vecten.stack.images import write_project_layer
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.output import Failure


def write_locks(root, text):
    for name in ("requirements.lock", "requirements-airflow.lock", "requirements-mcp.lock"):
        (root / name).write_text(text)


def config(**extra):
    return VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": ["acme-demo==1.0"],
            **extra,
        }
    )


def test_both_images_use_lock_and_refuse_stale_records(tmp_path):
    c = config()
    stack = tmp_path / ".vecten" / "stack"
    (tmp_path / "requirements.txt").write_text("acme-demo==1.0\n")
    write_locks(tmp_path, "acme-demo==1.0\nhelper==2.0\n")
    write_project_layer(stack, c, None)
    assert (stack / "build" / "requirements.txt").read_text() == "acme-demo==1.0\nhelper==2.0\n"
    c.extensions = ["acme-demo==2.0"]
    with pytest.raises(Failure, match="stale"):
        write_project_layer(stack, c, None)


def test_private_index_uses_build_secrets_only(tmp_path):
    c = config(packages={"private_index": True})
    stack = tmp_path / ".vecten" / "stack"
    write_project_layer(stack, c, None)
    for kind in ("airflow", "mcp"):
        text = (stack / "build" / f"{kind}.Dockerfile").read_text()
        assert "--mount=type=secret,id=package_index" in text
        assert "ARG " not in text and "ENV PIP" not in text
    doc = generate_compose(c, "local", ["platform", "mcp"], stack)
    assert doc["secrets"]["platform_package_index"]["environment"] == "PACKAGE_INDEX_URL"
    assert doc["secrets"]["mcp_package_index"]["environment"] == "PACKAGE_INDEX_URL"
    for name in ("airflow-api-server", "mcp-server"):
        build = doc["services"][name]["build"]
        assert build["secrets"][0]["target"] == "package_index"
        assert not any("INDEX" in key for key in doc["services"][name]["environment"])
    assert "build" not in doc["services"]["airflow-scheduler"]


def test_uv_keeps_private_credentials_out_of_arguments_and_errors(tmp_path, monkeypatch):
    from vecten.extensions import _uv
    from vecten_shared.provider.local import LocalProvider

    save_config(tmp_path, config(packages={"private_index": True}))
    LocalProvider(tmp_path).put_secret(
        "PACKAGE_INDEX_URL", "https://person:secret@example.test/simple"
    )
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "secret", "secret")

    monkeypatch.setattr("vecten.extensions.shutil.which", lambda _: "/bin/uv")
    monkeypatch.setattr("vecten.extensions.subprocess.run", run)
    with pytest.raises(Failure) as exc:
        _uv(["pip", "install", "acme-demo"], tmp_path)
    assert "secret" not in exc.value.message
    assert "secret" not in repr(seen[0][0])
    assert seen[0][1]["env"]["UV_INDEX_URL"].endswith("@example.test/simple")


def test_dag_loader_and_mcp_config_are_mounted(tmp_path):
    import yaml

    from vecten.stack.compose import write_compose

    c = config()
    save_config(tmp_path, c)
    doc = yaml.safe_load(write_compose(tmp_path, c).read_text())
    assert (
        "./extension_dags.py:/opt/airflow/dags/vecten_extensions.py:ro"
        in doc["services"]["airflow-dag-processor"]["volumes"]
    )
    assert "../../vecten.toml:/app/vecten.toml:ro" in doc["services"]["mcp-server"]["volumes"]
    assert "load_dags" in (tmp_path / ".vecten/stack/extension_dags.py").read_text()


def test_lock_cannot_disagree_with_pinned_extension(tmp_path):
    c = config()
    (tmp_path / "requirements.txt").write_text("acme-demo==1.0\n")
    write_locks(tmp_path, "acme-demo==9.0\n")
    with pytest.raises(Failure, match="lock"):
        write_project_layer(tmp_path / ".vecten/stack", c, None)


def test_build_secret_is_readable_by_nonroot_image_users(tmp_path):
    write_project_layer(tmp_path, config(packages={"private_index": True}), None)
    for name in ("airflow", "mcp"):
        assert "mode=0444" in (tmp_path / "build" / f"{name}.Dockerfile").read_text()


def test_docker_build_gets_index_secret_from_local_store(tmp_path, monkeypatch):
    from vecten.stack.docker import SubprocessDocker
    from vecten_shared.provider.local import LocalProvider

    save_config(tmp_path, config(packages={"private_index": True}))
    LocalProvider(tmp_path).put_secret(
        "PACKAGE_INDEX_URL", "https://person:fixture-token@example.test/simple"
    )
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("vecten.stack.docker.subprocess.run", run)
    SubprocessDocker().compose(tmp_path, [], [], ["up", "--build"])
    assert seen[0]["env"]["PACKAGE_INDEX_URL"].endswith("@example.test/simple")


def test_requirements_only_upgrade_is_consumed_by_both_images(tmp_path):
    c = config(extensions=["acme-demo"])
    stack = tmp_path / ".vecten/stack"
    for version in ("1.0", "2.0"):
        (tmp_path / "requirements.txt").write_text(f"acme-demo=={version}\n")
        write_locks(tmp_path, f"acme-demo=={version}\nhelper==2.0\n")
        write_project_layer(stack, c, None)
        assert f"acme-demo=={version}\n" in (stack / "build/requirements.txt").read_text()
        for kind in ("airflow", "mcp"):
            assert (
                "-r /tmp/requirements.txt" in (stack / "build" / f"{kind}.Dockerfile").read_text()
            )


def test_public_resolution_failure_has_useful_diagnostics(tmp_path, monkeypatch):
    from vecten.extensions import _uv

    save_config(tmp_path, config())
    monkeypatch.setattr("vecten.extensions.shutil.which", lambda _: "/bin/uv")
    monkeypatch.setattr("vecten.extensions.package_environment", lambda _: {})
    monkeypatch.setattr(
        "vecten.extensions.subprocess.run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 1, "", "No version of acme-demo satisfies >=99"
        ),
    )
    with pytest.raises(Failure, match="No version of acme-demo satisfies >=99"):
        _uv(["pip", "compile"], tmp_path)


def test_public_build_keeps_install_failure_diagnostics(tmp_path):
    write_project_layer(tmp_path, config(), None)
    for kind in ("airflow", "mcp"):
        text = (tmp_path / "build" / f"{kind}.Dockerfile").read_text()
        assert "cat /tmp/vecten-pip.log" in text


def test_images_use_separate_runtime_locks_and_preserve_base_packages(tmp_path):
    c = config()
    (tmp_path / "requirements.txt").write_text("acme-demo==1.0\n")
    (tmp_path / "requirements.lock").write_text("acme-demo==1.0\nhelper==3.0\n")
    stack = tmp_path / ".vecten/stack"
    for kind, version in [("airflow", "1.0"), ("mcp", "2.0")]:
        (tmp_path / f"requirements-{kind}.lock").write_text(f"acme-demo==1.0\nhelper=={version}\n")
    write_project_layer(stack, c, None)
    for kind, version in [("airflow", "1.0"), ("mcp", "2.0")]:
        assert f"helper=={version}\n" in (stack / "build" / f"requirements-{kind}.txt").read_text()
        text = (stack / "build" / f"{kind}.Dockerfile").read_text()
        assert f"COPY requirements-{kind}.txt" in text
        assert "--constraint /tmp/vecten-base-constraints.txt" in text


def _stamped_project(root, c, lock_text):
    """A project as `vecten lock` leaves it: pins, record and fresh stamped locks."""
    from vecten import locking

    (root / "requirements.txt").write_text("acme-demo==1.0\n")
    record = {"acme-demo": locking.Extension("1.0", ("platform",))}
    (root / locking.RECORD_FILE).write_text(locking.record_text(record))
    for component in locking.components(c):
        roots = locking.roots(c, locking.read_pins(root), record, component)
        stamp = locking.stamp_line(component, locking.inputs_hash(component, roots))
        (root / locking.LOCK_FILES[component]).write_text(f"{stamp}\n{lock_text[component]}")


def test_stamped_locks_give_each_image_its_own_packages(tmp_path):
    c = config(extensions=["acme-demo"])
    stack = tmp_path / ".vecten/stack"
    _stamped_project(
        tmp_path,
        c,
        {
            "cli": "acme-demo==1.0\n",
            "platform": "acme-demo==1.0\npolars==1.0\n",
            "mcp": "acme-demo==1.0\nrapidfuzz==3.0\n",
        },
    )
    write_project_layer(stack, c, None)
    airflow = (stack / "build" / "requirements-airflow.txt").read_text()
    mcp = (stack / "build" / "requirements-mcp.txt").read_text()
    assert airflow == "acme-demo==1.0\npolars==1.0\n"
    assert mcp == "acme-demo==1.0\nrapidfuzz==3.0\n"


def test_a_stale_stamped_lock_is_refused_naming_the_lock_and_vecten_lock(tmp_path):
    from vecten_shared.config import ComponentDependencies

    c = config(extensions=["acme-demo"])
    _stamped_project(tmp_path, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))
    c.dependencies.mcp = ComponentDependencies(python=["rapidfuzz"])
    with pytest.raises(Failure) as caught:
        write_project_layer(tmp_path / ".vecten/stack", c, None)
    assert "requirements-mcp.lock" in caught.value.message
    assert "vecten lock" in caught.value.message
    assert not (tmp_path / ".vecten/stack/build/mcp.Dockerfile").exists()


def test_a_stamped_lock_without_its_record_is_refused(tmp_path):
    from vecten import locking

    c = config(extensions=["acme-demo"])
    _stamped_project(tmp_path, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))
    (tmp_path / locking.RECORD_FILE).unlink()
    with pytest.raises(Failure, match="vecten lock"):
        write_project_layer(tmp_path / ".vecten/stack", c, None)


def test_project_requirements_without_locks_are_refused(tmp_path):
    c = config(extensions=[], dependencies={"platform": {"python": ["polars"]}})
    with pytest.raises(Failure) as caught:
        write_project_layer(tmp_path / ".vecten/stack", c, None)
    assert "vecten lock" in caught.value.message


def test_an_unstamped_lock_refusal_names_vecten_lock(tmp_path):
    c = config()
    (tmp_path / "requirements.txt").write_text("acme-demo==1.0\n")
    write_locks(tmp_path, "acme-demo==9.0\n")
    with pytest.raises(Failure, match="vecten lock"):
        write_project_layer(tmp_path / ".vecten/stack", c, None)


def test_project_requirements_in_a_project_locked_by_an_earlier_release_are_refused(tmp_path):
    c = config(dependencies={"platform": {"python": ["polars"]}})
    (tmp_path / "requirements.txt").write_text("acme-demo==1.0\n")
    write_locks(tmp_path, "acme-demo==1.0\n")
    with pytest.raises(Failure, match="vecten lock"):
        write_project_layer(tmp_path / ".vecten/stack", c, None)


def test_a_legacy_runtime_lock_must_match_the_pinned_extension(tmp_path):
    c = config(extensions=["acme-demo"])
    (tmp_path / "requirements.txt").write_text("acme-demo==1.0\n")
    (tmp_path / "requirements.lock").write_text("acme-demo==1.0\n")
    (tmp_path / "requirements-airflow.lock").write_text("acme-demo==9.0\n")
    (tmp_path / "requirements-mcp.lock").write_text("acme-demo==1.0\n")
    with pytest.raises(Failure, match="vecten lock"):
        write_project_layer(tmp_path / ".vecten/stack", c, None)


def test_system_packages_install_as_root_before_python_then_return_to_the_image_user(tmp_path):
    c = config(
        extensions=[],
        dependencies={"platform": {"system": ["libgomp1"]}, "mcp": {"system": ["libpq5=15.8"]}},
    )
    write_project_layer(tmp_path, c, None)
    for kind, package, user in (("airflow", "libgomp1", "airflow"), ("mcp", "libpq5=15.8", "app")):
        text = (tmp_path / "build" / f"{kind}.Dockerfile").read_text()
        assert text.splitlines()[1:4] == [
            "USER root",
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{package} && rm -rf /var/lib/apt/lists/* "
            f"&& dpkg -s {package.split('=')[0]} >/dev/null",
            f"USER {user}",
        ]


def test_a_dockerfile_without_system_packages_is_unchanged(tmp_path):
    with_none = tmp_path / "a"
    write_project_layer(with_none, config(), None)
    with_empty = tmp_path / "b"
    write_project_layer(with_empty, config(dependencies={"platform": {"system": []}}), None)
    for kind in ("airflow", "mcp"):
        text = (with_none / "build" / f"{kind}.Dockerfile").read_text()
        assert "USER root" not in text and "apt-get" not in text
        assert text == (with_empty / "build" / f"{kind}.Dockerfile").read_text()


# --- in-repo extensions -------------------------------------------------------


def _in_repo_project(root, c):
    from vecten import locking
    from vecten_shared.extension_record import Extension

    from .test_in_repo import make

    make(root)
    info = __import__("vecten.in_repo", fromlist=["read"]).check(root, "./extensions/acme-portal")
    record = {
        "acme-portal": Extension(
            info.version,
            info.extras,
            source_path=info.path,
            metadata_sha256=info.metadata_sha256,
            build_requires=("hatchling==1.26.3", "pluggy==1.5.0"),
        )
    }
    (root / "requirements.txt").write_text("./extensions/acme-portal\n")
    (root / locking.RECORD_FILE).write_text(locking.record_text(record))
    for component in locking.components(c):
        roots = locking.roots(c, {}, record, component)
        digest = locking.inputs_hash(component, [*roots, *locking.metadata_markers(record)])
        (root / locking.LOCK_FILES[component]).write_text(
            locking.stamp_line(component, digest) + "\nhttpx==0.28.1\n"
        )


def test_an_in_repo_extension_is_built_from_the_extra_context_without_resolving(tmp_path):
    c = config(extensions=["acme-portal"])
    _in_repo_project(tmp_path, c)
    stack = tmp_path / ".vecten" / "stack"
    write_project_layer(stack, c, None)
    assert (stack / "build" / "build-constraints.txt").read_text() == (
        "hatchling==1.26.3\npluggy==1.5.0\n"
    )
    for kind in ("airflow", "mcp"):
        text = (stack / "build" / f"{kind}.Dockerfile").read_text()
        assert "--mount=type=bind,from=extensions,target=/tmp/extensions,rw" in text
        assert "--no-deps" in text and "-r /tmp/requirements.txt" in text
        assert (
            "--no-deps --build-constraint /tmp/build-constraints.txt /tmp/extensions/acme-portal"
            in text
        )
        assert "pip check" in text


def test_a_locked_project_without_in_repo_extensions_installs_its_lock_without_resolving(
    tmp_path,
):
    c = config(extensions=["acme-demo"])
    _stamped_project(tmp_path, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))
    stack = tmp_path / ".vecten" / "stack"
    write_project_layer(stack, c, None)
    for kind in ("airflow", "mcp"):
        text = (stack / "build" / f"{kind}.Dockerfile").read_text()
        assert "--no-deps" in text
        assert "from=extensions" not in text
    assert not (stack / "build" / "build-constraints.txt").exists()


def test_an_unlocked_or_legacy_project_keeps_todays_install(tmp_path):
    for root, prepare in (
        (tmp_path / "unlocked", lambda r: None),
        (
            tmp_path / "legacy",
            lambda r: (
                (r / "requirements.txt").write_text("acme-demo==1.0\n"),
                write_locks(r, "acme-demo==1.0\n"),
            ),
        ),
    ):
        root.mkdir()
        prepare(root)
        stack = root / ".vecten" / "stack"
        write_project_layer(stack, config(), None)
        for kind in ("airflow", "mcp"):
            text = (stack / "build" / f"{kind}.Dockerfile").read_text()
            assert "--no-deps" not in text and "extensions" not in text


def test_a_build_rechecks_the_in_repo_rules(tmp_path):
    c = config(extensions=["acme-portal"])
    _in_repo_project(tmp_path, c)
    (tmp_path / "extensions/acme-portal/src/link").symlink_to(tmp_path)
    with pytest.raises(Failure, match="symbolic link"):
        write_project_layer(tmp_path / ".vecten" / "stack", c, None)


@pytest.mark.parametrize("body", ["acme-demo==9.0\n", ""])
def test_a_stamped_lock_whose_body_breaks_its_inputs_is_refused(tmp_path, body):
    c = config(extensions=["acme-demo"])
    _stamped_project(tmp_path, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))
    stamp = (tmp_path / "requirements-mcp.lock").read_text().splitlines()[0]
    (tmp_path / "requirements-mcp.lock").write_text(f"{stamp}\n{body}")
    with pytest.raises(Failure, match="requirements-mcp.lock"):
        write_project_layer(tmp_path / ".vecten/stack", c, None)
