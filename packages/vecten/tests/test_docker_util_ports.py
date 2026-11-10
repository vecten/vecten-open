"""The test containers' ports never take the laptop stack's fixed Postgres port."""

import socket

from vecten_shared.provider.local import LOCAL_PG_PUBLISHED_PORT

from . import docker_util


class Kernel:
    """`socket.socket` whose port-0 binds hand out `ports` in turn."""

    def __init__(self, ports):
        self.ports = list(ports)

    def __call__(self, *args, **kwargs):
        kernel = self

        class Bound:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def bind(self, address):
                self.port = kernel.ports.pop(0)

            def getsockname(self):
                return ("127.0.0.1", self.port)

        return Bound()


def test_free_port_skips_the_stack_s_postgres_port(monkeypatch):
    monkeypatch.setattr(socket, "socket", Kernel([LOCAL_PG_PUBLISHED_PORT, 41234]))
    assert docker_util.free_port() == 41234
