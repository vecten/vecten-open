## ADDED Requirements

### Requirement: Offline fixture replay preserves the recorded table shape

After a successful nonempty recording, Vecten SHALL save the dlt column types, key hints,
write disposition and nesting depth needed to load the recorded rows into their raw tables.
The rendered connector test and `vecten dbt-build-local` SHALL use this information offline.

#### Scenario: A connector stores each original record in a JSON column

- **WHEN** a connector records an object with a JSON column containing a list
- **THEN** offline replay exposes the JSON column and its list to staging queries in the same table

#### Scenario: A fixture contains rows only

- **WHEN** an object has a JSON Lines fixture without schema information
- **THEN** replay appends the rows using dlt's default type and nesting inference

#### Scenario: A resource changes its table name while yielding rows

- **WHEN** an item carries a persistent table-name hint
- **THEN** recording and replay use that name for the item and subsequent unmarked items
- **AND** a per-item table variant routes only the marked item
