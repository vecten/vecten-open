## Why

Retrying an exhausted metered source can bill for the same records again. Operators need to distinguish a failure that requires action from a temporary upstream error.

## What Changes

Connectors can signal a terminal failure. The generated object task stops after one attempt and reports the connector’s reason through the usual alert.
