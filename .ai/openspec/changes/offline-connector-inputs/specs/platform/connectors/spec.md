## ADDED Requirements

### Requirement: Run connector transformations offline from recorded inputs

In addition to recording output rows for warehouse replay, a user MAY explicitly record
JSON input rows before connector processing with `connector run --record --record-inputs`.
A participating connector SHALL accept an injectable reader at its source boundary. An input
sample SHALL identify the object and carry the expected transformation result from that run.
A failed extraction, load or fixture write SHALL preserve the previous input and output fixtures. The CLI SHALL refuse
input recording for a connector that declares an output fixture redactor.

The rendered connector test and local dbt build SHALL run the current source against each
saved input sample with network access blocked. A removed object, a parsing exception, an
unused input reader or changed output SHALL fail verification. With only output fixtures,
both entry points SHALL continue replaying the stored rows without importing the connector.

#### Scenario: A source renames a field

- **WHEN** the connector turns userId into user_id, then records both fixtures
- **THEN** the input fixture contains userId and the warehouse fixture contains user_id
- **AND** changing the connector to read a missing field fails the offline replay

#### Scenario: Recording inputs is not requested

- **WHEN** the user records ordinary fixtures
- **THEN** Vecten does not persist the upstream payloads

Input sampling SHALL count records within list pages and preserve the page boundary when
trimming the final page. Each selected object supports a single upstream reader. If its
parent and child both request live inputs, recording SHALL identify this limitation and
preserve earlier fixtures. Generated CI SHALL make declared connector dependencies
available to both commands that replay input samples.

An input sample SHALL record the output row limit of the run that captured it. Replay SHALL
stop the transformed output at that limit, at the same place as the recording did, so the
recorded rows and the replayed rows match under `--limit` and when one input yields several
rows.

#### Scenario: A limited recording of paged or fanned-out inputs

- **WHEN** a connector reads 100-row pages, or yields ten rows per input, and records with `--record --record-inputs --limit 3`
- **THEN** the output fixture holds three rows and the input fixture records the limit
- **AND** offline replay transforms the saved inputs and verifies the same three rows

#### Scenario: All captured inputs are filtered out

- **WHEN** input recording succeeds with zero output rows and there is no prior output fixture
- **THEN** the input fixture verifies that transformation offline with zero expected rows
- **AND** an empty refresh of an existing output fixture preserves its rows and companion files

#### Scenario: A connector dependency is a registered extension

- **WHEN** generated CI resolves a dependency for input replay
- **THEN** direct, transitive and build dependencies on registered local packages resolve from the checkout
- **AND** a registered Git package or a private index requirement cannot fall back to a public index
- **AND** connectors with only output fixtures do not need connector dependency installation
