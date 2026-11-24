## Why

A local Docker tag from an earlier build can shadow a published release. Operators need to see which image their project actually used.

## What Changes

Report the selected base image for each active component when the local stack starts.
