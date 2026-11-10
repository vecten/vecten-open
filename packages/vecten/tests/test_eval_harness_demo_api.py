"""The evaluation harness's demo API stub, against `connector/evals/files/demo-api.md`."""

import base64
import json
import socket
import ssl
import sys
import threading
from http.client import HTTPSConnection
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import ca as ca_mod  # noqa: E402
from eval_harness import demo_api  # noqa: E402
from eval_harness.front import Front  # noqa: E402

TOKEN = demo_api.READ_TOKEN
BEARER = {"authorization": f"Bearer {TOKEN}"}


def call(api, path, query="", headers=BEARER, method="GET"):
    status, response_headers, body = api.handle(method, path, query, headers)
    return status, response_headers, json.loads(body) if body else None


def every_page(api, path, query=""):
    ids, cursor, pages = [], None, 0
    while True:
        params = query + (f"&cursor={cursor}" if cursor else "")
        status, _, body = call(api, path, params.lstrip("&"))
        assert status == 200, body
        ids += [row["id"] for row in body["data"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            return ids, pages


@pytest.fixture
def api():
    return demo_api.DemoApi()


def test_a_missing_or_unknown_token_is_401(api):
    assert call(api, "/v1/companies", headers={})[0] == 401
    assert call(api, "/v1/companies", headers={"authorization": "Bearer nope"})[0] == 401
    assert call(api, "/v1/companies", headers={"authorization": TOKEN})[0] == 401


def test_a_token_without_the_read_scope_is_403(api):
    headers = {"authorization": f"Bearer {demo_api.NO_SCOPE_TOKEN}"}
    assert call(api, "/v1/deals", headers=headers)[0] == 403


def test_a_list_endpoint_pages_50_records_by_default_with_a_cursor(api):
    status, _, body = call(api, "/v1/companies")
    assert status == 200
    assert [row["id"] for row in body["data"]] == list(range(1, 51))
    padded = body["next_cursor"] + "=" * (-len(body["next_cursor"]) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == {"id": 50}


def test_following_the_cursor_returns_every_record_once_and_ends_on_null(api):
    companies, pages = every_page(api, "/v1/companies")
    assert companies == sorted(set(companies)) and len(companies) == len(api.companies)
    assert pages == -(-len(api.companies) // 50)
    deals, _ = every_page(api, "/v1/deals", "limit=200")
    assert deals == [deal["id"] for deal in api.deals]


def test_limit_is_capped_at_200(api):
    assert len(call(api, "/v1/deals", "limit=200")[2]["data"]) == 200
    assert call(api, "/v1/deals", "limit=201")[0] == 400
    assert call(api, "/v1/deals", "limit=0")[0] == 400
    assert call(api, "/v1/deals", "limit=ten")[0] == 400


def test_a_malformed_cursor_is_400(api):
    assert call(api, "/v1/deals", "cursor=%%%")[0] == 400


@pytest.mark.parametrize("path", ["/v1/companies", "/v1/deals"])
def test_updated_since_keeps_records_updated_at_or_after_it(api, path):
    rows = api.companies if path.endswith("companies") else api.deals
    pivot = sorted(row["updated_at"] for row in rows)[len(rows) // 2]
    expected = sorted(row["id"] for row in rows if row["updated_at"] >= pivot)
    ids, _ = every_page(api, path, f"updated_since={pivot}")
    assert ids == expected and 0 < len(ids) < len(rows)
    offset = pivot.replace("Z", "+00:00").replace("+", "%2B")
    assert every_page(api, path, f"updated_since={offset}")[0] == expected


def test_updated_since_from_the_epoch_returns_everything(api):
    ids, _ = every_page(api, "/v1/companies", "updated_since=1970-01-01T00:00:00Z")
    assert len(ids) == len(api.companies)


def test_a_malformed_updated_since_is_400(api):
    assert call(api, "/v1/companies", "updated_since=yesterday")[0] == 400


def test_past_the_rate_limit_the_api_answers_429_with_retry_after():
    api = demo_api.DemoApi(rate_limit=3, clock=lambda: 1000.0)
    assert [call(api, "/v1/companies")[0] for _ in range(3)] == [200, 200, 200]
    status, headers, _ = call(api, "/v1/companies")
    assert status == 429 and int(headers["retry-after"]) > 0


def test_unknown_paths_and_methods_are_refused(api):
    assert call(api, "/v1/contacts")[0] == 404
    assert call(api, "/v1/companies", method="POST")[0] == 405


def test_the_records_follow_the_documented_fields(api):
    assert set(api.companies[0]) == {
        "id",
        "name",
        "domain",
        "email",
        "country",
        "created_at",
        "updated_at",
    }
    assert set(api.deals[0]) == {
        "id",
        "company_id",
        "title",
        "stage",
        "amount",
        "created_at",
        "close_date",
        "is_won",
        "updated_at",
    }
    companies = {c["id"] for c in api.companies}
    for deal in api.deals:
        assert deal["company_id"] in companies
        assert deal["stage"] in ("open", "won", "lost")
        assert (deal["close_date"] is None) == (deal["stage"] == "open")
        assert deal["is_won"] == (deal["stage"] == "won")
    assert any(c["email"] == "" for c in api.companies)
    assert any("://" in c["domain"] for c in api.companies)


def test_the_data_is_the_same_on_every_start():
    assert demo_api.DemoApi().companies == demo_api.DemoApi().companies
    assert demo_api.DemoApi().deals == demo_api.DemoApi().deals


def test_the_bundle_adds_the_harness_ca_to_the_public_roots(tmp_path):
    authority = ca_mod.Authority.create(tmp_path)
    bundle = authority.bundle(tmp_path / "bundle.pem")
    text = bundle.read_text()
    assert authority.cert_path.read_text().strip() in text
    assert text.count("BEGIN CERTIFICATE") > 100


def test_the_authority_is_reused_from_its_directory(tmp_path):
    first = ca_mod.Authority.create(tmp_path)
    again = ca_mod.Authority.create(tmp_path)
    assert first.cert_path.read_bytes() == again.cert_path.read_bytes()


def test_the_authority_vouches_only_for_the_evaluation_names(tmp_path):
    """`up` suggests trusting this root system-wide, so its key must be useless for any name
    outside the evaluation's."""
    import ipaddress

    from cryptography import x509

    authority = ca_mod.Authority.create(tmp_path)
    cert = x509.load_pem_x509_certificate(authority.cert_path.read_bytes())
    constraints = cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    dns = {s.value for s in constraints.value.permitted_subtrees if isinstance(s, x509.DNSName)}
    assert dns == {"acme.test", "demo.test", "localhost"}
    ips = {s.value for s in constraints.value.permitted_subtrees if isinstance(s, x509.IPAddress)}
    assert ips == {ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")}


def test_an_unconstrained_authority_from_before_is_replaced(tmp_path):
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    authority = ca_mod.Authority.create(tmp_path)
    key = serialization.load_pem_private_key(authority.key_path.read_bytes(), password=None)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, ca_mod.NAME)])
    now = dt.datetime.now(dt.UTC)
    unconstrained = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    authority.cert_path.write_bytes(unconstrained.public_bytes(serialization.Encoding.PEM))
    again = ca_mod.Authority.create(tmp_path)
    cert = x509.load_pem_x509_certificate(again.cert_path.read_bytes())
    assert cert.extensions.get_extension_for_class(x509.NameConstraints).critical


@pytest.fixture
def served(tmp_path):
    authority = ca_mod.Authority.create(tmp_path / "ca")
    cert, key = authority.issue(["api.demo.test", "localhost"], tmp_path / "leaf")
    front = Front()
    front.mount("api.demo.test", demo_api.app(demo_api.DemoApi()))
    front.mount("localhost", demo_api.app(demo_api.DemoApi()))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = front.server("127.0.0.1", port, cert, key)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.05)
    yield authority, port
    server.should_exit = True
    thread.join(10)


def test_the_stub_answers_over_tls_under_its_name(served):
    authority, port = served
    context = ssl.create_default_context(cafile=str(authority.cert_path))
    connection = HTTPSConnection("127.0.0.1", port, context=context)
    connection.sock = context.wrap_socket(
        socket.create_connection(("127.0.0.1", port)), server_hostname="api.demo.test"
    )
    try:
        connection.request(
            "GET", "/v1/companies?limit=2", headers={**BEARER, "Host": "api.demo.test"}
        )
        response = connection.getresponse()
        assert response.status == 200
        assert [row["id"] for row in json.loads(response.read())["data"]] == [1, 2]
    finally:
        connection.close()


def test_the_rest_scaffold_reads_every_record_through_the_ca_bundle(served, monkeypatch, tmp_path):
    """The `connector` skill's dlt rest_api config, trusted through REQUESTS_CA_BUNDLE as the
    harness prints it for the shell `vecten connector run` executes in."""
    authority, port = served
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(authority.bundle(tmp_path / "bundle.pem")))
    from dlt.sources.rest_api import rest_api_source

    incremental = {
        "type": "incremental",
        "cursor_path": "updated_at",
        "initial_value": "1970-01-01T00:00:00Z",
    }
    source = rest_api_source(
        {
            "client": {
                "base_url": f"https://localhost:{port}/v1/",
                "auth": {"type": "bearer", "token": TOKEN},
                "paginator": {
                    "type": "cursor",
                    "cursor_path": "next_cursor",
                    "cursor_param": "cursor",
                },
            },
            "resource_defaults": {"primary_key": "id", "write_disposition": "merge"},
            "resources": [
                {"name": name, "endpoint": {"path": name, "params": {"updated_since": incremental}}}
                for name in ("companies", "deals")
            ],
        },
        name="demo",
    )
    api = demo_api.DemoApi()
    assert len(list(source.resources["companies"])) == len(api.companies)
    assert len(list(source.resources["deals"])) == len(api.deals)
