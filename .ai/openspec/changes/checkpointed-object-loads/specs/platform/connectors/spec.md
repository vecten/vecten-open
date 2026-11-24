## ADDED Requirements

### Requirement: Checkpoint one unit of a metered object

A local or scheduled object run SHALL support append resources and merges using the default, delete-insert, upsert or insert-only strategy that opt into distinct loads, each carrying a resource-scoped cursor in dlt state. The runner SHALL load each unit successfully before requesting another and SHALL report the sum of recovered and newly loaded rows. Recording and input replay SHALL remain bounded to the sampled unit.

#### Scenario: A later unit fails

- **WHEN** a resource completes one unit and the next raises an error
- **THEN** a retry using the destination and a fresh working directory reads the committed cursor and starts at the next unit

#### Scenario: A continued unit fails to report progress

- **WHEN** a continued unit omits its completion marker or requests a cursor already visited in the run
- **THEN** the runner SHALL report the error before requesting another unit
