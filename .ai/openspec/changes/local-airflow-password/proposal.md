# Local Airflow password lives in memory

The laptop SHALL persist the Airflow admin password only in the project's `.env`. Airflow SHALL receive its password file on memory-backed storage. Starting an existing project SHALL remove its old generated host password file.
