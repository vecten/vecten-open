"""The MCP the mocked deploy serves at `https://mcp.<domain>`, for the extended guide cases.

It waits for the mocked cloud's MCP host to hold a release, then does what the deployed hosts
would: on a Postgres container of the laptop stack's shape it replays the platform release's
connector fixtures and runs the dbt DAG's tasks (`tests/scenarios/platform.Hosts`, as the
scenarios do), and it serves the configured MCP runtime from the MCP release over that
Postgres, or over the existing warehouse a case names. Each new release (a push to the default
branch ships one) is served once its runtime is built, so the old one answers meanwhile.

A release's answers take effect when it ships, which is what a push to the default branch
does here. With `seed_passwords`, for cases that do not test sign-in, people sign in with the
harness password whatever the release records: every admin it names is invited and every
person gets `password` once they exist. Without it the release's own sign-in holds, and the
first admin's link comes from MCP_ADMIN_LINK in the mocked cloud, as `admin-link` stores it.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
import threading
import time
import traceback
from contextlib import ExitStack
from pathlib import Path

PACKAGE_TESTS = Path(__file__).resolve().parents[2]
SCENARIOS = PACKAGE_TESTS / "scenarios"
POLL_SECONDS = 2


def scenarios_platform():
    """`tests/scenarios/platform.py`, imported under a name of its own so this process needs
    no `tests` package on its path."""
    name = "eval_harness_scenarios"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, SCENARIOS / "__init__.py", submodule_search_locations=[str(SCENARIOS)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{name}.platform")


def tree_digest(*roots: Path) -> str:
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _current(cloud_dir: Path, host: str) -> str | None:
    marker = cloud_dir / "hosts" / host / "current"
    return marker.read_text().strip() if marker.is_file() else None


class DeployedMcp(threading.Thread):
    """Serves the mocked environment's MCP on `front` until `stop`."""

    def __init__(
        self,
        front,
        *,
        cloud_dir: Path,
        work: Path,
        host: str,
        password: str,
        gaps: dict[str, dict] | None = None,
        seed_passwords: bool = True,
        say=print,
    ):
        super().__init__(daemon=True, name="deployed-mcp")
        self.front, self.cloud_dir, self.work = front, Path(cloud_dir), Path(work)
        self.host, self.password, self.say = host, password, say
        self.gaps = gaps or {}
        self.seed_passwords = seed_passwords
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.served: str | None = None
        self.built: str | None = None
        self.loaded: str | None = None
        self.runtime = None
        self.admins: tuple[str, ...] = ()
        self._stack = ExitStack()
        self._pg = None
        self._patch = None
        front.wait(host, f"{host} answers once `vecten deploy` has shipped")

    def stop(self) -> None:
        self.stopped.set()
        if self.is_alive():
            self.join(30)
        self._stack.close()

    def run(self) -> None:
        failed: str | None = None
        while not self.stopped.is_set():
            commit = _current(self.cloud_dir, "mcp")
            if commit and commit not in (self.served, failed):
                try:
                    self.serve(commit)
                    failed = None
                except Exception:
                    failed = commit
                    self.say(f"serving release {commit[:8]} failed:\n{traceback.format_exc()}")
            if self.runtime is not None:
                try:
                    if self.seed_passwords:
                        self.seed_people()
                    seed_gaps(self.runtime.state, self.gaps)
                except Exception:
                    self.say(f"seeding sign-ins and gaps failed:\n{traceback.format_exc()}")
            self.stopped.wait(POLL_SECONDS)

    def _postgres(self, served):
        if self._pg is None:
            self._pg = self._stack.enter_context(served.laptop_postgres(self.work / "pg"))
        return self._pg

    def _monkeypatch(self):
        import pytest

        if self._patch is None:
            self._patch = pytest.MonkeyPatch()
            self._stack.callback(self._patch.undo)
        return self._patch

    def serve(self, commit: str) -> None:
        from vecten_mock_cloud import MockCloud

        from vecten_shared.config import load_config

        platform = scenarios_platform()
        served = platform.load_served()
        release = MockCloud.release(self.cloud_dir, "mcp")
        config = load_config(release)
        expected = f"mcp.{config.deploy.domain}" if config.deploy else None
        if expected != self.host:
            self.say(f"warning: release {commit[:8]} serves {expected}, not {self.host}")
        self.say(f"building the MCP for release {commit[:8]}")
        pg = self._postgres(served)
        patch = self._monkeypatch()
        hosts = platform.Hosts(self.cloud_dir, pg, patch, self.work / "scratch")
        if config.has("platform") and not config.warehouse.existing:
            platform_release = hosts.release("platform")
            loaded = tree_digest(platform_release / "connectors")
            if loaded != self.loaded:
                self.say("loading the raw fixtures")
                hosts.load_raw()
                pg.rerun_setup()
                self.loaded = loaded
            if platform_release.name != self.built:
                # The MCP serves a release's model only once the dbt DAG has built its commit.
                self.say(f"running the dbt DAG's tasks for {platform_release.name[:8]}")
                hosts.dbt_run()
                pg.rerun_setup()
                self.built = platform_release.name
        environ = {
            **platform.bundle_environ(release, hosts.artifacts),
            "VECTEN_MCP_SECRET_DIR": str(self.cloud_dir / "hosts" / "mcp" / "secrets"),
        }
        app, runtime = self.build(release, config, pg, patch, environ)
        self.front.mount_with_lifespan(self.host, app)
        self.runtime, self.served = runtime, commit
        self.admins = tuple(config.mcp.admins)
        self.ready.set()
        self.say(f"https://{self.host} serves release {commit[:8]}")

    def build(self, release, config, pg, patch, environ):
        """The runtime as `served.serve` wires it, under this host's public URL."""
        from types import SimpleNamespace

        from vecten_mcp.app import create_app
        from vecten_mcp.extensions import McpExtensions
        from vecten_mcp.runtime_setup import configured_runtime

        class LaptopProvider:
            def connection(self, env, db):
                return SimpleNamespace(host=pg.host, port=pg.port, database=db)

        secrets = self._cloud_secrets()
        patch.setattr("vecten_mcp.runtime_setup.get_provider", lambda *args: LaptopProvider())
        values = {
            "VECTEN_ENV": "local",
            "VECTEN_PROVIDER": "local",
            "VECTEN_MCP_PUBLIC_URL": f"https://{self.host}",
            "MCP_STATE_DB_PASSWORD": pg.passwords["MCP_STATE_DB_PASSWORD"],
            "MCP_PROVISIONER_PASSWORD": pg.passwords["PROVISIONER_PASSWORD"],
            "MCP_IMPERSONATOR_PASSWORD": pg.passwords["IMPERSONATOR_PASSWORD"],
            **environ,
        }
        if config.warehouse.existing:
            # The existing warehouse's roles hold the passwords the human stored for prod.
            for name in ("MCP_PROVISIONER_PASSWORD", "MCP_IMPERSONATOR_PASSWORD"):
                if name in secrets:
                    values[name] = secrets[name]
        for name, value in values.items():
            patch.setenv(name, value)
        if self.seed_passwords:
            config = config.model_copy(
                update={"mcp": config.mcp.model_copy(update={"auth": "password"})}
            )
        runtime = configured_runtime(Path(release), config, McpExtensions.from_config(config))
        backend = runtime.oauth.backend
        if hasattr(backend, "admin_link"):
            # A cloud provider reads MCP_ADMIN_LINK afresh at each redemption; here the
            # mocked cloud's secrets file is that store.
            backend.admin_link = lambda: self._cloud_secrets().get("MCP_ADMIN_LINK")
        return create_app(runtime=runtime), runtime

    def _cloud_secrets(self) -> dict[str, str]:
        import json

        path = self.cloud_dir / "secrets" / "prod.json"
        return json.loads(path.read_text()) if path.is_file() else {}

    def seed_people(self) -> None:
        """Invite every admin the release names, and give each person the harness password."""
        from vecten_mcp.identity import warehouse_name

        backend = self.runtime.oauth.backend
        state = backend.state
        for email in self.admins:
            if state.by_email(email) is None:
                taken = {p["warehouse_name"] for p in state.principals()}
                local = email.split("@")[0]
                state.invite(email, local.title(), warehouse_name(local, taken))
                self.say(f"invited admin {email}")
        for principal in state.principals():
            email = principal.get("email")
            if (
                principal.get("kind") == "human"
                and principal.get("enabled")
                and email
                and state.get("passwords", email) is None
            ):
                backend.set_password(backend.setup_link(email), self.password)
                self.say(f"{email} signs in with the harness password")


def seed_gaps(state, gaps: dict[str, dict]) -> None:
    """Store each gap the case starts with once; a gap the agent has since triaged or resolved
    keeps what it holds."""
    for gap_id, record in gaps.items():
        if state.get("gaps", gap_id) is None:
            state.put("gaps", gap_id, record)


def wait_until(condition, timeout: float, step: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()
