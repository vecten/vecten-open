import ssl
import urllib.error
import urllib.request

import certifi
import pytest

from vecten_shared import tls
from vecten_shared.output import Failure
from vecten_shared.provider import scaleway as provider


def _captured(monkeypatch):
    seen = {}
    real = ssl.create_default_context

    def create(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", create)
    return seen


def test_the_context_trusts_certifis_bundle(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    seen = _captured(monkeypatch)
    context = tls.context()
    assert seen["cafile"] == certifi.where()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_ssl_cert_file_overrides_the_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "proxy-ca.pem"
    bundle.symlink_to(certifi.where())
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    seen = _captured(monkeypatch)
    tls.context()
    assert seen["cafile"] == str(bundle)


def test_a_certificate_failure_is_recognised_through_urlerror():
    refused = ssl.SSLCertVerificationError(1, "certificate verify failed")
    assert tls.certificate_failure(urllib.error.URLError(refused))
    assert tls.certificate_failure(refused)
    assert not tls.certificate_failure(urllib.error.URLError(OSError("connection refused")))


def test_the_provider_opener_verifies_with_the_tls_context(monkeypatch):
    sentinel = ssl.create_default_context()
    monkeypatch.setattr(tls, "context", lambda: sentinel)
    opener = provider._opener()
    handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert [h._context for h in handlers] == [sentinel]


def test_the_provider_names_a_certificate_failure(monkeypatch):
    refused = ssl.SSLCertVerificationError(1, "certificate verify failed")

    def fail(request, timeout):
        raise urllib.error.URLError(refused)

    monkeypatch.setattr(provider, "_open", fail)
    with pytest.raises(Failure) as caught:
        provider._http("GET", "https://api.scaleway.com/secret-manager/x?token=t", {}, None)
    assert "TLS certificate check failed" in caught.value.message
    assert "SSL_CERT_FILE" in caught.value.message
    assert "token" not in caught.value.message
