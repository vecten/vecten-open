#!/usr/bin/env bash
# Runs warehouse_setup.sql, which reads the role passwords from this environment with \getenv
# so they never appear in psql's argv. The SQL sits in the vecten/ subdirectory because the
# postgres entrypoint would execute a top-level *.sql in /docker-entrypoint-initdb.d itself.
set -euo pipefail
with_mcp="${VECTEN_WITH_MCP:-false}"
if [ "$with_mcp" != "true" ] && [ "$with_mcp" != "false" ]; then
  echo "VECTEN_WITH_MCP must be exactly true or false, got: $with_mcp" >&2
  exit 2
fi
: "${AIRFLOW_DB_PASSWORD:?}" "${LOADER_PASSWORD:?}"
if [ "$with_mcp" = "true" ]; then
  : "${MCP_STATE_DB_PASSWORD:?required when VECTEN_WITH_MCP=true}" \
    "${PROVISIONER_PASSWORD:?required when VECTEN_WITH_MCP=true}" \
    "${IMPERSONATOR_PASSWORD:?required when VECTEN_WITH_MCP=true}"
fi
# The local container is the minimal profile's one instance: every database lives in it.
psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d postgres \
  -v with_airflow=true -v with_mcp_state="$with_mcp" -v with_warehouse=true \
  -v with_mcp="$with_mcp" -v areas="${VECTEN_AREAS:-}" \
  -f "$(dirname "${BASH_SOURCE[0]}")/vecten/warehouse_setup.sql"
