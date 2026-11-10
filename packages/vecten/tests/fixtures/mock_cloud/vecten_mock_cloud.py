"""A cloud on the local filesystem, for the scenarios and the scripted-human evaluation.

Select it in any `vecten` process with

    PYTHONPATH=packages/vecten/tests/fixtures/mock_cloud
    VECTEN_CONTROL_PLANE=vecten_mock_cloud:MockCloud
    VECTEN_MOCK_CLOUD_DIR=<a directory standing for the cloud account>

The directory holds `state/<env>/outputs.json`, `secrets/<env>.json`, `bootstrap/<env>.json`,
`hosts/<host>/releases/<commit>/` with `hosts/<host>/current` naming the live release, and
`ships.jsonl` with one line per bundle placed. Nothing here creates a database or a VM: a
bundle is extracted where a VM agent would extract it, and the caller runs what that VM would.
"""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any

from vecten.deploy.bundle import build_host_bundle, prepare_bundle_metadata
from vecten.deploy.control import CloudBootstrap
from vecten.deploy.outputs import outputs_text, parse_stored_outputs, save_outputs
from vecten.deploy.shipping import ship_order
from vecten_shared.config import VectenConfig, load_config
from vecten_shared.names import check_secret_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import (
    EnvironmentOutputs,
    PostgresOutput,
    load_outputs,
    project_environment_file,
)
from vecten_shared.records import write_text_atomic

CLOUD_DIR_VAR = "VECTEN_MOCK_CLOUD_DIR"


def _host(name: str, components: list[str], hostnames: list[str]) -> dict:
    return {
        "instance_id": f"mock-{name}",
        "public_ip": "192.0.2.10" if name == "platform" else "192.0.2.11",
        "components": components,
        "hostnames": hostnames,
    }


class MockCloud:
    """`ControlPlane` and the secret half of `Provider`, over `VECTEN_MOCK_CLOUD_DIR`."""

    cloud = "mock"
    cloud_only_plans_refresh = True
    admin_grants: tuple[str, ...] = ()
    guarded_resources: tuple[str, ...] = ()

    OUTPUTS: dict[str, Any] = {
        "environment": "prod",
        "region": "mock-1",
        "hosts": {
            "platform": _host("platform", ["platform"], ["airflow.data.acme.com"]),
            "mcp": _host("mcp", ["mcp"], ["mcp.data.acme.com"]),
        },
        "setup_host": "mcp",
        "bucket": "mock-data",
        "secret_prefix": "vecten/acme/prod/",
        "postgres": {
            "state": {
                "host": "state.mock.internal",
                "port": 5432,
                "databases": ["airflow", "mcp_state"],
                "master_secret": "mock:state-master",
            },
            "warehouse": {
                "host": "warehouse.mock.internal",
                "port": 5432,
                "databases": ["warehouse"],
                "master_secret": "mock:warehouse-master",
            },
        },
    }

    def __init__(self, project_root: Path, env: str):
        self.project_root = Path(project_root)
        self.env = env
        directory = os.environ.get(CLOUD_DIR_VAR)
        if not directory:
            raise Failure(
                ExitCode.PREREQUISITE, f"{CLOUD_DIR_VAR} names no directory for the mocked cloud"
            )
        self.root = Path(directory)

    @staticmethod
    def release(cloud_dir: Path, host: str) -> Path:
        """The release directory `host` runs now."""
        hosts = Path(cloud_dir) / "hosts" / host
        return hosts / "releases" / (hosts / "current").read_text().strip()

    def _json(self, path: Path, default: Any) -> Any:
        return json.loads(path.read_text()) if path.is_file() else default

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def config(self) -> VectenConfig:
        return load_config(self.project_root)

    def check_login(self) -> str:
        return "mock-account"

    def bootstrap_env(
        self,
        env: str,
        repository: str,
        branch: str,
        account: str,
        *,
        subject_prefixes: Sequence[str],
    ) -> CloudBootstrap:
        record = self.root / "bootstrap" / f"{env}.json"
        created = [] if record.is_file() else ["mock-state", "mock-deploy-role", "mock-plan-role"]
        self._write_json(
            record,
            {
                "repository": repository,
                "branch": branch,
                "account": account,
                "subject_prefixes": list(subject_prefixes),
            },
        )
        # The report carries the fields `vecten bootstrap` prints for AWS, whose shape it has.
        return CloudBootstrap(
            state_bucket="mock-state",
            environment_variables={},
            repository_variables={},
            report={
                "role_arn": "mock:deploy-role",
                "plan_role_arn": "mock:plan-role",
                "created": created,
            },
            repository_use=None,
        )

    def terraform_roots(self, env: str, *, cloud_only: bool = False, read_only: bool = False):
        return []

    def backend_config(self, env: str, root) -> dict[str, str]:
        return {}

    def terraform_env(self) -> dict[str, str]:
        return {}

    def outputs(self) -> EnvironmentOutputs:
        return load_outputs(project_environment_file(self.project_root, self.env), self.env)

    def _outputs_file(self) -> Path:
        return self.root / "state" / self.env / "outputs.json"

    def store_outputs(self, raw: dict[str, Any]) -> EnvironmentOutputs:
        # `terraform output -json` wraps each value; a plain outputs dict is taken as it is.
        values = {
            name: value["value"] if isinstance(value, dict) and "value" in value else value
            for name, value in raw.items()
        }
        outputs = EnvironmentOutputs.model_validate({**values, "environment": self.env})
        path = self._outputs_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, outputs_text(outputs))
        save_outputs(self.project_root, self.env, outputs)
        return outputs

    def stored_outputs(self) -> EnvironmentOutputs | None:
        path = self._outputs_file()
        if not path.is_file():
            return None
        outputs = parse_stored_outputs(path.read_text(), self.outputs_location(), self.env)
        save_outputs(self.project_root, self.env, outputs)
        return outputs

    def outputs_location(self) -> str:
        return str(self._outputs_file())

    def forget_outputs(self) -> list[str]:
        for path in (self._outputs_file(), project_environment_file(self.project_root, self.env)):
            path.unlink(missing_ok=True)
        return []

    def check_refresh_safe(self) -> None:
        return None

    def before_cloud_apply(self) -> None:
        return None

    def after_cloud_apply(self, outputs: EnvironmentOutputs) -> None:
        return None

    def ship_hosts(
        self,
        project_root: Path,
        config: VectenConfig,
        env: str,
        outputs: EnvironmentOutputs,
        commit: str,
    ) -> list[str]:
        metadata = prepare_bundle_metadata(
            project_root,
            config,
            commit,
            with_mcp=any("mcp" in host.components for host in outputs.hosts.values()),
        )
        shipped = []
        with tempfile.TemporaryDirectory(prefix="vecten-mock-bundles-") as tmp:
            for host in ship_order(outputs):
                bundle = build_host_bundle(
                    project_root, config, env, outputs, host, commit, Path(tmp), metadata=metadata
                )
                target = self.root / "hosts" / host / "releases" / commit
                target.mkdir(parents=True, exist_ok=True)
                with tarfile.open(bundle) as tar:
                    tar.extractall(target, filter="data")
                write_text_atomic(self.root / "hosts" / host / "current", commit + "\n")
                with (self.root / "ships.jsonl").open("a") as log:
                    log.write(json.dumps({"env": env, "host": host, "commit": commit}) + "\n")
                shipped.append(host)
        return shipped

    def final_snapshots(self, outputs, now: datetime, *, on_created, absent, expires) -> list[str]:
        names = [f"{key}-final-{now:%Y%m%d%H%M%S}" for key in outputs.postgres]
        for name in names:
            on_created(name)
        return names

    def lift_protection(self, terraform, root, state, *, checked, absent):
        return root

    def require_admin_access(self) -> None:
        return None

    def master_credential(self, instance: PostgresOutput) -> tuple[str, str]:
        raise Failure(ExitCode.PREREQUISITE, "the mocked cloud has no databases")

    def admin_connection(self, outputs, instance) -> AbstractContextManager[dict[str, Any]]:
        raise Failure(ExitCode.PREREQUISITE, "the mocked cloud has no databases")

    def _secrets_file(self) -> Path:
        return self.root / "secrets" / f"{self.env}.json"

    def secret_id(self, name: str) -> str:
        return f"mock:{self.env}/{name}"

    def read_object(self, key: str) -> bytes | None:
        bucket = (self.root / "bucket").resolve()
        path = (bucket / key).resolve()
        if not path.is_relative_to(bucket) or not path.is_file():
            return None
        return path.read_bytes()

    def read_state_object(self, key: str) -> bytes | None:
        # The mock cloud has no Terraform roots, so no root's state lock exists.
        return None

    def put_secret(self, name: str, value: str) -> None:
        check_secret_name(name)
        values = self._json(self._secrets_file(), {})
        values[name] = value
        self._write_json(self._secrets_file(), values)

    def get_secret(self, name: str) -> str:
        values = self._json(self._secrets_file(), {})
        if name not in values:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"secret {name} is not set; run `vecten secret set {name} --env {self.env}`",
            )
        return values[name]

    def list_secrets(self) -> list[str]:
        return sorted(self._json(self._secrets_file(), {}))
