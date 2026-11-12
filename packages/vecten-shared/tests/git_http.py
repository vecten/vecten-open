"""A git smart-HTTP server over TLS with basic authentication, for tests of git sources.

`git http-backend` serves the repositories under one root. A repository named in `public`
answers anyone; every other path, including one that does not exist, asks for credentials
first, as GitHub does, so a missing repository and a private one look alike to a client
without a token. A repository named in `version_0` answers in protocol version 0, where git
refuses to fetch a commit no ref advertises.
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import os
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SERVICE_SUFFIXES = ("/info/refs", "/git-upload-pack", "/git-receive-pack", "/HEAD")


def _certificate(directory: Path) -> tuple[Path, Path, Path]:
    """A CA and a leaf for 127.0.0.1 it signs: (ca, leaf chain, leaf key) paths."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "git-sources test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    ca_path, chain_path, key_path = (
        directory / "ca.pem",
        directory / "leaf.pem",
        directory / "key.pem",
    )
    ca_path.write_bytes(ca.public_bytes(pem))
    chain_path.write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    key_path.write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return ca_path, chain_path, key_path


def _repository(path_info: str) -> str:
    for suffix in SERVICE_SUFFIXES:
        if suffix in path_info:
            return path_info.split(suffix, 1)[0].strip("/")
    return path_info.strip("/")


@dataclass
class GitServer:
    """Serves `root` at `url`; `users` maps a username to its token."""

    root: Path
    users: dict[str, str]
    public: set[str] = field(default_factory=set)
    version_0: set[str] = field(default_factory=set)
    url: str = ""
    ca_path: Path | None = None
    authorizations: list[str | None] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None

    def start(self, workdir: Path) -> GitServer:
        workdir.mkdir(parents=True, exist_ok=True)
        self.ca_path, chain, key = _certificate(workdir)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain, key)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.daemon_threads = True
        self._server = server
        self.url = f"https://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def _authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, token = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        return self.users.get(user) == token

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - the base class names it
                pass

            def do_GET(self):  # noqa: N802 - the base class dispatches by this name
                self._serve()

            def do_POST(self):  # noqa: N802 - the base class dispatches by this name
                self._serve()

            def _body(self) -> bytes:
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    chunks = []
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            return b"".join(chunks)
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _serve(self):
                path_info, _, query = self.path.partition("?")
                repository = _repository(path_info)
                header = self.headers.get("Authorization")
                server.authorizations.append(header)
                if repository not in server.public and not server._authorized(header):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self._body() if self.command == "POST" else b""
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_PROJECT_ROOT": str(server.root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": path_info,
                    "QUERY_STRING": query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "REMOTE_ADDR": "127.0.0.1",
                    "REMOTE_USER": "git",
                }
                if self.headers.get("Git-Protocol") and repository not in server.version_0:
                    env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
                if self.headers.get("Content-Encoding"):
                    env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
                result = subprocess.run(
                    ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
                )
                # http-backend ends its CGI header block with CRLF CRLF before the payload.
                head, _, payload = result.stdout.partition(b"\r\n\r\n")
                status, headers = 200, []
                for line in head.decode("latin-1").splitlines():
                    name, _, value = line.partition(":")
                    if name.lower() == "status":
                        status = int(value.split()[0])
                    elif name:
                        headers.append((name, value.strip()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler
