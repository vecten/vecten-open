# Base image for the MCP component. Built at release from uv.lock and the wheels in dist/;
# a project adds its own thin layer on top (extensions, connector dependencies).
ARG BASE_IMAGE=python:3.14-slim
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM ${BASE_IMAGE}
ARG VECTEN_VERSION
LABEL org.opencontainers.image.source=https://github.com/vecten/vecten-open \
      org.opencontainers.image.description="The Vecten Open MCP server" \
      org.opencontainers.image.licenses=Apache-2.0 \
      org.opencontainers.image.version=${VECTEN_VERSION}
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
# The uv cache mounts below are created for uid 1000, so app must be that uid.
RUN useradd --create-home --uid 1000 app \
    && python -m venv /opt/venv \
    && chown -R app /opt/venv \
    && mkdir -p /var/lib/vecten-mcp/secrets \
    && chown -R app /var/lib/vecten-mcp \
    && chmod 700 /var/lib/vecten-mcp/secrets
ENV PATH=/opt/venv/bin:$PATH
USER app
# Build-time only: ARG values never reach the image's environment.
ARG UV_CACHE_DIR=/mnt/uv-cache UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
# uv is mounted, never copied; its temporary lock files land in a tmpfs /tmp, and the lock it
# takes on the venv (/opt/venv/.lock) is removed in the same step, so the image holds none of
# them. The lock inputs alone decide this layer, so a code change reuses it. --locked fails on a
# lock that no longer matches the members' metadata; --inexact keeps the venv's pip, which the
# project layer installs with.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,id=vecten-mcp-uv,target=/mnt/uv-cache,uid=1000 \
    --mount=type=tmpfs,target=/tmp \
    --mount=type=bind,source=uv.lock,target=/mnt/lock/uv.lock \
    --mount=type=bind,source=pyproject.toml,target=/mnt/lock/pyproject.toml \
    --mount=type=bind,source=packages,target=/mnt/lock/packages \
    cd /mnt/lock \
    && UV_PROJECT_ENVIRONMENT=/opt/venv \
        uv sync --locked --no-dev --package vecten-mcp --no-install-workspace --inexact \
    && rm -f /opt/venv/.lock
# The release wheels themselves; every dependency they declare must already be installed.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,id=vecten-mcp-uv,target=/mnt/uv-cache,uid=1000 \
    --mount=type=tmpfs,target=/tmp \
    --mount=type=bind,source=dist,target=/mnt/wheels \
    uv pip install --python /opt/venv/bin/python --no-deps \
        /mnt/wheels/vecten_shared-*.whl /mnt/wheels/vecten_mcp-*.whl \
    && uv pip check --python /opt/venv/bin/python \
    && rm -f /opt/venv/.lock
EXPOSE 8001
CMD ["python", "-m", "vecten_mcp"]
