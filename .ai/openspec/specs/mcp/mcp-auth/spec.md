# mcp/mcp-auth Specification

## Purpose
Authentication of MCP clients, people and service principals: the server's own OAuth 2.1 authorization server, pluggable identity backends, the client credentials grant for headless agents, consent and connected-client revocation.

## Requirements

### Requirement: OAuth 2.1 authorization server with dynamic client registration
The server SHALL act as its own OAuth 2.1 authorization server and SHALL accept dynamic client registration, so that remote MCP clients can connect without pre-shared client credentials. It SHALL register public clients (`none`) and confidential ones (`client_secret_post`, `client_secret_basic`), issuing the latter a secret it stores only as a hash and checks on every authorization-code and refresh-token request; a registered client SHALL never hold the `client_credentials` grant, which belongs to service principals an admin creates. A refused registration SHALL answer with the RFC 7591 error code and a description. Every MCP request MUST present an access token issued by this server.

#### Scenario: Remote MCP client connects for the first time
- **WHEN** an MCP client that has never connected registers itself dynamically and completes the authorization flow for a person
- **THEN** the server issues an access token bound to that person
- **AND** subsequent tool calls carrying that token are served

#### Scenario: Client registers as a confidential client
- **WHEN** a client such as Claude registers with `token_endpoint_auth_method` `client_secret_post`
- **THEN** the registration answers 201 with a `client_secret`
- **AND** a token request for that client without the secret is refused with `invalid_client`

#### Scenario: Request without a valid token
- **WHEN** an MCP request arrives with no access token, an expired token, or a token the server did not issue
- **THEN** the server rejects the request with an authentication error
- **AND** no tool runs and no warehouse connection is opened

### Requirement: Identity backends are plugins
The server SHALL resolve a person's identity through an identity backend selected at deploy time from the `vecten.mcp.auth` entry point group. The open edition SHALL ship a `password` backend and an `oidc` backend against a single provider. A backend MUST return the person's identity only: issuer, subject, email and display name. Group membership SHALL live in `mcp_state` and is managed by admins; an Enterprise directory-sync extension MAY write it.

#### Scenario: OIDC login against one provider
- **WHEN** the deployment is configured with the `oidc` backend pointing at a Google Workspace or Entra ID tenant and a person signs in
- **THEN** the person is authenticated through that provider
- **AND** the session carries the person's identity from the provider and the groups recorded in `mcp_state`

#### Scenario: Backend not installed
- **WHEN** the deployment names an identity backend that no installed plugin provides
- **THEN** the server refuses to start and reports the missing backend by name

### Requirement: OIDC is the default on a cloud deployment
A deployed environment SHALL use the `oidc` backend unless `[mcp] auth = "password"` is set in `vecten.toml` explicitly. The `password` backend SHALL be the default only under `up`. When it is used, passwords SHALL be stored with argon2id, logins SHALL be rate-limited per principal and per source address, and a person SHALL set their password through a one-time link rather than receive one.

#### Scenario: Deployed without an OIDC client
- **WHEN** a project with `mcp` is deployed with no OIDC issuer and client configured and no password opt-in
- **THEN** the server starts, refuses every login, and `next` reports an `ask` item for the OIDC issuer and client id

#### Scenario: Password opt-in on cloud
- **WHEN** `[mcp] auth = "password"` is set and the environment is deployed
- **THEN** logins use the password backend with the stated protections

### Requirement: The first admin's password link
In password mode on a deployed environment, `vecten mcp admin-link --env <env> [--email <admin>]` SHALL issue a one-time set-password link for an address in `[mcp] admins`, the first one by default, with the operator's cloud credentials: it SHALL store a random token, the address and the issue time as the secret `MCP_ADMIN_LINK` and print the page `https://mcp.<domain>/set-password` and, beside it and never in its URL, the token. The page SHALL count each submission against the per-address limit that logins use. The server SHALL read `MCP_ADMIN_LINK` when a link is redeemed, not at startup, and SHALL accept a token only if it matches the stored one, fewer than 15 minutes have passed since issue, the address is still in `[mcp] admins`, and the token was not redeemed before; it SHALL invite the admin when no principal exists and record the token as redeemed with the password. The command SHALL refuse, before writing any secret, an environment not deployed, a local environment, OIDC sign-in, and an address outside `[mcp] admins`. On the laptop the first admin's link SHALL come from `MCP_ADMIN_SETUP_TOKEN`, read when the server starts.

#### Scenario: First admin on cloud
- **WHEN** `vecten mcp admin-link --env prod` runs in password mode with `ada@acme.com` first in `[mcp] admins`
- **THEN** it prints the set-password page and a token, and Ada can set her password through it once, within 15 minutes, without a restart of the server

#### Scenario: A newer link
- **WHEN** `admin-link` runs twice
- **THEN** only the newer link sets a password

### Requirement: Token handling
Authorization code flows SHALL require PKCE with S256 and exact redirect URI matching. Access tokens SHALL be bound to the MCP endpoint as audience, SHALL live at most one hour, and refresh SHALL require fresh authentication with the backend once a maximum session lifetime, seven days by default, has passed. Tokens and client secrets SHALL be stored hashed.

#### Scenario: Offboarded person
- **WHEN** a person is removed from the identity provider
- **THEN** their access token stops working within an hour and no refresh succeeds past the session lifetime

#### Scenario: Token presented to another service
- **WHEN** an access token issued for the MCP endpoint is presented to a different resource server
- **THEN** that server rejects it on audience

### Requirement: Signing key and client registrations
The signing key SHALL live in the secrets store at `mcp/signing_key` and rotate every thirty days with the previous key published until its tokens expire. The next key SHALL be published in the JWKS seven days before it signs, because Snowflake caches an External OAuth JWKS per URL and fetches no key it has not seen. Dynamic client registration SHALL be rate-limited and registrations never used within a day SHALL be pruned.

#### Scenario: Key rotation
- **WHEN** the signing key rotates
- **THEN** tokens signed with the previous key verify until they expire and new tokens carry the new key id

#### Scenario: Next key published ahead
- **WHEN** the signing key is twenty-three days old
- **THEN** the JWKS lists the next key beside the current one while tokens are still signed with the current key, and from day thirty tokens carry the next key's id

#### Scenario: Registration flood
- **WHEN** one address registers clients faster than the limit
- **THEN** further registrations are refused for the window and never-used registrations are pruned after a day

### Requirement: The CLI authenticates as a person
`vecten login` SHALL obtain a token for the person running it through the device authorization grant, or a loopback redirect when a browser is available, against the same authorization server, and SHALL cache it locally outside the project. Admin API calls made by the CLI SHALL carry that token, so they are audited under the person.

#### Scenario: Admin runs user add from the laptop
- **WHEN** an admin runs `vecten user add` with a cached token
- **THEN** the admin API records the access event under that admin's principal

#### Scenario: No token
- **WHEN** a `vecten user`, `group` or `grant` command runs with no cached token
- **THEN** it exits non-zero naming `vecten login`

### Requirement: Client credentials grant for service principals
A `svc_<name>` service principal SHALL authenticate with the OAuth 2.1 client credentials grant using a client id and secret an admin issued for it. The token SHALL be bound to that principal, and its tool calls SHALL run as that principal's own warehouse user. Human principals MUST NOT be issued client credentials.

#### Scenario: Headless agent connects
- **WHEN** a scheduled agent registered as `svc_reporting` presents its client id and secret to the token endpoint
- **THEN** the server issues a token bound to `svc_reporting`
- **AND** its `sql-exec` calls run as the `svc_reporting` warehouse user and are audited under that principal

#### Scenario: Human principal attempts client credentials
- **WHEN** a client requests a token through the client credentials grant for a human principal
- **THEN** the token endpoint refuses
- **AND** the refusal states that human principals authenticate through the authorization code flow

### Requirement: Login page
The server SHALL render a login page as part of the authorization flow for backends that need the person to enter credentials or be redirected to a provider.

#### Scenario: Password login
- **WHEN** an authorization flow starts under the `password` backend
- **THEN** the person is shown a login page
- **AND** a wrong password returns to the page with an error and issues no token

### Requirement: Consent page
Before the server issues a token to a client, the person MUST be shown a consent page naming the client and MUST approve it. A declined consent SHALL issue no token.

#### Scenario: Person approves a new client
- **WHEN** a person authenticates and the requesting client has not been approved by that person before
- **THEN** a consent page shows the client's name
- **AND** approval completes the flow with a token
- **AND** denial ends the flow without a token

### Requirement: Session resolves principal and groups
On every tool call the server SHALL resolve the access token to the principal, human or service, and that principal's groups before any tool is selected or run.

#### Scenario: Group membership changes between calls
- **WHEN** an admin removes a person from a group and that person makes a later tool call with an existing token
- **THEN** the call is served with the person's current groups, not the groups at login

### Requirement: Connected clients page with revocation
The server SHALL render a page listing the clients a person has authorized, and the person SHALL be able to revoke any of them. A revoked client's tokens MUST stop working immediately.

#### Scenario: Person revokes a client
- **WHEN** a person revokes a connected client
- **THEN** the next request from that client with its existing token is rejected as unauthenticated
- **AND** the revocation is recorded as an access event

### Requirement: Streamable HTTP transport only
The MCP endpoint SHALL be served over streamable HTTP. The legacy SSE transport MUST NOT be offered.

#### Scenario: Legacy SSE client
- **WHEN** a client attempts to connect using the legacy SSE transport
- **THEN** the connection is refused
- **AND** the streamable HTTP endpoint remains available to other clients

### Requirement: Rate limits count the client's own address

Every limit the server keys by source address SHALL use the address of the client that connected to the front door, not the front door's own. The front door SHALL replace any forwarded-address header the client sent with the address it sees. The server SHALL take a forwarded address only from a peer in a private or loopback range, and otherwise use the connecting peer's address.

#### Scenario: Two clients behind the front door

- **WHEN** one client exhausts the per-address limit on the set-password page through the front door
- **THEN** a second client at another address can still log in and request tokens through the same front door

#### Scenario: Spoofed forwarded header

- **WHEN** a client sends its own forwarded-address header through the front door
- **THEN** the server counts the request against the address the front door saw

#### Scenario: Forwarded header from a public peer

- **WHEN** a peer outside the private and loopback ranges connects to the server directly with a forwarded-address header
- **THEN** the server counts the request against that peer's own address

### Requirement: Setting a password ends the person's other sessions

When a person sets a password through a one-time link or the first admin's link, the server SHALL revoke every OAuth session the person holds, end every browser session the person holds, and invalidate every other pending set-password link issued for the person, before the request that sets the password returns. Revoked sessions' access and refresh tokens MUST stop working immediately.

#### Scenario: Stolen tokens after a reset

- **WHEN** a person who holds an access token and a refresh token sets a new password through a link
- **THEN** the access token is refused and the refresh token yields no new tokens

#### Scenario: Browser session after a reset

- **WHEN** a person signed in to the browser pages sets a new password through a link
- **THEN** the earlier browser session no longer signs them in

#### Scenario: Older reset link

- **WHEN** two set-password links are issued for a person and the newer one is used
- **THEN** the older link is refused
