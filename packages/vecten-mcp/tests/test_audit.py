import json
import re
import threading
import time

import pytest

from vecten_mcp.state import StateStore


def test_receipt_ids_are_ulids_and_sql_hash_includes_parameters():
    from vecten_mcp.audit import bound_sql_hash, receipt

    one, two = receipt("abc", now=1000), receipt("abc", now=1000)
    assert re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", one["id"])
    assert one["id"] != two["id"]
    assert one["model"] == "abc" and one["issued_at"] == 1000
    assert one["sql_sha256"] is None
    assert bound_sql_hash("SELECT %s", [1]) != bound_sql_hash("SELECT %s", [2])
    assert bound_sql_hash("SELECT %s", [1]) == bound_sql_hash("SELECT %s", [1])


def test_audit_flush_is_insert_only_and_keeps_required_fields(tmp_path):
    from vecten_mcp.audit import AuditService, receipt

    state = StateStore("sqlite:///" + str(tmp_path / "state.db"))
    audit = AuditService(state, start=False)
    stamp = receipt("abc", now=1000)
    row = {
        "id": stamp["id"],
        "at": 1000,
        "principal_id": "person",
        "tool": "describe",
        "resolution": "compiled",
        "plugin": "vecten-mcp",
    }
    assert audit.enqueue(row)
    assert audit.find(stamp["id"]) is None
    audit.flush()
    assert audit.find(stamp["id"])["principal_id"] == "person"
    assert audit.stats() == {"queued": 0, "written": 1, "dropped": 0}
    assert audit.enqueue(row)
    audit.flush()
    assert audit.stats()["dropped"] == 1
    assert audit.find(stamp["id"])["tool"] == "describe"
    audit.close()
    state.close()


def test_queue_does_not_wait_for_blocked_database_and_counts_overflow(tmp_path):
    from vecten_mcp.audit import AuditService

    state = StateStore("sqlite:///" + str(tmp_path / "state.db"))
    entered, release = threading.Event(), threading.Event()
    audit = AuditService(state, capacity=1, start=False)

    def blocked(row):
        entered.set()
        release.wait(3)
        raise RuntimeError("secret=never-log")

    audit.write = blocked
    audit.start()
    assert audit.enqueue({"id": "first"})
    assert entered.wait(2)
    started = time.monotonic()
    assert audit.enqueue({"id": "second"})
    assert not audit.enqueue({"id": "third"})
    assert time.monotonic() - started < 0.2
    release.set()
    audit.close()
    assert audit.stats()["dropped"] == 3


def test_redaction_hides_denied_comparison_values_but_keeps_columns_and_other_literals():
    from vecten_mcp.audit import redact_row

    row = {
        "sql": "SELECT c.email FROM customers c WHERE c.email='jane@acme.com' AND status='paid'",
        "arguments": {"sql": "email='jane@acme.com'", "limit": 5},
        "question": "Find jane@acme.com",
        "columns": ["c.email", "status"],
    }
    result = redact_row(row, denied=["customers.email"], kind="postgres")
    assert "jane@acme.com" not in json.dumps(result)
    assert "paid" in result["sql"]
    assert result["columns"] == ["c.email", "status"]
    assert result["arguments"]["limit"] == 5
    assert row["question"] == "Find jane@acme.com"


def test_global_redaction_and_refused_query_filter_redaction():
    from vecten_mcp.audit import redact_row

    row = {
        "question": "customer details",
        "arguments": {
            "filters": [
                {"field": "orders.email", "value": "secret-value"},
                {"field": "status", "value": "paid"},
            ]
        },
        "sql": None,
    }
    filtered = redact_row(row, denied=["orders.email"], kind="postgres")
    assert "secret-value" not in json.dumps(filtered)
    assert "paid" in json.dumps(filtered)
    hidden = redact_row(row, denied=[], kind="postgres", redact=True)
    assert hidden["question"] == "[redacted]"
    assert hidden["arguments"] == "[redacted]"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers WHERE email IN ('one','two') AND status='paid'",
        "SELECT * FROM customers WHERE LOWER(email) = 'one' AND status='paid'",
        "SELECT * FROM customers WHERE email BETWEEN 'one' AND 'two' AND status='paid'",
    ],
)
def test_denied_predicate_redaction_covers_lists_functions_and_ranges(sql):
    from vecten_mcp.audit import redact_row

    result = redact_row(
        {"sql": sql, "arguments": {}, "question": None}, denied=["customers.email"], kind="postgres"
    )
    assert "'one'" not in result["sql"] and "'two'" not in result["sql"]
    assert "'paid'" in result["sql"]


def test_refusal_chain_expires_and_has_bounded_capacity():
    from types import SimpleNamespace

    from vecten_mcp.audit import AuditService

    audit = AuditService(None, start=False)
    caller = SimpleNamespace(principal={"id": "p"}, client_id="c", session_id="s", expires_at=150)
    audit.remember_refusal(caller, "refusal", now=100)
    assert audit.chain(caller, "sql-exec", "new", now=150) == "new"
    caller.expires_at = 10000
    audit.remember_refusal(caller, "old", now=100)
    assert audit.chain(caller, "sql-exec", "fresh", now=401) == "fresh"
    for index in range(1025):
        caller.session_id = str(index)
        audit.remember_refusal(caller, str(index), now=500)
    caller.session_id = "0"
    assert audit.chain(caller, "sql-exec", "evicted", now=501) == "evicted"
    caller.session_id = "1024"
    assert audit.chain(caller, "sql-exec", "new", now=501) == "1024"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM customers WHERE 'secret' = email AND status='paid'",
        (
            "SELECT email FROM customers WHERE email='secret'; "
            "SELECT email FROM customers WHERE email='other-secret'"
        ),
        "SELECT email FROM customers WHERE email='o''brien@example.com' AND status='paid'",
    ],
)
def test_redaction_covers_all_statements_and_escaped_sql_arguments(sql):
    from vecten_mcp.audit import redact_row

    result = redact_row(
        {"sql": sql, "arguments": {"nested": {"query": sql}}, "question": None},
        denied=["customers.email"],
        kind="postgres",
    )
    assert "secret" not in json.dumps(result)
    assert "example.com" not in json.dumps(result)
    if "paid" in sql:
        assert "paid" in result["sql"]


def test_redaction_follows_derived_column_alias():
    from vecten_mcp.audit import redact_row

    row = redact_row(
        {
            "sql": (
                "WITH c AS (SELECT email AS contact FROM customers) "
                "SELECT contact FROM c WHERE contact='secret' AND status='paid'"
            ),
            "arguments": {},
        },
        denied=["customers.email"],
        kind="postgres",
    )
    assert "secret" not in row["sql"] and "paid" in row["sql"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM customers WHERE email=E'secret' AND status='paid'",
        "SELECT email FROM customers WHERE email=$$secret$$ AND status='paid'",
        "WITH c(contact) AS (SELECT email FROM customers) "
        "SELECT contact FROM c WHERE contact='secret' AND status='paid'",
        "SELECT contact FROM (SELECT email FROM customers) AS c(contact) "
        "WHERE contact='secret' AND status='paid'",
        "EXPLAIN SELECT email FROM customers WHERE email='secret' AND status='paid'",
        "SELECT email FROM customers WHERE email='secret' AND status='paid' /* secret */",
    ],
)
def test_review_denied_value_redaction_variants(sql, caplog):
    from vecten_mcp.audit import redact_row

    row = redact_row(
        {"sql": sql, "arguments": {"sql": sql}, "question": "Find secret"},
        denied=["customers.email"],
        kind="postgres",
    )
    assert "secret" not in json.dumps(row)
    assert "secret" not in caplog.text
    assert "paid" in row["sql"]


@pytest.mark.timeout(10)
def test_reverse_alias_chain_redacts_within_bounded_time():
    from vecten_mcp.audit import redact_row

    sql = "SELECT " + ",".join(f"a{i + 1} AS a{i}" for i in range(3999))
    sql += ",ssn AS a3999 FROM t WHERE a0='private-value' AND status='paid'"
    assert len(sql.encode()) < 64000
    started = time.monotonic()
    row = redact_row(
        {"sql": sql, "arguments": {}, "question": "Find private-value"},
        denied={"ssn"},
        kind="postgres",
    )
    assert time.monotonic() - started < 4
    assert "private-value" not in json.dumps(row)
    assert "paid" in row["sql"]


@pytest.mark.parametrize(
    "kind, expression",
    [
        ("postgres", "starts_with(ssn, 'private-value')"),
        ("snowflake", "contains(ssn, 'private-value')"),
        ("postgres", "CASE ssn WHEN 'private-value' THEN 1 ELSE 0 END = 1"),
        ("postgres", "CASE ssn WHEN 'private-value' THEN 1 ELSE 0 END"),
        ("postgres", "starts_with(lower(ssn), 'private-value')"),
        ("postgres", "lower(ssn) = 'private-value'"),
    ],
)
def test_function_and_case_comparisons_redact_values(kind, expression):
    from vecten_mcp.audit import redact_row

    sql = f"SELECT {expression} FROM t WHERE status='paid'"
    row = redact_row(
        {"sql": sql, "arguments": {"sql": sql}, "question": "Find private-value"},
        denied={"ssn"},
        kind=kind,
    )
    assert "private-value" not in json.dumps(row)
    assert "paid" in row["sql"]
