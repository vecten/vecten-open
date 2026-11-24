# Service login checks using forwarded request headers

## Why

A proxy that sends the original host and URI needs the same group decision as the NGINX configuration Vecten generates. The MCP already has the service declarations and group bindings needed to make that decision.

## What Changes

- Accept a forward-auth check for a declared login route and optionally redirect a browser to sign-in.
- Check the generated NGINX configuration against its route declarations.

## Impact

The MCP gate and its tests change. Existing generated NGINX installations continue to use their route keys.
