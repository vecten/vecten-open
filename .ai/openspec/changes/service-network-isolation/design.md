## Service networks

Use the unique Compose service name as the network key. Only the service and NGINX attach.
On laptops Docker assigns the bridge interface. On VMs derive a stable, 15-character Linux
interface name from the project and service. The reserved prefix `vecten-` also covers the
old `vecten-services` bridge during an upgrade. A single iptables interface match
(`vecten-+`) covers the bridges before Docker creates them. Older host records keep their
exact interface match. Hosts without services need no rule.

## Audit attribution

NGINX clears the incoming viewer attribution header in every location that forwards a
service hostname. The gate still supplies its five identity headers. An app can use the
verified viewer when it makes its own authenticated request to the MCP endpoint.
