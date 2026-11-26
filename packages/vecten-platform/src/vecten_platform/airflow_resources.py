"""Apply project and extension declarations after Airflow has migrated its metadata database."""

import os
from pathlib import Path

from vecten_shared.airflow_resources import effective_resources
from vecten_shared.config import load_config
from vecten_shared.extension_record import read_record


def apply(root: Path) -> None:
    from airflow.models.pool import Pool
    from airflow.models.variable import Variable
    from airflow.utils.session import create_session

    declared = effective_resources(load_config(root), read_record(root) or {})
    with create_session() as session:
        for name, slots in declared.pools.items():
            pool = Pool.get_pool(name, session=session)
            if pool is None:
                session.add(
                    Pool(
                        pool=name,
                        slots=slots,
                        description="Managed by Vecten",
                        include_deferred=False,
                    )
                )
            else:
                pool.slots = slots
    for name, default in declared.variables.items():
        Variable.setdefault(name, default)


if __name__ == "__main__":
    apply(Path(os.environ["VECTEN_PROJECT_ROOT"]))
