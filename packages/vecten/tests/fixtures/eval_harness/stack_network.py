"""The existing warehouse on the local stack's network.

The warehouse container publishes its port on the host's 127.0.0.1 only, and inside the stack's
containers `warehouse.acme.test` resolves to nothing the stack can reach. Joining it to each
`vecten-` Compose project's default network under that alias gives the stack's MCP the same
name the locator holds. `vecten up` creates the network and `vecten down` removes it, so the
join repeats while the harness serves.
"""

from __future__ import annotations

import subprocess
import sys
import threading

STACK_PREFIX = "vecten-"
LISTING_FORMAT = '{{.Name}}\t{{.Label "com.docker.compose.project"}}'


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def networks_to_join(listing: str, joined: set[str]) -> list[str]:
    """The stack networks in a `docker network ls` listing the warehouse is not on yet."""
    found = []
    for line in listing.splitlines():
        name, _, project = line.partition("\t")
        if project.startswith(STACK_PREFIX) and name not in joined:
            found.append(name)
    return found


def joined(container: str) -> set[str]:
    result = _docker(
        "inspect",
        "-f",
        "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}\n{{end}}",
        container,
    )
    return {line for line in result.stdout.splitlines() if line}


def join_once(container: str, alias: str) -> list[str]:
    """Join every stack network the container is not on; the networks joined."""
    listing = _docker(
        "network",
        "ls",
        "--filter",
        "label=com.docker.compose.network=default",
        "--format",
        LISTING_FORMAT,
    )
    done = []
    for network in networks_to_join(listing.stdout, joined(container)):
        result = _docker("network", "connect", "--alias", alias, network, container)
        if result.returncode == 0:
            done.append(network)
        else:
            print(
                f"could not join {container} to {network}: {result.stderr.strip()}", file=sys.stderr
            )
    return done


class Joiner:
    """Repeats `join_once` until stopped."""

    def __init__(self, container: str, alias: str, interval: float = 2.0):
        self.container, self.alias, self.interval = container, alias, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            for network in join_once(self.container, self.alias):
                print(f"joined the warehouse to {network} as {self.alias}", flush=True)
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
