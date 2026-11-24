import pytest

pytest.importorskip("airflow")


def test_reapplying_pool_and_variable_declarations_in_airflow(tmp_path, monkeypatch):
    from airflow.api_fastapi.auth.managers.simple.simple_auth_manager import SimpleAuthManager
    from airflow.models.pool import Pool
    from airflow.models.variable import Variable

    from vecten_platform.airflow_resources import apply

    root = tmp_path / "vecten.toml"
    config = (
        'users=1\n[project]\nname="demo"\nscaffold_version="0.1.1"\n'
        '[airflow.pools]\nmetered=2\n[airflow.variables]\nfreeze="off"\n'
    )
    root.write_text(config)
    apply(tmp_path)
    assert Pool.get_pool("metered").slots == 2 and Variable.get("freeze") == "off"
    Pool.create_or_update_pool("metered", 2, "Owned by the operator", include_deferred=True)
    Variable.set("freeze", "on")
    root.write_text(config.replace("metered=2", "metered=3"))
    apply(tmp_path)
    pool = Pool.get_pool("metered")
    assert pool.slots == 3 and Variable.get("freeze") == "on"
    assert pool.include_deferred and pool.description == "Owned by the operator"
    monkeypatch.setenv("AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS", "admin:admin,monitor:viewer")
    manager = SimpleAuthManager()
    viewer = next(person for person in manager.get_users() if person.username == "monitor")
    assert manager.is_authorized_pool(method="GET", user=viewer)
    assert not manager.is_authorized_pool(method="POST", user=viewer)
    assert not manager.is_authorized_dag(method="DELETE", user=viewer)
