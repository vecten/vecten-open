# Design

The administrator installs a read-only procedure in the environment's access schema. The procedure
checks that its owner can see all grants, then uses Snowflake's hierarchy option on both current
and future grants. It returns a verdict to the provisioner, which needs only permission to call it.
The administrator can run the same two grant queries for a detailed review.

The server repeats the check on recorded bindings during reconciliation. It records failures in
access health so an administrator can correct the grants. Only shared accounts need to upgrade
their access procedures from version 6 to version 8.
