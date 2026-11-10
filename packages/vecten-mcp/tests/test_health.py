from starlette.testclient import TestClient

from vecten_mcp import __version__
from vecten_mcp.app import create_app


def test_health_route():
    client = TestClient(create_app())
    r = client.get("/health")
    assert (
        r.status_code == 200 and r.json()["status"] == "ok" and r.json()["version"] == __version__
    )


def test_unknown_route_is_404():
    assert TestClient(create_app()).get("/nope").status_code == 404
