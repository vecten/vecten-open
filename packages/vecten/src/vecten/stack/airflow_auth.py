"""Seed the local Airflow password file and keep its tmpfs mounted across service restarts."""

import json
import os
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    env = dotenv_values("/project.env", interpolate=False)
    os.umask(0o077)
    target = Path("/run/vecten-airflow-auth/passwords.json")
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps({env["AIRFLOW_ADMIN_USER"]: env["AIRFLOW_ADMIN_PASSWORD"]}))
    temp.replace(target)
    os.execlp("sleep", "sleep", "infinity")


if __name__ == "__main__":
    main()
