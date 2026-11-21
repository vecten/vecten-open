## ADDED Requirements

### Requirement: Read-only in-repo declaration check

`vecten lock --check` SHALL compare each registered in-repo extension's manifest, services and apps against `extensions.lock.json` without updating project records or resolving packages. Generated project CI SHALL run this check without private Git credentials.

#### Scenario: An extension changes its port

- **WHEN** CI checks an editable extension whose declared port differs from its record
- **THEN** the check fails and names the extension and the command to refresh the lock
- **AND** checking succeeds after the new declaration is locked

The check SHALL verify the freshness of each component lock even when there are zero local
extensions, naming missing or stale records. Project CI SHALL check projects with committed
lock records. CI declaration preparation SHALL use the CLI's lock installer and SHALL
leave a missing lock to the check's normal diagnostic.
