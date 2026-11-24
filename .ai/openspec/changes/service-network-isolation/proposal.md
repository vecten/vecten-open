## Why

Services on a shared Docker network can contact peer containers directly. Every incoming
service request should pass through NGINX so the route's access rules and identity
headers apply. Client-supplied viewer attribution also needs to end at service ingress.

## What changes

Give each running service and container app a dedicated Docker network. Keep the VM's
metadata firewall and app-to-MCP audit attribution working across this change.
