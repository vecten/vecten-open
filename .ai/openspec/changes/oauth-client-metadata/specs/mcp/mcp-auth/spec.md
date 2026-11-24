## ADDED Requirements

### Requirement: Public clients identified by HTTPS metadata documents

The authorization server SHALL advertise client ID metadata document support and allow a public client to start an authorization code flow using its HTTPS document URL. It SHALL validate the document identity, name, redirect URIs and supported authentication method, check the requested redirect exactly and show the client and redirect hosts during consent. It SHALL constrain DNS, address selection, TLS, response size, cache lifetime and concurrent requests.

#### Scenario: Public client signs in

- **WHEN** a public client presents its document URL and a listed redirect URI with S256 PKCE
- **THEN** the person can consent and the client can exchange the code

#### Scenario: Unusable client document

- **WHEN** the document cannot be fetched from a public address within the bounds or fails validation
- **THEN** the authorization request fails before a login flow is created
