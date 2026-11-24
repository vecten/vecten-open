## ADDED Requirements

### Requirement: Record a bounded sample or use synthetic fixtures

Recording SHALL stop each synchronous resource after the requested number of input rows
(default and maximum 200) and SHALL trim the last yielded page. Connector code MAY read the
per-object budget before requesting a page. A recording-only hook MAY transform rows before
they reach the fixture writer or sample load. A connector declaration or extension MAY
disable recording, which SHALL refuse before loading connector code.

#### Scenario: A metered resource has more pages

- **WHEN** a recording reaches its limit
- **THEN** the runner stops pulling, without requesting the next page

#### Scenario: A connector uses invented rows

- **WHEN** an author checks hand-written JSON Lines fixtures
- **THEN** the CLI validates object records and checks filenames against explicit object groups offline
- **AND** a project that disables recording receives instructions for synthetic fixtures

#### Scenario: A REST endpoint reads its parent to fetch children

- **WHEN** a recording selects a dependent resource with a row limit
- **THEN** the runner bounds the synchronous parent and child iterators separately
- **AND** recording a connector still attempts its other declared objects
