# shared/edition-boundary Specification

## Purpose
The line between Vecten Open and the Enterprise extensions: the open product is complete, exactly three licensed features sit outside it, and they install through the same extension points as any other package.

## Requirements

### Requirement: Vecten Open is the complete product

Vecten Open SHALL include both components, deployment to AWS and Scaleway, password and OIDC login, per-person warehouse identity, audit, usage analytics, the feedback loop, the admin panel and admin tools, every skill, the Ossie generator and the dbt importer. The open code MUST contain no licence key check and no telemetry.

#### Scenario: Fresh open install

- **WHEN** the open packages are deployed with no extension installed
- **THEN** every listed feature works

#### Scenario: Code inspection for gating

- **WHEN** the open repository is searched for licence key checks, feature flags that disable listed features, or outbound telemetry
- **THEN** none is found

### Requirement: Exactly three Enterprise extensions

The Enterprise list SHALL consist of exactly: single sign-on with SAML, Okta and Entra ID group sync, role mapping and SCIM; locked-down provisioning with a just-in-time credential and a script mode; and compliance evidence packaging with signed audit export, a `compliance` profile and `doctor --compliance`. Anything not on this list MUST be open.

#### Scenario: Documentation states the line

- **WHEN** a reader opens the edition documentation
- **THEN** the three Enterprise extensions are listed by name and everything else is stated as open

### Requirement: Enterprise features install through the extension points

Each Enterprise feature SHALL be a package declaring the same entry point groups any extension uses. The open product SHALL carry the hooks and nothing else: no partial, trial or disabled variant of an Enterprise feature.

#### Scenario: SAML backend requested without the extension

- **WHEN** an open deployment names a `saml` identity backend
- **THEN** startup fails with a message naming the backend as not installed
- **AND** no open code path implements any part of SAML

#### Scenario: Extension installed

- **WHEN** the SSO extension is added with `vecten extension add` and the deployment names the `saml` backend
- **THEN** the backend is available with no change to the open code

### Requirement: Compliance mechanisms are open

The append-only audit with retention, the access events table, encryption at rest, TLS, secrets kept out of images, per-person identity, column deny lists and the redaction toggle SHALL be in Vecten Open.

#### Scenario: Open deployment audited

- **WHEN** an auditor inspects an open deployment
- **THEN** every listed mechanism is present without an extension

### Requirement: No code depends on an extension's publisher

Every extension, whoever publishes it, SHALL install through the same extension points. No code path, configuration flag or edition check in the open repository SHALL depend on an extension's publisher, and user-facing documentation SHALL present no edition other than Vecten Open and the Enterprise extensions.

#### Scenario: Extension from any publisher installed

- **WHEN** an extension is added with `vecten extension add`
- **THEN** it is wired like any other extension
- **AND** the open product behaves identically whether or not the extension's publisher is Vecten

#### Scenario: Search for edition switches

- **WHEN** the open repository is searched for an edition switch or a check on an extension's publisher
- **THEN** none is found
