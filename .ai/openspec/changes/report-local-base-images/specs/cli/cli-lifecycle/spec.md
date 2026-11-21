## ADDED Requirements

### Requirement: Local stack reports its base images

A successful `vecten up` SHALL report the base image reference and source (local build or registry) for each component it started in both human and JSON output. When local build tags are selected it SHALL name the registry override for the published images.

#### Scenario: A contributor has local builds

- **WHEN** the stack selects its unprefixed local images
- **THEN** the output names each active component’s tag and explains how to select the published images

#### Scenario: Registry or omitted component

- **WHEN** the stack uses the published or an explicitly configured registry
- **THEN** the output contains the exact registry references and lists only locally active components
