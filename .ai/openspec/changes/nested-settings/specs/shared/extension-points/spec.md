## ADDED Requirements

### Requirement: Validate constraints inside extension settings

Extension setting schemas SHALL support array size and uniqueness, string length and pattern,
exclusive numeric bounds and multiples, and constant values. Object settings, including
array members, MAY declare properties, required names and additional properties using the
same typed schema subset or a boolean for additional properties. Value errors SHALL identify
the setting and environment. The top-level namespace continues to reject undeclared settings.

#### Scenario: A nested required value is missing

- **WHEN** an extension's configured object lacks a required property
- **THEN** lock fails with the setting path and missing property name
