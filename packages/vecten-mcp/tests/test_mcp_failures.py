import psycopg.errors
from snowflake.connector.errors import ProgrammingError

from vecten_mcp.failures import (
    ModelNotReady,
    NoWarehouseAccess,
    describe_error,
    sign_in_reason,
    warehouse_reason,
)


def postgres_refusal():
    return psycopg.errors.InsufficientPrivilege("permission denied for table orders")


def snowflake_refusal():
    return ProgrammingError(msg="Current session is restricted", errno=3107, sqlstate="0A000")


def test_a_postgres_error_is_described_with_its_sqlstate():
    assert describe_error(postgres_refusal()) == (
        "InsufficientPrivilege 42501: permission denied for table orders"
    )
    assert warehouse_reason(postgres_refusal()) == (
        "the warehouse refused the request (42501): permission denied for table orders"
    )


def test_a_snowflake_error_is_described_with_its_error_code():
    assert describe_error(snowflake_refusal()) == (
        "ProgrammingError 3107: Current session is restricted"
    )
    assert warehouse_reason(snowflake_refusal()) == (
        "the warehouse refused the request (3107): Current session is restricted"
    )


def test_credentials_in_an_error_are_redacted():
    error = ValueError("postgres://user:fixture-secret@host/db password=hunter22 failed")
    described = describe_error(error)
    assert described.startswith("ValueError: ")
    assert "fixture-secret" not in described and "hunter22" not in described
    assert "[redacted]" in described


def test_an_error_that_is_not_the_warehouse_has_no_client_reason():
    assert warehouse_reason(ValueError("boom")) is None
    assert warehouse_reason(OSError(2, "No such file")) is None


def test_a_long_message_is_capped():
    assert len(describe_error(RuntimeError("x" * 5000))) < 1100
    long = psycopg.errors.InsufficientPrivilege("y" * 5000)
    assert len(warehouse_reason(long)) < 400


def test_the_wrapped_cause_is_what_gets_described():
    try:
        try:
            raise snowflake_refusal()
        except ProgrammingError as exc:
            raise RuntimeError("Error executing tool query_metrics") from exc
    except RuntimeError as wrapper:
        assert describe_error(wrapper).startswith("ProgrammingError 3107")
        assert warehouse_reason(wrapper).endswith("Current session is restricted")


def test_a_model_that_is_not_ready_says_so_and_points_at_issues():
    message = str(ModelNotReady())
    assert "semantic model is not ready" in message
    assert "sql_exec" in message and "vecten issues" in message


def test_no_warehouse_access_names_the_fix():
    message = str(NoWarehouseAccess())
    assert "no warehouse access" in message
    assert "not provisioned" in message and "disabled" in message
    assert "vecten user add" in message


def test_a_connection_failure_gives_the_client_no_reason():
    refused = psycopg.errors.InvalidPassword(
        'connection to server at "10.0.0.5" failed: password authentication failed for "mcp"'
    )
    assert warehouse_reason(refused) is None
    assert warehouse_reason(psycopg.OperationalError("server closed the connection")) is None
    login = ProgrammingError(msg="Incorrect username or password", errno=390100)
    assert warehouse_reason(login) is None


def test_a_state_database_failure_is_not_called_a_warehouse_refusal():
    import sqlalchemy.exc

    try:
        try:
            raise postgres_refusal()
        except psycopg.Error as exc:
            raise sqlalchemy.exc.ProgrammingError("INSERT", {}, exc) from exc
    except sqlalchemy.exc.ProgrammingError as wrapped:
        assert warehouse_reason(wrapped) is None
        assert "42501" in describe_error(wrapped)


def test_a_rejected_server_token_tells_the_client_where_the_cause_is():
    from snowflake.connector.errors import DatabaseError

    rejected = DatabaseError(
        msg="Failed to connect to DB: acct.snowflakecomputing.com:443. Invalid OAuth access token.",
        errno=390303,
    )
    reason = sign_in_reason(rejected)
    assert "did not accept" in reason and "vecten issues" in reason
    # The warehouse's own text names the host, which the caller has no part in.
    assert "snowflakecomputing" not in reason and "acct" not in reason
    assert warehouse_reason(rejected) is None


def test_other_failures_have_no_sign_in_reason():
    login = ProgrammingError(msg="Incorrect username or password", errno=390100)
    assert sign_in_reason(login) is None
    assert sign_in_reason(snowflake_refusal()) is None
    assert sign_in_reason(ValueError("boom")) is None
