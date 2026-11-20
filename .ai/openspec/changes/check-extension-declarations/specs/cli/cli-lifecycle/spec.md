## ADDED Requirements

### Requirement: Read-only in-repo declaration check

`vecten lock --check` SHALL compare each registered in-repo extension's manifest, services and apps against `extensions.lock.json` without updating project records or resolving packages. Generated project CI SHALL run this check without private Git credentials.

#### Scenario: An extension changes its port

- **WHEN** CI checks an editable extension whose declared port differs from its record
- **THEN** the check fails and names the extension and the command to refresh the lock
- **AND** checking succeeds after the new declaration is locked
