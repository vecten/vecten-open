import asyncio
import json
import socket

import httpx
import pytest

from vecten_mcp.auth.client_metadata import (
    MAX_BYTES,
    ClientMetadata,
    _lifetime,
    _metadata_url,
    _validate,
)
from vecten_mcp.auth.limits import RateLimiter

CID = "https://client.example/mcp.json"
DOC = {"client_name": "Client", "client_id": CID, "redirect_uris": ["https://client.example/back"]}


@pytest.mark.parametrize(
    "value",
    [
        "https://client.example",
        "https://user@client.example/mcp",
        CID + "#",
        CID + "/../y",
        "http://client.example/mcp",
    ],
)
def test_document_id_is_an_absolute_https_document_url(value):
    with pytest.raises(ValueError):
        _metadata_url(value)


@pytest.mark.parametrize(
    "change",
    [
        {"client_id": "https://other.example/mcp.json"},
        {"client_name": ""},
        {"redirect_uris": ["http://example.com/back"]},
        {"redirect_uris": [12]},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"client_secret": "unused"},
        {"grant_types": ["client_credentials"]},
    ],
)
def test_invalid_document_is_refused(change):
    with pytest.raises(ValueError):
        _validate(json.dumps(DOC | change), CID)


def test_grants_the_server_does_not_offer_are_ignored():
    # Published documents list grants for other servers too, such as the JWT bearer grant.
    listed = ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"]

    doc = _validate(json.dumps(DOC | {"grant_types": listed}), CID)

    assert doc["grant_types"] == ["authorization_code", "refresh_token"]


@pytest.mark.parametrize(
    ("directive", "ttl"),
    [("max-age=18", 16), ("max-age=99999", 298), ("no-store", 0), ("no-cache", 0)],
)
def test_cache_lifetime(directive, ttl):
    assert _lifetime({"cache-control": directive, "age": "2"}) == ttl


def test_resolver_requires_every_answer_to_be_public(monkeypatch):
    async def check():
        async def answer(*args, **kwargs):
            return [(0, 0, 0, "", (ip, 443)) for ip in ("8.8.8.8", "127.0.0.1")]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", answer)
        with pytest.raises(ValueError):
            await ClientMetadata().resolve(httpx.URL(CID))

    asyncio.run(check())


@pytest.mark.parametrize(
    ("status", "data", "accepted"),
    [
        (200, json.dumps(DOC).encode(), True),
        (302, b"", False),
        (200, b" " * (MAX_BYTES + 1), False),
    ],
)
def test_checked_ip_receives_one_bounded_request_with_original_tls_name(
    monkeypatch, status, data, accepted
):
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"content-type": "application/json", "cache-control": "max-age=5"},
            stream=httpx.ByteStream(data),
        )

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=transport, **kw))

    async def check():
        async def answer(host, port, **kwargs):
            assert host == "client.example" and kwargs["type"] == socket.SOCK_STREAM
            return [(0, 0, 0, "", ("8.8.8.8", port))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", answer)
        now = [10]
        metadata = ClientMetadata(clock=lambda: now[0])
        limit = RateLimiter()
        if accepted:
            assert (await metadata.get(CID, limit, "peer"))["client_name"] == "Client"
            assert (await metadata.get(CID, limit, "peer"))["client_name"] == "Client"
            assert len(calls) == 1
            now[0] += 6
            await metadata.get(CID, limit, "peer")
            assert len(calls) == 2
        else:
            with pytest.raises(ValueError):
                await metadata.get(CID, limit, "peer")
            assert len(calls) == 1
        req = calls[0]
        assert req.url.host == "8.8.8.8"
        assert req.headers["host"] == req.extensions["sni_hostname"] == "client.example"
        assert "authorization" not in req.headers and "cookie" not in req.headers

    asyncio.run(check())


@pytest.mark.parametrize(
    ("registered", "requested", "expected"),
    [
        ("http://127.0.0.1/cb", "http://127.0.0.1:53124/cb", True),
        ("http://[::1]:8888/cb?x=1", "http://[::1]:53124/cb?x=1", True),
        ("http://[::1]/cb", "http://127.0.0.1:53124/cb", False),
        ("http://127.0.0.1/cb", "http://127.0.0.1:53124/other", False),
        ("http://127.0.0.1/cb?x=1", "http://127.0.0.1:53124/cb?x=2", False),
        ("https://127.0.0.1/cb", "https://127.0.0.1:53124/cb", False),
        ("http://localhost/cb", "http://localhost:53124/cb", False),
    ],
)
def test_metadata_redirect_port_exception(registered, requested, expected):
    from vecten_mcp.auth.client_metadata import metadata_redirect_matches

    assert metadata_redirect_matches(requested, [registered]) is expected
