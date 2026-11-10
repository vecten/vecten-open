# Base image for the platform component. Built at release from the wheels in dist/;
# a project adds its own thin layer on top (extensions, connector dependencies).
ARG AIRFLOW_IMAGE=apache/airflow:3.3.2-python3.14
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM ${AIRFLOW_IMAGE}
ARG AIRFLOW_VERSION=3.3.2
ARG VECTEN_VERSION
LABEL org.opencontainers.image.source=https://github.com/vecten/vecten-open \
      org.opencontainers.image.description="Apache Airflow with the Vecten Open platform runtime" \
      org.opencontainers.image.licenses=Apache-2.0 \
      org.opencontainers.image.version=${VECTEN_VERSION}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*
USER airflow
# A fresh artifacts volume copies this directory's owner and mode, and Compose runs Airflow as
# an arbitrary uid in group 0, so the group needs write access.
RUN mkdir -p /opt/airflow/artifacts && chmod 775 /opt/airflow/artifacts
# Build-time only: ARG values never reach the image's environment. The upstream image sets
# UV_CACHE_DIR with ENV, which an ARG cannot override, so the cache directory is a flag.
ARG UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
# uv.lock holds no Airflow, so this resolves against the packages the upstream image installed,
# into its virtualenv ($VIRTUAL_ENV). The upstream image ships its own uv ahead of /bin on PATH;
# /bin/uv is the pinned one, mounted, never copied, with its temporary lock files in a tmpfs
# /tmp; the lock it takes on the virtualenv ($VIRTUAL_ENV/.lock) is removed in the same step.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,id=vecten-airflow-uv,target=/mnt/uv-cache,uid=50000,gid=0 \
    --mount=type=tmpfs,target=/tmp \
    --mount=type=bind,source=dist,target=/mnt/wheels \
    /bin/uv pip install --cache-dir /mnt/uv-cache "apache-airflow==${AIRFLOW_VERSION}" \
        /mnt/wheels/vecten_shared-*.whl /mnt/wheels/vecten_platform-*.whl \
    && rm -f "${VIRTUAL_ENV}/.lock"
