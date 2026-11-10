"""The existing warehouse of guide case 2 and semantic case 3: Postgres at
warehouse.acme.test:5432, database `analytics`, a `marts` schema holding one seeded table, and
the administrator login the locator names.

`vecten mcp introspect` connects with `sslmode=require`, so the server speaks TLS with a
certificate from the harness authority. The superuser and `mcp_admin` share the password the
human types at `vecten secret set WAREHOUSE_ADMIN_PASSWORD`, so introspection signs in whether
the agent passes `--user` or relies on the locator's user.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

HOST = "warehouse.acme.test"
PORT = 5432
DATABASE = "analytics"
ADMIN = "mcp_admin"
CONTAINER = "vecten-eval-warehouse"

ORDERS = [
    (1001, "2026-07-03", "DE", "1200.00"),
    (1002, "2026-07-11", "GB", "340.50"),
    (1003, "2026-07-19", "FR", "89.90"),
    (1004, "2026-08-02", "DE", "2310.00"),
    (1005, "2026-08-14", "PL", "455.25"),
    (1006, "2026-08-29", "GB", "1999.99"),
    (1007, "2026-09-05", "US", "760.00"),
    (1008, "2026-09-17", "DE", "125.00"),
    (1009, "2026-09-30", "FR", "3100.00"),
    (1010, "2026-10-08", "PL", "640.40"),
]
ORDER_REVENUE = sum(Decimal(amount) for *_, amount in ORDERS)


@dataclass(frozen=True)
class Seed:
    """The table under `marts` a case's warehouse holds: (name, SQL type) columns, rows."""

    table: str
    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple, ...]
    comment: str


GUIDE_SEED = Seed(
    "orders",
    (
        ("order_id", "integer PRIMARY KEY"),
        ("ordered_at", "date NOT NULL"),
        ("country", "text NOT NULL"),
        ("amount", "numeric(12, 2) NOT NULL"),
    ),
    tuple(ORDERS),
    "One row per order, with its amount in EUR.",
)


def _literal(value) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return "'" + str(value).replace("'", "''") + "'"
    return str(value)


def seed_sql(password: str, seed: Seed = GUIDE_SEED) -> str:
    literal = password.replace("'", "''")
    columns = ",\n".join(f"  {name} {kind}" for name, kind in seed.columns)
    names = ", ".join(name for name, _kind in seed.columns)
    rows = ",\n  ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in seed.rows)
    comment = seed.comment.replace("'", "''")
    return f"""CREATE ROLE {ADMIN} LOGIN CREATEROLE PASSWORD '{literal}';
GRANT ALL ON DATABASE {DATABASE} TO {ADMIN};
CREATE SCHEMA marts AUTHORIZATION {ADMIN};
SET ROLE {ADMIN};
CREATE TABLE marts.{seed.table} (
{columns}
);
COMMENT ON TABLE marts.{seed.table} IS '{comment}';
INSERT INTO marts.{seed.table} ({names}) VALUES
  {rows};
RESET ROLE;
"""


def _docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kwargs)


@dataclass
class ExistingWarehouse:
    work: Path
    password: str
    port: int = PORT
    container: str = CONTAINER
    seed: Seed = GUIDE_SEED

    def start(self, cert: Path, key: Path, image: str) -> None:
        """Start the container, replacing a leftover one of the same name."""
        _docker("rm", "-f", "-v", self.container, check=False)
        init = self.work / "warehouse-init"
        certs = self.work / "warehouse-certs"
        for directory in (init, certs):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True)
        (init / "10-acme.sql").write_text(seed_sql(self.password, self.seed))
        shutil.copy(cert, certs / "server.crt")
        shutil.copy(key, certs / "server.key")
        for path in (init, certs, *init.iterdir(), *certs.iterdir()):
            path.chmod(0o755 if path.is_dir() else 0o644)
        # Postgres refuses a key file others can read or another user owns, so the entrypoint
        # copies it to a place the server's user owns before starting.
        script = (
            "mkdir -p /run/vecten-ssl && cp /certs/server.* /run/vecten-ssl/ && "
            "chown postgres /run/vecten-ssl/server.* && chmod 600 /run/vecten-ssl/server.key && "
            "exec docker-entrypoint.sh postgres -c ssl=on "
            "-c ssl_cert_file=/run/vecten-ssl/server.crt "
            "-c ssl_key_file=/run/vecten-ssl/server.key"
        )
        _docker(
            "run",
            "-d",
            "--name",
            self.container,
            "-p",
            f"127.0.0.1:{self.port}:5432",
            "-e",
            f"POSTGRES_PASSWORD={self.password}",
            "-e",
            f"POSTGRES_DB={DATABASE}",
            "-v",
            f"{init}:/docker-entrypoint-initdb.d:ro",
            "-v",
            f"{certs}:/certs:ro",
            "--entrypoint",
            "bash",
            image,
            "-c",
            script,
        )
        deadline = time.time() + 90
        while True:
            logs = _docker("logs", self.container, check=False)
            text = logs.stdout + logs.stderr
            # The entrypoint's init server reports ready first; the second report is the
            # server listening on TCP after the init scripts ran.
            if text.count("ready to accept connections") >= 2:
                return
            if (
                _docker(
                    "inspect", "-f", "{{.State.Running}}", self.container, check=False
                ).stdout.strip()
                != "true"
                or time.time() > deadline
            ):
                raise RuntimeError(f"the warehouse container did not start:\n{text[-2000:]}")
            time.sleep(1)

    def stop(self) -> None:
        _docker("rm", "-f", "-v", self.container, check=False)

    def provision(self, project: Path, passwords: dict[str, str], environ: dict[str, str]):
        """Run the provisioner script `vecten mcp provisioner-script` prints for `project`, as
        the warehouse administrator does, with the passwords the human stores.

        The `next` item that shows the script runs before the deploy layer exists, so the
        script is the one printed without `--env`; a Postgres script names no issuer.
        """
        from eval_harness.issues_replay import real_vecten

        command = [*real_vecten(environ.get("PATH", "")), "mcp", "provisioner-script"]
        script = subprocess.run(
            [*command, "--json"],
            cwd=project,
            env=environ,
            capture_output=True,
            text=True,
        )
        if script.returncode != 0:
            raise RuntimeError(f"vecten mcp provisioner-script failed: {script.stdout}")
        sql = json.loads(script.stdout)["sql"]
        exported = [f"--env={name}" for name in passwords]
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                *exported,
                self.container,
                "psql",
                "-U",
                "postgres",
                "-d",
                DATABASE,
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                "-",
            ],
            input=sql,
            capture_output=True,
            text=True,
            env={**os.environ, **passwords},
        )
        if result.returncode != 0:
            raise RuntimeError(f"the provisioner script failed:\n{result.stderr[-2000:]}")
        return result.stdout
