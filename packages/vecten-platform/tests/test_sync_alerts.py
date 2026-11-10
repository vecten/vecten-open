from vecten_platform.sync.airflow_dag import ObjectSyncError
from vecten_platform.sync.alerts import alert_on_failure, alert_payload


class FakeTask:
    def __init__(self, task_id):
        self.task_id = task_id


class FakeTI:
    try_number = 2
    map_index = 1


def context(exc=None, task_id="sync"):
    return {
        "dag": type("D", (), {"dag_id": "sync_hubspot_default"})(),
        "task": FakeTask(task_id),
        "task_instance": FakeTI(),
        "run_id": "manual__1",
        "exception": exc,
    }


def test_a_mapped_sync_failure_is_named_by_object_and_wrapped_class():
    exc = ObjectSyncError("hubspot", "deals", "KeyError: 'dealname'")
    payload = alert_payload(context(exc))
    assert payload["dag_id"] == "sync_hubspot_default"
    assert payload["task_id"] == "sync" and payload["map_index"] == 1 and payload["try_number"] == 2
    assert payload["error_class"] == "KeyError"
    assert payload["error"] == "hubspot.deals: KeyError: 'dealname'"
    assert payload["issue_id"] == "iss_airflow_sync_hubspot_default_sync.deals_KeyError"
    assert payload["message"] == (
        "ask your agent: `vecten issues show iss_airflow_sync_hubspot_default_sync.deals_KeyError`"
    )


def test_the_text_a_chat_webhook_shows_names_dag_task_issue_and_command():
    exc = ObjectSyncError("hubspot", "deals", "KeyError: 'dealname'")
    text = alert_payload(context(exc))["text"]
    assert text == (
        "sync_hubspot_default.sync failed for deals with KeyError (run manual__1), issue "
        "iss_airflow_sync_hubspot_default_sync.deals_KeyError; ask your agent: "
        "`vecten issues show iss_airflow_sync_hubspot_default_sync.deals_KeyError`"
    )


def test_another_task_is_named_by_task_and_exception_class():
    payload = alert_payload(context(RuntimeError("boom"), task_id="discover"))
    assert payload["error_class"] == "RuntimeError" and payload["error"] == "boom"
    assert payload["issue_id"] == "iss_airflow_sync_hubspot_default_discover_RuntimeError"


def test_no_webhook_posts_nothing(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    calls = []
    alert_on_failure(context(RuntimeError("x")), post=lambda url, json: calls.append((url, json)))
    assert calls == []


def test_webhook_receives_the_payload(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    calls = []
    alert_on_failure(context(RuntimeError("x")), post=lambda url, json: calls.append((url, json)))
    assert calls[0][0] == "https://hooks.example/abc"
    assert calls[0][1]["error"] == "x"


def test_webhook_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")

    def boom(url, json):
        raise OSError("unreachable")

    alert_on_failure(context(RuntimeError("x")), post=boom)
    assert "unreachable" in caplog.text


def test_error_redacts_query_strings_secrets_and_bearer_tokens():
    message = (
        "GET https://api.example.com/v1/items?api_key=abc123 returned 401, "
        "Authorization: Bearer xyz"
    )
    error = alert_payload(context(RuntimeError(message)))["error"]
    assert "abc123" not in error and "xyz" not in error
    assert "https://api.example.com/v1/items" in error
    assert "Bearer [redacted]" in error


def test_error_redacts_key_value_pairs():
    error = alert_payload(context(RuntimeError("login failed password=hunter2 token: t0k")))[
        "error"
    ]
    assert "hunter2" not in error and "t0k" not in error


def test_error_is_capped_at_200_characters():
    assert len(alert_payload(context(RuntimeError("x" * 500)))["error"]) == 200


def test_error_keeps_only_the_first_line():
    assert alert_payload(context(RuntimeError("first\nsecond")))["error"] == "first"


def test_error_redacts_json_quoted_pairs():
    error = alert_payload(context(RuntimeError('body {"access_token": "abc4", "ok": 1}')))["error"]
    assert "abc4" not in error and '"ok": 1' in error


def test_error_redacts_the_credential_after_an_auth_scheme():
    error = alert_payload(context(RuntimeError("Authorization: Basic abc5== rejected")))["error"]
    assert "abc5" not in error
    assert "Authorization: Basic [redacted] rejected" in error


def test_error_redacts_url_userinfo():
    error = alert_payload(context(RuntimeError("cannot reach postgresql://loader:abc6@db:5432/wh")))
    assert error["error"] == "cannot reach postgresql://[redacted]@db:5432/wh"


def test_error_redacts_userinfo_whose_password_holds_an_at_sign():
    error = alert_payload(context(RuntimeError("cannot reach postgresql://loader:p@ss@db:5432/wh")))
    assert error["error"] == "cannot reach postgresql://[redacted]@db:5432/wh"


def test_webhook_failure_log_names_neither_the_webhook_url_nor_secrets(monkeypatch, caplog):
    url = "https://hooks.example/services/T0/B0/s3cr3t"
    monkeypatch.setenv("ALERT_WEBHOOK_URL", url)

    def boom(url, json):
        raise OSError(f"POST {url} failed token=abc7")

    alert_on_failure(context(RuntimeError("x")), post=boom)
    assert "OSError" in caplog.text
    assert "s3cr3t" not in caplog.text and "abc7" not in caplog.text
