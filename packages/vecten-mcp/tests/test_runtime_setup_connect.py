import logging

import psycopg
import pytest

from vecten_mcp.runtime_setup import connect_role, state_url
from vecten_shared.provider import ConnectionSpec

HOST = 'connection failed: connection to server at "10.0.0.5", port 5432 failed: FATAL:  '
WRONG_PASSWORD = HOST + 'password authentication failed for user "provisioner"'
NO_ROLE = HOST + 'role "provisioner" does not exist'
NO_HOST = (
    'connection failed: could not translate host name "db.internal" to address: '
    "nodename nor servname provided, or not known"
)


def failing(*messages):
    calls = []
    pending = list(messages)

    def attempt(role, password):
        calls.append((role, password))
        raise psycopg.OperationalError(pending.pop(0))

    attempt.calls = calls
    return attempt


def test_a_refused_login_names_both_causes():
    with pytest.raises(ValueError) as caught:
        connect_role("provisioner", ["a", "b"], failing(WRONG_PASSWORD, WRONG_PASSWORD))
    assert str(caught.value) == (
        "the warehouse refused the login `provisioner`: "
        "the role does not exist or its password does not match"
    )
    assert isinstance(caught.value.__cause__, psycopg.OperationalError)


def test_a_missing_login_says_so():
    with pytest.raises(ValueError) as caught:
        connect_role("provisioner", ["a"], failing(NO_ROLE))
    assert str(caught.value) == "the warehouse has no login `provisioner`"


def test_an_unreachable_warehouse_claims_no_authentication():
    with pytest.raises(ValueError) as caught:
        connect_role("provisioner", ["a"], failing(NO_HOST))
    assert str(caught.value) == "could not connect to the warehouse as `provisioner`"


def test_a_missing_login_wins_over_a_wrong_password():
    with pytest.raises(ValueError) as caught:
        connect_role("provisioner", ["a", "b"], failing(WRONG_PASSWORD, NO_ROLE))
    assert str(caught.value) == "the warehouse has no login `provisioner`"


def test_a_rotation_connects_with_the_pending_password():
    sentinel = object()
    calls = []

    def attempt(role, password):
        calls.append((role, password))
        if password == "current":
            raise psycopg.OperationalError(WRONG_PASSWORD)
        return sentinel

    assert connect_role("provisioner", ["current", "pending"], attempt) is sentinel
    assert calls == [("provisioner", "current"), ("provisioner", "pending")]


def test_a_rotation_that_connects_logs_no_warning(caplog):
    def attempt(role, password):
        if password == "current":
            raise psycopg.OperationalError(WRONG_PASSWORD)
        return object()

    with caplog.at_level(logging.DEBUG, logger="vecten_mcp.runtime_setup"):
        connect_role("provisioner", ["current", "pending"], attempt)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("text", "host"),
    [(WRONG_PASSWORD, "10.0.0.5"), (NO_ROLE, "10.0.0.5"), (NO_HOST, "db.internal")],
)
def test_no_message_names_the_host_and_the_log_does(text, host, caplog):
    with caplog.at_level(logging.WARNING, logger="vecten_mcp.runtime_setup"):
        with pytest.raises(ValueError) as caught:
            connect_role("provisioner", ["a"], failing(text))
    assert "10.0.0.5" not in str(caught.value)
    assert "db.internal" not in str(caught.value)
    assert any(host in record.getMessage() for record in caplog.records)


STATE = ConnectionSpec(
    kind="postgres", database="mcp_state", role="mcp_state", auth="password", host="db", port=5432
)


@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_a_deployed_state_connection_refuses_plain_text(cloud):
    url = state_url(STATE, "secret", cloud)
    assert url.query == {"sslmode": "require"}
    assert (url.host, url.port, url.database, url.username) == (
        "db",
        5432,
        "mcp_state",
        "mcp_state",
    )


def test_the_local_state_connection_takes_what_the_container_offers():
    assert "sslmode" not in state_url(STATE, "secret", "local").query
