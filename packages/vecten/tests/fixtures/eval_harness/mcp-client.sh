#!/bin/bash
# mcp-client: the person's MCP client; run it with no arguments for its usage.
export MCP_CLIENT_STATE=@@STATE@@
exec @@PYTHON@@ -I @@SCRIPT@@ "$@"
