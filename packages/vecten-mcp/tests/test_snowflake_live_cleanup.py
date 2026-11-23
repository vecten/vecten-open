from unittest.mock import MagicMock, call

import pytest

from . import test_snowflake_live as live


@pytest.mark.parametrize("preflight_error", [pytest.fail.Exception, RuntimeError])
def test_refused_or_failed_preflight_closes_connection_without_cleanup(
    monkeypatch, preflight_error
):
    cursor = MagicMock()
    admin = MagicMock()
    admin.cursor.return_value = cursor
    monkeypatch.setattr(live, "_admin", lambda: admin)

    def ownership_query(cursor, sql):
        if preflight_error is RuntimeError:
            raise RuntimeError("ownership lookup failed")
        if sql.startswith("SHOW ROLES"):
            return [{"name": live._names().provisioner_role, "owner": "VECTEN_DEPLOY_LIVESF"}]
        return []

    monkeypatch.setattr(live, "_rows", ownership_query)
    with pytest.raises(preflight_error, match="VECTEN_DEPLOY_LIVESF|ownership lookup failed"):
        next(live.account.__wrapped__())
    cursor.execute.assert_not_called()
    cursor.close.assert_called_once()
    admin.close.assert_called_once()


def test_cleanup_only_drops_verified_fixture_objects(monkeypatch):
    names = live._names()
    answers = {
        f"SHOW INTEGRATIONS LIKE '{names.integration}'": [{"name": names.integration}],
        f"SHOW GRANTS ON INTEGRATION {names.integration}": [
            {"privilege": "OWNERSHIP", "granted_to": "ROLE", "grantee_name": live.ADMIN_ROLE}
        ],
        f"SHOW DATABASES LIKE '{names.database}'": [{"name": "VECTENXCI", "owner": "OTHER"}],
        f"SHOW USERS LIKE '{live.PERSON.upper()}'": [
            {"name": live.PERSON.upper(), "owner": names.provisioner_role}
        ],
        f"SHOW ROLES LIKE '{names.provisioner_role}'": [
            {"name": names.provisioner_role, "owner": live.ADMIN_ROLE}
        ],
    }
    monkeypatch.setattr(live, "_rows", lambda cursor, sql: answers.get(sql, []))
    cursor = MagicMock()
    live._teardown(cursor, names)
    assert cursor.execute.call_args_list == [
        call(f"GRANT ROLE {names.provisioner_role} TO ROLE {live.ADMIN_ROLE}"),
        call(f"DROP USER IF EXISTS {live.PERSON.upper()}"),
        call(f"DROP SECURITY INTEGRATION IF EXISTS {names.integration}"),
        call(f"DROP ROLE IF EXISTS {names.provisioner_role}"),
    ]


def test_partial_setup_removes_the_database_created_by_this_fixture(monkeypatch):
    admin = MagicMock()
    cursor = admin.cursor.return_value
    database_exists = False

    def execute(sql):
        nonlocal database_exists
        if sql.rstrip(";") == f"CREATE DATABASE {live.DATABASE}":
            database_exists = True
        elif sql == f"DROP DATABASE IF EXISTS {live.DATABASE}":
            database_exists = False
        else:
            raise RuntimeError("fixture setup interrupted")

    def ownership_query(cursor, sql):
        if database_exists and sql == f"SHOW DATABASES LIKE '{live.DATABASE}'":
            return [{"name": live.DATABASE, "owner": live.ADMIN_ROLE}]
        return []

    cursor.execute.side_effect = execute
    monkeypatch.setattr(live, "_admin", lambda: admin)
    monkeypatch.setattr(live, "_rows", ownership_query)
    with pytest.raises(RuntimeError, match="fixture setup interrupted"):
        next(live.account.__wrapped__())
    assert cursor.execute.call_args_list[-1] == call(f"DROP DATABASE IF EXISTS {live.DATABASE}")
    assert not database_exists
    cursor.close.assert_called_once()
    admin.close.assert_called_once()
