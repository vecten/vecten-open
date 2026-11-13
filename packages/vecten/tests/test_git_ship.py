"""Git-sourced extensions in the image layer, the bundle and the local stack: the source is
fetched on a laptop or in CI, checked against its recorded `source_sha256`, and built on the
host from `extensions/<name>/` like an in-repo extension."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from vecten import git_extensions, locking
from vecten.stack.compose import _build
from vecten.stack.images import write_project_layer
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.extension_record import Extension, GitOrigin
from vecten_shared.git_sources import source_sha256
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
    build: tuple[str, ...] = ("hatchling==1.26.3", "pluggy==1.5.0"),
    body: str = "httpx==0.28.1\n",
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
            build_requires=build,
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
        (root / locking.LOCK_FILES[component]).write_text(f"{stamp}\n{line}\n{body}")
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


# --- fetching the recorded sources -------------------------------------------------------------

TAG = f"{NAME}/v1.2.0"


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    from .test_git_extensions import Remote, _load_git_http, _token

    base = tmp_path_factory.mktemp("git-ship")
    root = base / "root"
    root.mkdir()
    commits = Remote(root, base / "work")
    token = _token()
    server = (
        _load_git_http().GitServer(root=root, users={"x-access-token": token}).start(base / "tls")
    )
    yield SimpleNamespace(commits=commits, server=server, token=token, tmp=base)
    server.stop()


@pytest.fixture
def served(remote, monkeypatch, tmp_path):
    """`remote` with this test's git pointed at its CA and the token in the environment."""
    monkeypatch.setenv("GIT_SSL_CAINFO", str(remote.server.ca_path))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    host = remote.server.url.removeprefix("https://")
    monkeypatch.setenv("GIT_CREDENTIALS", f"https://x-access-token:{remote.token}@{host}/acme")
    remote.server.authorizations.clear()
    return remote


def served_origin(served, tag: str = TAG, commit: str | None = None) -> GitOrigin:
    from .test_git_extensions import REPOSITORY, SUBDIRECTORY

    commit = commit or served.commits.commits[tag]
    return GitOrigin(f"{served.server.url}/{REPOSITORY}", commit, SUBDIRECTORY, tag, False)


def served_project(root, served, c: VectenConfig | None = None, digest: str | None = None):
    root.mkdir(parents=True, exist_ok=True)
    digest = digest or served.commits.hash_of(TAG, served.tmp)
    git_project(root, c or config(), served_origin(served), digest)
    return root


def _files(directory) -> dict[str, tuple[bytes, bool]]:
    return {
        p.relative_to(directory).as_posix(): (p.read_bytes(), bool(p.stat().st_mode & 0o100))
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def test_each_recorded_commit_is_fetched_and_checked_into_its_folder(served, tmp_path):
    root = served_project(tmp_path / "p", served)
    into = tmp_path / "out"
    found = git_extensions.write_sources(root, config(), into)
    assert found == {NAME: into / NAME}
    files = _files(into / NAME)
    assert files["pyproject.toml"][0].startswith(b"[project]")
    assert files["bin/check"][1] and not files["pyproject.toml"][1]
    assert source_sha256(into / NAME) == served.commits.hash_of(TAG, served.tmp)
    assert not any(served.token[:8].encode() in body for body, _ in files.values())
    assert sorted(p.name for p in into.iterdir()) == [NAME]


def test_a_source_whose_hash_differs_from_the_record_is_refused_naming_both(served, tmp_path):
    root = served_project(tmp_path / "p", served, digest="b" * 64)
    into = tmp_path / "out"
    with pytest.raises(Failure) as caught:
        git_extensions.write_sources(root, config(), into)
    actual = served.commits.hash_of(TAG, served.tmp)
    message = caught.value.message
    assert NAME in message and served.commits.commits[TAG] in message
    assert "b" * 64 in message and actual in message
    assert not (into / NAME).exists()


def test_a_commit_the_host_no_longer_serves_names_the_commit_and_extension_add(served, tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    git_project(root, config(), served_origin(served, commit="c" * 40), DIGEST)
    with pytest.raises(Failure) as caught:
        git_extensions.write_sources(root, config(), tmp_path / "out")
    message = caught.value.message
    assert "c" * 40 in message and "acme/extensions" in message
    assert "vecten extension add" in message
    assert served.token[:8] not in message


def test_records_that_disagree_are_refused_before_any_fetch(served, tmp_path):
    root = served_project(tmp_path / "p", served)
    lock = root / "requirements-mcp.lock"
    lock.write_text(lock.read_text().replace("# tag", "# tag x"))
    with pytest.raises(Failure, match="vecten extension add"):
        git_extensions.write_sources(root, config(), tmp_path / "out")
    assert served.server.authorizations == []


def test_a_project_without_git_sources_fetches_nothing_and_reads_no_credential(
    tmp_path, monkeypatch
):
    from .test_extension_builds import _stamped_project

    c = config(extensions=["acme-demo"])
    _stamped_project(tmp_path, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))

    def credentials(root):
        raise AssertionError("credentials read")

    monkeypatch.setattr(git_extensions, "credentials", credentials)
    assert git_extensions.write_sources(tmp_path, c, tmp_path / "out") == {}
    assert not (tmp_path / "out").exists()


def test_a_kept_copy_is_reused_only_while_its_hash_matches(served, tmp_path):
    root = served_project(tmp_path / "p", served)
    into = tmp_path / "kept"
    git_extensions.write_sources(root, config(), into, keep=True)
    (into / "stale").mkdir()
    served.server.authorizations.clear()
    git_extensions.write_sources(root, config(), into, keep=True)
    assert served.server.authorizations == []
    assert sorted(p.name for p in into.iterdir()) == [NAME]
    (into / NAME / "pyproject.toml").write_text("tampered\n")
    git_extensions.write_sources(root, config(), into, keep=True)
    assert served.server.authorizations
    assert source_sha256(into / NAME) == served.commits.hash_of(TAG, served.tmp)


def test_a_kept_entry_that_is_a_link_is_replaced_by_a_fetched_folder(served, tmp_path):
    root = served_project(tmp_path / "p", served)
    elsewhere = tmp_path / "elsewhere"
    git_extensions.write_sources(root, config(), elsewhere)
    into = tmp_path / "kept"
    into.mkdir()
    (into / NAME).symlink_to(elsewhere / NAME)
    git_extensions.write_sources(root, config(), into, keep=True)
    assert not (into / NAME).is_symlink() and (into / NAME / "pyproject.toml").is_file()
    assert (elsewhere / NAME / "pyproject.toml").is_file()


def test_a_kept_folder_that_is_a_link_is_never_written_through(served, tmp_path):
    from .test_extension_builds import _stamped_project

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep-me").write_text("not the stack's\n")
    root = served_project(tmp_path / "p", served)
    into = tmp_path / "kept"
    into.symlink_to(elsewhere)
    git_extensions.write_sources(root, config(), into, keep=True)
    assert not into.is_symlink() and (into / NAME / "pyproject.toml").is_file()
    assert sorted(p.name for p in elsewhere.iterdir()) == ["keep-me"]
    plain = tmp_path / "plain"
    plain.mkdir()
    c = config(extensions=["acme-demo"])
    _stamped_project(plain, c, dict.fromkeys(("cli", "platform", "mcp"), "acme-demo==1.0\n"))
    other = tmp_path / "plain-kept"
    other.symlink_to(elsewhere)
    assert git_extensions.write_sources(plain, c, other, keep=True) == {}
    assert not other.exists() and not other.is_symlink()
    assert sorted(p.name for p in elsewhere.iterdir()) == ["keep-me"]


# --- the bundle ------------------------------------------------------------------------------


def _bundle_project(tmp_path, served, digest: str | None = None):
    from vecten_shared.config import load_config

    from .deploy_fixtures import render_aws

    root = render_aws(tmp_path / "proj")
    c = load_config(root)
    c.extensions = [NAME]
    served_project(root, served, c, digest)
    return root


def _build_bundle(root, out, **kw):
    from vecten.deploy.bundle import build_host_bundle
    from vecten_shared.config import load_config
    from vecten_shared.provider.environment import EnvironmentOutputs

    from .deploy_fixtures import COMMIT as SHIPPED
    from .deploy_fixtures import STANDARD_OUTPUTS

    outputs = EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    return build_host_bundle(
        root, load_config(root), "prod", outputs, "platform", SHIPPED, out, **kw
    )


def test_the_bundle_carries_each_git_source_beside_in_repo_extensions(served, tmp_path):
    import tarfile

    root = _bundle_project(tmp_path, served)
    bundle = _build_bundle(root, tmp_path / "out")
    with tarfile.open(bundle, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        compose = yaml.safe_load(tar.extractfile(".vecten/stack/docker-compose.yml").read())
        dockerfile = tar.extractfile(".vecten/stack/build/airflow.Dockerfile").read().decode()
        record = tar.extractfile(".vecten/stack/build/extensions.lock.json").read()
    prefix = f"extensions/{NAME}/"
    assert f"{prefix}pyproject.toml" in members
    # The executable bit is part of source_sha256, so it survives the bundle.
    assert members[f"{prefix}bin/check"].mode == 0o755
    assert members[f"{prefix}pyproject.toml"].mode == 0o644
    assert record == (root / locking.RECORD_FILE).read_bytes()
    builds = [s["build"] for s in compose["services"].values() if "build" in s]
    assert builds and all(
        b["additional_contexts"] == {"sources": "../../extensions"} for b in builds
    )
    assert VERIFY in dockerfile
    assert not (root / "extensions" / NAME).exists()


def test_a_bundle_whose_fetched_source_differs_from_the_record_is_not_written(served, tmp_path):
    root = _bundle_project(tmp_path, served, digest="d" * 64)
    with pytest.raises(Failure, match="d" * 64):
        _build_bundle(root, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.tar.gz"))


def test_a_project_folder_named_like_a_git_source_is_refused(served, tmp_path):
    root = _bundle_project(tmp_path, served)
    (root / "extensions" / NAME).mkdir(parents=True)
    (root / "extensions" / NAME / "pyproject.toml").write_text("[project]\n")
    with pytest.raises(Failure, match=f"extensions/{NAME}"):
        _build_bundle(root, tmp_path / "out")


def test_sources_fetched_once_are_handed_to_every_bundle(served, tmp_path, monkeypatch):
    root = _bundle_project(tmp_path, served)
    sources = git_extensions.write_sources(root, load(root), tmp_path / "sources")
    served.server.authorizations.clear()
    _build_bundle(root, tmp_path / "out", sources=sources)
    assert served.server.authorizations == []


def load(root):
    from vecten_shared.config import load_config

    return load_config(root)


@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_a_ship_fetches_every_source_before_any_host_changes(tmp_path, monkeypatch, cloud):
    from vecten.deploy import bundle
    from vecten_shared.provider.environment import EnvironmentOutputs

    from .deploy_fixtures import COMMIT as SHIPPED
    from .deploy_fixtures import STANDARD_OUTPUTS

    events = []

    def write_sources(root, c, into, keep=False):
        events.append("fetch")
        raise Failure(5, "acme-resolution at x: source_sha256 differs")

    monkeypatch.setattr(git_extensions, "write_sources", write_sources)
    monkeypatch.setattr(bundle, "prepare_bundle_metadata", lambda *a, **k: ({}, {}))
    outputs = EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    if cloud == "aws":
        from vecten.deploy import aws

        monkeypatch.setattr(aws, "prepare_bundle_metadata", lambda *a, **k: ({}, {}))
        monkeypatch.setattr(aws, "build_host_bundle", lambda *a, **k: events.append("bundle"))
        provider = aws.AwsProvider.__new__(aws.AwsProvider)
        monkeypatch.setattr(provider, "_check_env", lambda env: None, raising=False)
        monkeypatch.setattr(provider, "ship_host", lambda *a: events.append("ship"), raising=False)
        call = lambda: provider.ship_hosts(tmp_path, config(), "prod", outputs, SHIPPED)  # noqa: E731
    else:
        from vecten.deploy.scaleway import ship

        monkeypatch.setattr(ship, "prepare_bundle_metadata", lambda *a, **k: ({}, {}))
        monkeypatch.setattr(ship, "build_host_bundle", lambda *a, **k: events.append("bundle"))
        monkeypatch.setattr(ship, "ship_host", lambda *a, **k: events.append("ship"))
        plane = SimpleNamespace(storage=lambda kind: None, orphaned_volumes={})
        call = lambda: ship.ship_hosts(plane, tmp_path, config(), "prod", outputs, SHIPPED)  # noqa: E731
    with pytest.raises(Failure, match="source_sha256"):
        call()
    assert events == ["fetch"]


# --- `vecten up` ---------------------------------------------------------------------------------


def test_up_builds_from_a_copy_fetched_under_the_stack_directory(
    served, invoke, workdir, monkeypatch
):
    from vecten.stack import up as up_mod

    from .test_cmd_up_down import FakeDocker

    assert (
        invoke("init", "p", "--name", "acme", "--users", "8", "--source", "hubspot").exit_code == 0
    )
    root = workdir / "p"
    c = load(root)
    c.extensions = [NAME]
    served_project(root, served, c)
    monkeypatch.setattr(up_mod, "wait_for_front_doors", lambda urls, timeout: None)
    tracked = {p for p in root.rglob("*") if ".vecten" not in p.parts}
    up_mod.up(root, FakeDocker())
    stack = root / ".vecten" / "stack"
    assert source_sha256(stack / "sources" / NAME) == served.commits.hash_of(TAG, served.tmp)
    compose = yaml.safe_load((stack / "docker-compose.yml").read_text())
    builds = [s["build"] for s in compose["services"].values() if "build" in s]
    assert builds and all(
        b["additional_contexts"] == {"sources": str(stack / "sources")} for b in builds
    )
    assert {p for p in root.rglob("*") if ".vecten" not in p.parts} - tracked <= {
        root / ".env",
    }


def test_a_bundle_without_git_sources_fetches_nothing_and_keeps_its_modes(tmp_path, monkeypatch):
    import tarfile

    from .deploy_fixtures import render_aws

    root = render_aws(tmp_path / "proj")
    script = root / "extensions" / "acme-portal" / "bin" / "run"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)

    def refuse(*args, **kwargs):
        raise AssertionError("a project without git sources fetched or read a credential")

    monkeypatch.setattr(git_extensions, "fetch_copy", refuse)
    monkeypatch.setattr(git_extensions, "credentials", refuse)
    with tarfile.open(_build_bundle(root, tmp_path / "out"), "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert members["extensions/acme-portal/bin/run"].mode == 0o644
    assert ".vecten/stack/build/extensions.lock.json" not in members


def test_only_a_git_sources_files_keep_their_executable_bit():
    import tarfile

    from vecten.deploy.bundle import _normalised

    def mode(name: str, sources: frozenset[str]) -> int:
        info = tarfile.TarInfo(name)
        info.mode = 0o755
        return _normalised(info, sources).mode

    assert mode(f"extensions/{NAME}/bin/check", frozenset({NAME})) == 0o755
    assert mode("extensions/acme-portal/bin/run", frozenset({NAME})) == 0o644
    assert mode(".vecten/ship/apply.py", frozenset({NAME})) == 0o644


# The decompressed bundles a project without git sources ships, as the release without git
# sources built them for this project; gzip's own bytes depend on the zlib build, so they are
# left out. A change meant to alter every bundle updates these.
UNCHANGED_BUNDLES = {
    "platform": "1f457fd87595a57a4e24590558a90783c04be6d9592ad209f5972d4552457bc5",
    "mcp": "bf3f5f6de530b303d29c30b35637111d10872c571b7e5d86dd0860cc78d30c45",
}


def test_a_project_without_git_sources_ships_the_bytes_it_shipped_before_them(tmp_path):
    import gzip
    import hashlib

    from vecten.deploy.bundle import build_host_bundle
    from vecten_shared.provider.environment import EnvironmentOutputs

    from .deploy_fixtures import COMMIT as SHIPPED
    from .deploy_fixtures import STANDARD_OUTPUTS, render_aws

    root = render_aws(tmp_path / "proj")
    extension = root / "extensions" / "acme-portal"
    (extension / "src" / "acme_portal").mkdir(parents=True)
    (extension / "pyproject.toml").write_text("[project]\nname = 'acme-portal'\n")
    (extension / "src" / "acme_portal" / "__init__.py").write_text("")
    (extension / "bin").mkdir()
    (extension / "bin" / "run").write_text("#!/bin/sh\n")
    (extension / "bin" / "run").chmod(0o755)
    outputs = EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    # Lineage and feedback come from git and the MCP; pinned, the bundle depends on files alone.
    lineage = {"commit": SHIPPED, "ancestors": []}
    metadata = (lineage, {**lineage, "resolutions": [], "unreviewed": []})
    found = {}
    for host in UNCHANGED_BUNDLES:
        bundle = build_host_bundle(
            root, load(root), "prod", outputs, host, SHIPPED, tmp_path / "out", metadata=metadata
        )
        found[host] = hashlib.sha256(gzip.decompress(bundle.read_bytes())).hexdigest()
    assert found == UNCHANGED_BUNDLES
