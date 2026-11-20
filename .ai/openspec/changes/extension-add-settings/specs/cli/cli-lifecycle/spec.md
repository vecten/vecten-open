## ADDED Requirements

### Requirement: Settings during extension registration

Extension registration SHALL accept repeatable `--set key=value` arguments for base settings and validate the resulting configuration before committing project records.

#### Scenario: A required setting at install time

- **WHEN** a project adds an extension with all required settings
- **THEN** one command writes the registration, validated settings and locks
- **AND** a rejected or missing required setting leaves all previous project records intact
