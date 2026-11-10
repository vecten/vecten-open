"""The Airflow REST client `vecten issues` reads task instances and logs through."""

from datetime import UTC, datetime

import httpx
import pytest

from vecten.issues import airflow
from vecten.issues.airflow import AirflowClient
from vecten.issues.context import SourceUnavailable

SINCE = datetime(2026, 10, 7, tzinfo=UTC)


def ti(i, state="failed"):
    return {
        "dag_id": "sync_hubspot_default",
        "task_id": "sync",
        "dag_run_id": f"scheduled__{i}",
        "map_index": 0,
        "rendered_map_index": "deals",
        "try_number": 3,
        "state": state,
        "run_after": "2026-10-20T02:00:00Z",
    }


@pytest.fixture
def requests(monkeypatch):
    seen = []
    rows = [ti(i) for i in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(201, json={"access_token": "jwt-1"})
        if request.url.path == "/api/v2/dags/~/dagRuns/~/taskInstances":
            offset = int(request.url.params["offset"])
            page = rows[offset : offset + int(request.url.params["limit"])]
            return httpx.Response(200, json={"task_instances": page, "total_entries": len(rows)})
        if "/logs/" in request.url.path:
            content = [{"event": "boom", "level": "error"}, "plain line"]
            return httpx.Response(200, json={"content": content})
        return httpx.Response(404, json={})

    monkeypatch.setattr(airflow, "TRANSPORT", httpx.MockTransport(handler))
    return seen


def test_the_laptop_client_sends_a_bearer_token_and_pages_through_every_row(requests):
    client = AirflowClient("http://127.0.0.1:8482", username="admin", password="pw")
    attempts = client.task_instances(SINCE)
    assert len(attempts) == 150
    assert attempts[0].rendered_map_index == "deals" and attempts[0].try_number == 3
    login, first, second = requests
    assert login.method == "POST" and b'"password": "pw"' in login.content.replace(b'":"', b'": "')
    assert first.headers["Authorization"] == "Bearer jwt-1"
    assert first.url.params["run_after_gte"] == SINCE.isoformat()
    assert first.url.params.get_list("state") == ["success", "failed", "skipped"]
    assert second.url.params["offset"] == "100"


def test_a_deployed_airflow_takes_the_token_as_bearer_through_nginx(requests):
    client = AirflowClient("https://airflow.data.acme.com", username="admin", password="pw")
    client.task_instances(SINCE)
    assert requests[1].headers["Authorization"] == "Bearer jwt-1"
    assert "Cookie" not in requests[1].headers


def test_a_log_is_its_content_lines(requests):
    client = AirflowClient("http://127.0.0.1:8482", username="admin", password="pw")
    (attempt, *_) = client.task_instances(SINCE)
    text = client.log(attempt)
    assert text.splitlines() == ['{"event": "boom", "level": "error"}', "plain line"]
    request = requests[-1]
    assert request.url.path == (
        "/api/v2/dags/sync_hubspot_default/dagRuns/scheduled__0/taskInstances/sync/logs/3"
    )
    assert request.url.params["map_index"] == "0"


def test_a_refused_login_is_an_unavailable_source(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    monkeypatch.setattr(airflow, "TRANSPORT", transport)
    client = AirflowClient("http://127.0.0.1:8482", username="admin", password="wrong")
    with pytest.raises(SourceUnavailable, match="refused the login"):
        client.task_instances(SINCE)


def test_an_unreachable_airflow_is_an_unavailable_source(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(airflow, "TRANSPORT", httpx.MockTransport(refuse))
    client = AirflowClient("http://127.0.0.1:8482", username="admin", password="pw")
    with pytest.raises(SourceUnavailable, match="not reachable"):
        client.task_instances(SINCE)


def test_every_page_is_read_up_to_the_total(monkeypatch):
    rows = [ti(i, "success") for i in range(5001)]

    def handler(request):
        if request.url.path == "/auth/token":
            return httpx.Response(201, json={"access_token": "jwt-1"})
        offset = int(request.url.params["offset"])
        page = rows[offset : offset + int(request.url.params["limit"])]
        return httpx.Response(200, json={"task_instances": page, "total_entries": len(rows)})

    monkeypatch.setattr(airflow, "TRANSPORT", httpx.MockTransport(handler))
    client = AirflowClient("http://127.0.0.1:8482", username="admin", password="pw")
    assert len(client.task_instances(SINCE)) == 5001
    monkeypatch.setattr(airflow, "MAX_ROWS", 4000)
    with pytest.raises(SourceUnavailable, match="5001 task instances"):
        AirflowClient("http://127.0.0.1:8482", username="admin", password="pw").task_instances(
            SINCE
        )
