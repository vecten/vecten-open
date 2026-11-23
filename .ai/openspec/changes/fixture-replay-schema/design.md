## Recording and replay

Keep the sampled rows in `<object>.jsonl`. Save the portable table metadata from a successful
load in `<object>.schema.json` beside them. Record the effective nesting depth per object so
that fixtures recorded with different settings can share a replay pipeline.

The connector test and the local dbt build both use the same fixture reader. A missing schema
file selects dlt's original append and inference behaviour for that object. Replay reads the
connector's output rows directly, so it can run offline without connector credentials.

An incompatible schema file stops replay and the error identifies the fixture. Falling back
to inference could change a JSON column into child tables or change a table's merge keys.
Re-record the object after a dlt upgrade if the schema becomes incompatible.
