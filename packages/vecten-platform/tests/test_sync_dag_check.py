import pytest

from vecten_platform.sync.airflow_dag import failed_objects, raise_for_failed


def test_failed_objects_maps_indexes_to_names():
    states = {"sync_0": "success", "sync_1": "failed", "sync_2": "upstream_failed"}
    assert failed_objects(["a", "b", "c"], states) == ["b", "c"]


def test_skipped_and_successful_objects_pass():
    assert failed_objects(["a", "b"], {"sync_0": "success", "sync_1": "skipped"}) == []


def test_an_object_without_a_state_counts_as_failed():
    assert failed_objects(["a", "b"], {"sync_0": "success"}) == ["b"]


def test_raise_names_the_failed_objects():
    with pytest.raises(RuntimeError, match="demo: 1 of 3 objects failed: bad"):
        raise_for_failed(
            "demo",
            ["good_a", "bad", "good_b"],
            {"sync_0": "success", "sync_1": "failed", "sync_2": "success"},
        )


def test_no_failure_does_not_raise():
    raise_for_failed("demo", ["a"], {"sync_0": "success"})


def test_a_failed_object_raises_the_wrapper_naming_object_and_source_class(monkeypatch, tmp_path):
    from vecten_platform.sync import airflow_dag
    from vecten_platform.sync.airflow_dag import ObjectSyncError, sync_one
    from vecten_platform.sync.runner import ObjectOutcome

    ctx = type("Ctx", (), {"connectors_dir": tmp_path, "dlt_dir": tmp_path})()
    monkeypatch.setattr(airflow_dag, "runtime", lambda: ctx)
    monkeypatch.setattr(airflow_dag, "load_connector", lambda d, n: type("M", (), {"source": 0}))
    monkeypatch.setattr(airflow_dag, "warehouse_destination", lambda ctx: None)
    failed = ObjectOutcome("deals", False, 0, "KeyError: 'dealname'")
    monkeypatch.setattr(airflow_dag, "run_object", lambda *args: failed)
    with pytest.raises(ObjectSyncError) as raised:
        sync_one("hubspot", "deals")
    assert str(raised.value) == "hubspot.deals: KeyError: 'dealname'"
    assert raised.value.connector == "hubspot" and raised.value.object_name == "deals"
    assert isinstance(raised.value, RuntimeError)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ImportError("no module named hubspot_api"), "ImportError: no module named hubspot_api"),
        ("failure", "Failure: secret HUBSPOT_TOKEN is not set in prod"),
    ],
)
def test_every_failure_of_an_object_names_the_object(monkeypatch, tmp_path, raised, expected):
    from vecten_platform.sync import airflow_dag
    from vecten_platform.sync.airflow_dag import ObjectSyncError, sync_one
    from vecten_shared.issues import failure_class
    from vecten_shared.output import ExitCode, Failure

    if raised == "failure":
        raised = Failure(ExitCode.PREREQUISITE, "secret HUBSPOT_TOKEN is not set in prod")
    ctx = type("Ctx", (), {"connectors_dir": tmp_path, "dlt_dir": tmp_path})()
    monkeypatch.setattr(airflow_dag, "runtime", lambda: ctx)

    def load(directory, name):
        raise raised

    monkeypatch.setattr(airflow_dag, "load_connector", load)
    with pytest.raises(ObjectSyncError) as caught:
        sync_one("hubspot", "deals")
    assert str(caught.value) == f"hubspot.deals: {expected}"
    assert failure_class("ObjectSyncError", str(caught.value)) == expected.split(":")[0]
