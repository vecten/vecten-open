from importlib import resources
from pathlib import Path

SETUP_DATABASES = ("airflow", "mcp_state", "warehouse")


def setup_sql_path() -> Path:
    return Path(str(resources.files("vecten_shared.sql").joinpath("warehouse_setup.sql")))


def local_init_script_path() -> Path:
    return Path(str(resources.files("vecten_shared.sql").joinpath("local_init.sh")))


def setup_variables(instance_databases: list[str], with_mcp: bool) -> dict[str, str]:
    """The psql `-v` map that runs warehouse_setup.sql for one instance.

    `with_mcp` is true only on the instance holding `warehouse`: `provisioner` and
    `impersonator` belong to the warehouse, and the state instance holds no human login role.
    """
    unknown = sorted(set(instance_databases) - set(SETUP_DATABASES))
    if unknown:
        raise ValueError(f"no setup for database(s): {', '.join(unknown)}")

    def flag(on: bool) -> str:
        return "true" if on else "false"

    with_warehouse = "warehouse" in instance_databases
    return {
        "with_airflow": flag("airflow" in instance_databases),
        "with_mcp_state": flag("mcp_state" in instance_databases),
        "with_warehouse": flag(with_warehouse),
        "with_mcp": flag(with_mcp and with_warehouse),
    }
