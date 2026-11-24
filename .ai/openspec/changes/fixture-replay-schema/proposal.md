## Why

A local build can lose a connector's JSON column when dlt infers a fresh schema from its
recorded rows. Staging queries that work after a live sync then fail offline.

## What changes

Save the load schema alongside each recorded object and use it in both offline replay paths.
Projects can continue using their existing JSON Lines fixtures.
