"""Exercise the deployed Analytics probe against a real HTTPS socket."""

import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import certifi

from vecten.stack.certs import ensure_self_signed
from vecten_mcp.apps import _probe_app


def test_deployed_health_probe_checks_https_and_hostname(tmp_path, monkeypatch):
    requests = []
    status = [200]

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(status[0])
            self.end_headers()

    certs = ensure_self_signed(tmp_path, ["localhost"])
    certificate = certs / "localhost.crt"
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, certs / "localhost.key")
    with HTTPServer(("127.0.0.1", 0), Health) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            url = f"https://localhost:{server.server_port}/"
            assert _probe_app(url, local=False) == "unreachable"
            monkeypatch.setattr(certifi, "where", lambda: str(certificate))
            assert _probe_app(url, local=False) == "healthy"
            status[0] = 503
            assert _probe_app(url, local=False) == "unhealthy (HTTP 503)"
            assert _probe_app(url.replace("localhost", "127.0.0.1"), local=False) == "unreachable"
            assert requests == ["/_vecten/health", "/_vecten/health"]
        finally:
            server.shutdown()
            worker.join()
