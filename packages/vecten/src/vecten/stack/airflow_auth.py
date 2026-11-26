"""Seed the local Airflow password file and keep its tmpfs mounted across service restarts."""

import json
import os
from pathlib import Path

from dotenv import dotenv_values


def passwords(env, accounts):
    admin = env["AIRFLOW_ADMIN_USER"]
    values = {admin: env["AIRFLOW_ADMIN_PASSWORD"]}
    for account, secret in accounts.items():
        if account == admin or not env.get(secret):
            raise ValueError(
                f"Airflow API account {account} needs a distinct username and secret {secret}"
            )
        values[account] = env[secret]
    return values


def main() -> None:
    env = dotenv_values("/project.env", interpolate=False)
    os.umask(0o077)
    target = Path("/run/vecten-airflow-auth/passwords.json")
    temp = target.with_suffix(".tmp")
    accounts = json.loads(os.environ.get("VECTEN_AIRFLOW_API_ACCOUNTS", "{}"))
    temp.write_text(json.dumps(passwords(env, accounts)))
    temp.replace(target)
    os.execlp("sleep", "sleep", "infinity")


if __name__ == "__main__":
    main()
