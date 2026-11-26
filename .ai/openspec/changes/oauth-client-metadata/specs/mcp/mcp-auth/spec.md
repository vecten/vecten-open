## ADDED Requirements

### Requirement: Public clients identified by HTTPS metadata documents

The server SHALL advertise HTTPS client metadata document support, validate the document, constrain outbound requests and display the client and redirect hosts as separate labelled fields on consent.

#### Scenario: Matching the redirect

- **WHEN** the client requests a redirect
- **THEN** it must match a registered URI exactly except for the port on HTTP loopback IP literals (127.0.0.1 or ::1)

#### Scenario: Grants available to the client

- **WHEN** a metadata client asks for a token
- **THEN** the server issues refresh tokens only when its document lists `refresh_token` and refuses the device grant

#### Scenario: Document lists grants the server does not offer

- **WHEN** the document lists `authorization_code` together with grants this server does not issue, such as the JWT bearer grant
- **THEN** the server accepts the document and ignores those grants

#### Scenario: Public client signs in

- **WHEN** a public client presents its document URL and a listed redirect URI with S256 PKCE
- **THEN** the person can consent and the client can exchange the code

#### Scenario: Unusable client document

- **WHEN** the document cannot be fetched from a public address within the bounds or fails validation
- **THEN** the authorization request fails before a login flow is created
