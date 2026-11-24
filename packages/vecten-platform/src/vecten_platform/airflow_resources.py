"""Apply project and extension declarations after Airflow has migrated its metadata database."""

import os
from pathlib import Path

from vecten_shared.airflow_resources import effective_resources
from vecten_shared.config import load_config
from vecten_shared.extension_record import read_record


def apply(root: Path) -> None:
    from airflow.models.pool import Pool
    from airflow.models.variable import Variable

    declared = effective_resources(load_config(root), read_record(root) or {})
    for name, slots in declared.pools.items():
        Pool.create_or_update_pool(name, slots, "Managed by Vecten", include_deferred=False)
    for name, default in declared.variables.items():
        Variable.setdefault(name, default)


if __name__ == "__main__":
    apply(Path(os.environ["VECTEN_PROJECT_ROOT"]))
