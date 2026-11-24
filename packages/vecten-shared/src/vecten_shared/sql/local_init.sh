#!/usr/bin/env bash
# Runs warehouse_setup.sql, which reads the role passwords from this environment with \getenv
# so they never appear in psql's argv. The SQL sits in the vecten/ subdirectory because the
# postgres entrypoint would execute a top-level *.sql in /docker-entrypoint-initdb.d itself.
set -euo pipefail
with_mcp="${VECTEN_WITH_MCP:-false}"
with_gate="${VECTEN_WITH_GATE:-false}"
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
if [[ "$with_gate" == true ]]; then
  : "${MCP_GATE_DB_PASSWORD:?required when VECTEN_WITH_GATE=true}"
fi
if [[ "${1:-}" == "--ensure-gate" ]]; then
  [[ "$with_gate" == true ]] || exit 0
  # A fresh volume already admits the gate. A failed or partial upgrade retries the setup.
  if PGPASSWORD="$MCP_GATE_DB_PASSWORD" psql -X -w -h 127.0.0.1 -U mcp_gate -d mcp_state -c 'SELECT 1' >/dev/null 2>&1; then
    exit 0
  fi
  exec psql -X -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d postgres \
    -v with_gate=true -v with_airflow=false -v with_mcp_state=true -v with_warehouse=false -v with_mcp=false \
    -f "$(dirname "${BASH_SOURCE[0]}")/vecten/warehouse_setup.sql"
fi
# The local container is the minimal profile's one instance: every database lives in it.
psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d postgres \
  -v with_gate="$with_gate" -v with_airflow=true -v with_mcp_state="$with_mcp" -v with_warehouse=true \
  -v with_mcp="$with_mcp" -v areas="${VECTEN_AREAS:-}" -v source_layer_areas="${VECTEN_SOURCE_LAYER_AREAS:-}" \
  -f "$(dirname "${BASH_SOURCE[0]}")/vecten/warehouse_setup.sql"
