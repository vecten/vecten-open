## ADDED Requirements

### Requirement: Explicitly terminal object failures

A connector SHALL be able to mark an object failure as terminal. The sync task SHALL fail on that attempt and run its configured failure callback. Other extraction failures SHALL keep their configured retries.

#### Scenario: A metered source exhausts its credits

- **WHEN** the source explicitly reports a terminal error while extracting an object
- **THEN** Airflow makes one attempt and records the failed object
- **AND** an ordinary temporary error remains eligible for retries
