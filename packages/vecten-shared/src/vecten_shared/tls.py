"""The trust store for HTTPS calls made through urllib.

A python.org build of Python on macOS reads no CA store until its `Install Certificates.command`
has run, so urllib verifies against certifi's bundle instead; `SSL_CERT_FILE`, set for a proxy
that re-signs HTTPS, names the bundle to use.
"""

from __future__ import annotations

import os
import ssl

CERTIFICATE_HINT = (
    "TLS certificate check failed; if a proxy re-signs HTTPS on this network, point "
    "SSL_CERT_FILE at its CA bundle"
)


def context() -> ssl.SSLContext:
    import certifi

    return ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or certifi.where())


def certificate_failure(exc: BaseException) -> bool:
    """Whether `exc` is a refused server certificate, raised bare or wrapped by urllib."""
    reason = getattr(exc, "reason", None)
    return isinstance(exc, ssl.SSLCertVerificationError) or isinstance(
        reason, ssl.SSLCertVerificationError
    )
