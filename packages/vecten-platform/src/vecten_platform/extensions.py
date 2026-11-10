"""Expose installed extension DAGs to Airflow without copying package code."""

import hashlib
import importlib.util
import sys
from pathlib import Path

from vecten_shared.config import VectenConfig
from vecten_shared.extensions import extension_paths
from vecten_shared.output import ExitCode, Failure


def dag_files(config: VectenConfig) -> list[Path]:
    return [
        path
        for directory in extension_paths(config, "vecten.dags").values()
        for path in sorted(directory.rglob("*.py"))
        if not any(part.startswith("_") for part in path.relative_to(directory).parts)
    ]


def load_dags(config: VectenConfig) -> dict[str, object]:
    paths = dag_files(config)
    if not paths:
        return {}
    from airflow.sdk import DAG
    from airflow.sdk.definitions._internal.contextmanager import DagContext

    dags = {}
    for path in paths:
        module_name = "vecten_dag_" + hashlib.sha256(str(path).encode()).hexdigest()
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        # The outer DagBag must attribute these DAGs to the generated loader in its bundle,
        # so consume only our registrations and restore the outer parser's context.
        previous_name = DagContext.current_autoregister_module_name
        previous_dags = DagContext.autoregistered_dags.copy()
        DagContext.autoregistered_dags.clear()
        DagContext.current_autoregister_module_name = module_name
        try:
            spec.loader.exec_module(module)
            found = {v for v in vars(module).values() if isinstance(v, DAG)}
            found.update(dag for dag, _ in DagContext.autoregistered_dags)
        except Exception as exc:
            raise Failure(
                ExitCode.CONFIG,
                f"extension DAG {path.name} failed to load ({type(exc).__name__})",
            ) from exc
        finally:
            DagContext.current_autoregister_module_name = previous_name
            DagContext.autoregistered_dags.clear()
            DagContext.autoregistered_dags.update(previous_dags)
        for dag in found:
            if dag.dag_id in dags and dags[dag.dag_id] is not dag:
                raise Failure(ExitCode.CONFIG, f"duplicate extension DAG {dag.dag_id}")
            dags[dag.dag_id] = dag
    return dags
