#!/usr/bin/env bash
# Builds the node into www/ (git-ignored). Needs Node.js 22.13 or later.
set -euo pipefail
cd "$(dirname "$0")"
AGSC="${AGSC:-npx --yes --package agentic-system-core@1.1.0 agsc}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || echo 1795176000)}"
$AGSC lint && $AGSC build && $AGSC verify
