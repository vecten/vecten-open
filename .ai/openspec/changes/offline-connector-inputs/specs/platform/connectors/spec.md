## ADDED Requirements

### Requirement: Run connector transformations offline from recorded inputs

In addition to recording output rows for warehouse replay, a user MAY explicitly record
JSON input rows before connector processing with `connector run --record --record-inputs`.
A participating connector SHALL accept an injectable reader at its source boundary. An input
sample SHALL identify the object and carry the expected transformation result from that run.
A failed extraction or load SHALL preserve the previous input sample. The CLI SHALL refuse
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
