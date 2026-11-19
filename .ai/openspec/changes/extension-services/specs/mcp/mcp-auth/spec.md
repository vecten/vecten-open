## ADDED Requirements

### Requirement: The gate signs people in for service routes

The server SHALL serve a gate for every hostname that a `login` route binds in its own copy of `vecten.toml`. On such a hostname, NGINX SHALL forward `/_vecten/gate/start`, `/_vecten/gate/callback` and `/_vecten/gate/logout` to the server. `start` SHALL set a short-lived HttpOnly state cookie on that hostname holding a random verifier, and redirect to the server's `/gate/authorize` on `mcp.<domain>` with the hostname, the verifier's hash and the return path, never the verifier. `authorize` SHALL sign the person in through the configured identity backend, or use the person's existing browser session on the server, and SHALL redirect back to `/_vecten/gate/callback` on the same hostname with a one-time code bound to that hostname and that hash and valid for at most 60 seconds. The callback SHALL redeem the code only with a verifier from the state cookie whose hash matches, create a gate session for the principal in `mcp_state`, set the cookie `__Host-vecten-gate` (Secure, HttpOnly, SameSite=Lax, no Domain) holding an opaque random session id, and redirect to the return path. The server SHALL refuse `authorize` for a hostname no `login` route binds, a return path that is not a path on that hostname, and a principal that is disabled or not provisioned as a person; service principals SHALL NOT receive gate sessions. Gate session ids SHALL be stored only as hashes. `logout` SHALL end the gate session and clear the cookie.

#### Scenario: First visit

- **WHEN** Ada, not signed in anywhere, opens `https://portal.<domain>/docs` on a `login` route
- **THEN** she signs in on `mcp.<domain>` and lands on `https://portal.<domain>/docs` with a gate cookie for `portal.<domain>` only

#### Scenario: Second service, same browser

- **WHEN** Ada, signed in to the server's browser pages, then opens `https://dash.<domain>/`
- **THEN** she reaches the dashboard without entering credentials again

#### Scenario: Open redirect attempt

- **WHEN** a link sends a browser to `/gate/authorize` for `evil.example` or with a return path of `https://evil.example/`
- **THEN** the server refuses it and issues no code

#### Scenario: Code replayed

- **WHEN** a callback code is presented a second time, or on another hostname
- **THEN** the server refuses it and creates no session

#### Scenario: Leaked callback URL

- **WHEN** someone else's client presents a valid, unused callback URL without the state cookie of the browser that started the flow
- **THEN** the server refuses it and creates no session

### Requirement: The gate verifies every request with live groups

The server SHALL offer NGINX a verify operation that takes the request's gate cookie and the bound route. It SHALL answer 200 with the principal's email, id, display name, current groups and admin flag when the gate session exists for that hostname, has not expired and the principal is active and belongs to at least one of the route's groups in its own copy of `vecten.toml`; 401 when there is no valid session; and 403 when the person is outside the groups. Under `standard`, NGINX on another VM SHALL reach the operation over HTTPS at `mcp.<domain>` with the server's certificate verified; on the MCP's own VM it SHALL reach it on the Compose network. The operation SHALL be exempt from per-address rate limits and SHALL return nothing about a principal for an invalid session. A gate session SHALL end at the server's maximum session lifetime, when the principal is disabled or deleted, and on logout.

#### Scenario: Person disabled

- **WHEN** an admin disables Bob while he has a gate session
- **THEN** Bob's next request on any `login` route is redirected to sign in, and signing in fails

#### Scenario: Route groups changed by a ship

- **WHEN** a ship changes a route's `groups` from `["default"]` to `["finance"]`
- **THEN** the next request of a person outside `finance` on that route is answered 403

## MODIFIED Requirements

### Requirement: Setting a password ends the person's other sessions

When a person sets a password through a one-time link or the first admin's link, the server SHALL revoke every OAuth session the person holds, end every browser session and every gate session the person holds, and invalidate every other pending set-password link issued for the person, before the request that sets the password returns. Revoked sessions' access and refresh tokens MUST stop working immediately.

#### Scenario: Stolen tokens after a reset

- **WHEN** a person who holds an access token and a refresh token sets a new password through a link
- **THEN** the access token is refused and the refresh token yields no new tokens

#### Scenario: Browser session after a reset

- **WHEN** a person signed in to the browser pages sets a new password through a link
- **THEN** the earlier browser session no longer signs them in

#### Scenario: Gate session after a reset

- **WHEN** a person with a gate session on `portal.<domain>` sets a new password through a link
- **THEN** the next request with that gate cookie is redirected to sign in

#### Scenario: Older reset link

- **WHEN** two set-password links are issued for a person and the newer one is used
- **THEN** the older link is refused
