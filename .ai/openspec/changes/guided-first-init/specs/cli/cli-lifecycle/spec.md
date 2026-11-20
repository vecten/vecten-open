## ADDED Requirements

### Requirement: Initialize in the guide's working directory

Before the first render, `vecten init` SHALL permit a `.claude/` directory, a `.DS_Store`
file and the recorded `vecten-answers.yml` file in the target. It SHALL leave all three intact
and count only files it renders. Other top-level entries SHALL cause a refusal that names each one.

#### Scenario: Claude Code has started in the target

- **GIVEN** the target contains `.claude/.cc-writes/`, `.claude/settings.local.json`, `.DS_Store`
  and the completed local intake
- **WHEN** the guide runs `vecten init` there
- **THEN** the project is rendered directly in that directory and its existing entries are preserved
- **AND** the generated Git ignore rules exclude local Claude settings, write logs and Finder metadata while allowing shared Claude configuration

#### Scenario: The target contains unrelated work

- **GIVEN** the target also contains `.git/` and `notes.txt`
- **WHEN** the guide runs `vecten init`
- **THEN** it exits 4 before rendering and identifies both blocking entries

### Requirement: Check the project name during intake

The name question SHALL state the format accepted by `init`. The name SHALL start with a lowercase
ASCII letter and contain at most 63 lowercase ASCII letters, digits, hyphens or underscores.

#### Scenario: Correct a project name during intake

- **WHEN** the human answers `Acme Data Warehouse`
- **THEN** `vecten answer name` exits 2, explains the format and preserves the recorded answers
- **AND** the human can answer `acme-data-warehouse` and continue the intake
