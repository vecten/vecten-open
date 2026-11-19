"""What each component's lock is resolved from, and how a lock records and checks its inputs."""

import json

import pytest

from vecten import locking
from vecten.locking import Extension
from vecten_shared.config import ComponentDependencies, VectenConfig
from vecten_shared.versions import VERSIONS

PROJECT = {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}
ALL_EXTRAS = Extension(version="1.0", extras=("cli", "mcp", "platform"))


def config(**extra):
    return VectenConfig.model_validate({**PROJECT, **extra})


def pins(*lines):
    return {locking.normalize_name(line.split("[")[0].split("=")[0]): line for line in lines}


def test_each_component_requests_only_its_own_extra_and_only_when_provided():
    c = config(extensions=["acme", "plain"])
    p = pins("acme==1.0", "plain==2.0")
    record = {"acme": ALL_EXTRAS, "plain": Extension(version="2.0", extras=())}
    assert locking.roots(c, p, record, "platform") == ["acme[platform]==1.0", "plain==2.0"]
    assert locking.roots(c, p, record, "mcp") == ["acme[mcp]==1.0", "plain==2.0"]
    assert locking.roots(c, p, record, "cli") == [
        "acme[cli]==1.0",
        "plain==2.0",
        f"vecten=={VERSIONS.vecten}",
    ]


def test_an_extra_the_project_asked_for_is_kept_beside_the_components():
    c = config(extensions=["acme[fast]"])
    roots = locking.roots(c, pins("acme[fast]==1.0"), {"acme": ALL_EXTRAS}, "platform")
    assert roots == ["acme[fast,platform]==1.0"]


def test_connector_dependencies_go_into_every_lock_and_project_requirements_into_their_own():
    c = config(
        connectors=[{"name": "crm", "source": "rest", "dependencies": ["httpx>=0.27"]}],
        dependencies={
            "platform": {"python": ["polars>=1,<2"]},
            "mcp": {"python": ["rapidfuzz>=3,<4"]},
            "cli": {"python": ["rich>=13"]},
        },
    )
    assert locking.roots(c, {}, {}, "platform") == ["httpx>=0.27", "polars>=1,<2"]
    assert locking.roots(c, {}, {}, "mcp") == ["httpx>=0.27", "rapidfuzz>=3,<4"]
    assert locking.roots(c, {}, {}, "cli") == [
        "httpx>=0.27",
        "rich>=13",
        f"vecten=={VERSIONS.vecten}",
    ]


def test_roots_are_unknown_while_an_extension_has_no_pin_or_no_record():
    c = config(extensions=["acme"])
    assert locking.roots(c, {}, {"acme": ALL_EXTRAS}, "platform") is None
    assert locking.roots(c, pins("acme==1.0"), {}, "platform") is None
    assert locking.roots(c, pins("acme>=1.0"), {"acme": ALL_EXTRAS}, "platform") is None


def test_a_record_for_another_version_does_not_count():
    c = config(extensions=["acme"])
    record = {"acme": Extension(version="0.9", extras=("platform",))}
    assert locking.roots(c, pins("acme==1.0"), record, "platform") is None


def test_the_inputs_hash_follows_the_roots_the_release_and_the_component(monkeypatch):
    first = locking.inputs_hash("platform", ["a==1", "b==2"])
    assert first == locking.inputs_hash("platform", ["b==2", "a==1"])
    assert first != locking.inputs_hash("platform", ["a==1"])
    assert first != locking.inputs_hash("mcp", ["a==1", "b==2"])
    monkeypatch.setattr(locking, "release", lambda: "9.9.9")
    assert first != locking.inputs_hash("platform", ["a==1", "b==2"])


def test_the_stamp_is_the_first_line_and_reads_back():
    line = locking.stamp_line("mcp", "ab" * 32)
    assert line == f"# vecten-lock: component=mcp inputs={'ab' * 32}"
    assert locking.read_stamp(f"{line}\nhttpx==0.27.0\n") == ("mcp", "ab" * 32)
    assert locking.read_stamp("httpx==0.27.0\n") is None
    assert locking.read_stamp("# vecten-lock: component=mcp\n") is None


def test_the_record_round_trips_sorted():
    record = {"zeta": Extension("2.0", ("platform",)), "acme": Extension("1.0", ("mcp", "cli"))}
    text = locking.record_text(record)
    assert list(json.loads(text)["extensions"]) == ["acme", "zeta"]
    assert json.loads(text)["extensions"]["acme"] == {
        "version": "1.0",
        "extras": ["cli", "mcp"],
        "secrets": [],
        "settings": None,
    }
    assert locking.parse_record(text) == {
        "acme": Extension("1.0", ("cli", "mcp")),
        "zeta": Extension("2.0", ("platform",)),
    }


@pytest.mark.parametrize("text", ["not json", "[]", '{"extensions": {"a": {"version": 1}}}'])
def test_a_malformed_record_reads_as_missing(text):
    assert locking.parse_record(text) is None


def _write_locked_project(root, c, record, lock_pins):
    (root / "requirements.txt").write_text("acme==1.0\n")
    (root / locking.RECORD_FILE).write_text(locking.record_text(record))
    for component in locking.components(c):
        roots = locking.roots(c, pins("acme==1.0"), record, component)
        stamp = locking.stamp_line(component, locking.inputs_hash(component, roots))
        (root / locking.LOCK_FILES[component]).write_text(f"{stamp}\n{lock_pins}")


def test_fresh_locks_have_no_problems(tmp_path):
    c = config(extensions=["acme"])
    _write_locked_project(tmp_path, c, {"acme": ALL_EXTRAS}, "acme==1.0\n")
    assert locking.lock_problems(tmp_path, c) == []


def test_a_changed_project_requirement_makes_only_that_components_lock_stale(tmp_path):
    c = config(extensions=["acme"])
    _write_locked_project(tmp_path, c, {"acme": ALL_EXTRAS}, "acme==1.0\n")
    c.dependencies.mcp = ComponentDependencies(python=["rapidfuzz"])
    assert locking.lock_problems(tmp_path, c) == ["requirements-mcp.lock"]


def test_a_missing_record_or_lock_is_a_problem(tmp_path):
    c = config(extensions=["acme"])
    _write_locked_project(tmp_path, c, {"acme": ALL_EXTRAS}, "acme==1.0\n")
    (tmp_path / "requirements-airflow.lock").unlink()
    assert locking.lock_problems(tmp_path, c) == ["requirements-airflow.lock"]
    (tmp_path / locking.RECORD_FILE).unlink()
    assert set(locking.lock_problems(tmp_path, c)) == {
        locking.RECORD_FILE,
        "requirements.lock",
        "requirements-airflow.lock",
        "requirements-mcp.lock",
    }


def test_an_unstamped_lock_is_reported_as_unstamped(tmp_path):
    c = config(extensions=["acme"])
    _write_locked_project(tmp_path, c, {"acme": ALL_EXTRAS}, "acme==1.0\n")
    (tmp_path / "requirements-mcp.lock").write_text("acme==1.0\n")
    assert locking.lock_state(tmp_path, c, "mcp") == "unstamped"
    assert locking.lock_state(tmp_path, c, "platform") == "fresh"


def test_divergence_lists_packages_pinned_differently_across_locks():
    locks = {
        "cli": "# vecten-lock: x\nhttpx==0.27.0\nrich==13.0\n",
        "platform": "httpx==0.26.0\nrich==13.0\n",
        "mcp": "httpx==0.27.0\n",
    }
    assert locking.divergence(locks) == {
        "httpx": {"cli": "0.27.0", "mcp": "0.27.0", "platform": "0.26.0"}
    }


def test_a_project_without_extensions_has_fresh_locks_with_an_empty_record(tmp_path):
    c = config()
    (tmp_path / locking.RECORD_FILE).write_text(locking.record_text({}))
    for component in locking.components(c):
        roots = locking.roots(c, {}, {}, component)
        stamp = locking.stamp_line(component, locking.inputs_hash(component, roots))
        (tmp_path / locking.LOCK_FILES[component]).write_text(stamp + "\n")
    assert locking.lock_problems(tmp_path, c) == []


def test_a_registration_constraint_the_pin_breaks_makes_the_locks_stale(tmp_path):
    c = config(extensions=["acme"])
    _write_locked_project(tmp_path, c, {"acme": ALL_EXTRAS}, "acme==1.0\n")
    c.extensions = ["acme>=2"]
    assert locking.lock_state(tmp_path, c, "platform") == "stale"
    assert locking.roots(c, pins("acme==1.0"), {"acme": ALL_EXTRAS}, "platform") is None


def test_an_invalid_registration_reads_as_stale_not_as_a_crash():
    c = config(extensions=["acme @ https://example.com/a.whl"])
    assert locking.roots(c, pins("acme==1.0"), {"acme": ALL_EXTRAS}, "platform") is None
