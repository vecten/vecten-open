# Design

Use a project-scoped Docker volume backed by tmpfs. An isolated container using the pinned upstream Airflow image reads the existing .env and writes only the admin credential to the shared memory mount. Airflow opens its password file for writing as well as reading. No password is interpolated into Docker metadata.
