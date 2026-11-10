import ssl
import urllib.error
import urllib.request

import pytest

from vecten.deploy.scaleway import clients
from vecten_shared import tls
from vecten_shared.output import Failure


def test_the_control_plane_opener_verifies_with_the_tls_context(monkeypatch):
    sentinel = ssl.create_default_context()
    monkeypatch.setattr(tls, "context", lambda: sentinel)
    handlers = [h for h in clients._opener().handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert [h._context for h in handlers] == [sentinel]


def test_a_certificate_failure_is_named_with_its_remedy(monkeypatch):
    refused = ssl.SSLCertVerificationError(1, "certificate verify failed")

    def fail(request, timeout):
        raise urllib.error.URLError(refused)

    monkeypatch.setattr(clients, "_open", fail)
    with pytest.raises(Failure) as caught:
        clients._http("GET", "https://api.scaleway.com/iam/v1alpha1/api-keys", {}, None)
    assert "TLS certificate check failed" in caught.value.message
    assert "SSL_CERT_FILE" in caught.value.message


def test_another_network_failure_keeps_its_type_only(monkeypatch):
    def fail(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError("refused at 10.0.0.1"))

    monkeypatch.setattr(clients, "_open", fail)
    with pytest.raises(Failure) as caught:
        clients._http("GET", "https://api.scaleway.com/x", {}, None)
    assert caught.value.message == "the Scaleway API is unreachable: URLError"
