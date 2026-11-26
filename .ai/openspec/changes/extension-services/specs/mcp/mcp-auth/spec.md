## ADDED Requirements

### Requirement: The gate signs people in for service routes

NGINX SHALL proxy service-host start, callback and logout to the standalone gate and packaged `/_vecten/assets/` files to the gate upstream. Start SHALL bind a random verifier to a short-lived HttpOnly host cookie and redirect to the MCP issuer. The issuer SHALL authenticate the user and bind a one-time code to the known host, relative return path and challenge. The gate SHALL redeem it only with the matching verifier, set a host-only session cookie, store its hash and revoke it on logout.

#### Scenario: First visit

- **WHEN** Ada, not signed in anywhere, opens `https://portal.<domain>/docs` on a `login` route
- **THEN** she signs in on `mcp.<domain>` and lands on `https://portal.<domain>/docs` with a gate cookie for `portal.<domain>` only

#### Scenario: The sign-out page loads after the session ends

- **WHEN** a signed-out browser requests the sign-out page's favicon, stylesheet or fonts on a service hostname with a `login` route
- **THEN** NGINX forwards only `GET` and `HEAD` for the packaged files to fixed upstream paths without client credentials or a sign-in check
- **AND** unknown paths under `/_vecten/assets/` return 404

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

The standalone gate SHALL verify the service cookie and bound route against the active principal and current memberships. It SHALL return 200 and verified identity headers when admitted, 401 without a valid session, or 403 when outside the groups. NGINX SHALL reach it over the local bridge or verified HTTPS through the MCP host. Verification SHALL be exempt from per-address limits. Logout, disable, deletion and expiry SHALL end access.

#### Scenario: Trusted verification response

- **WHEN** NGINX verifies an admitted person's session
- **THEN** the gate returns only the defined identity headers for the person's email, principal ID, display name, current groups and administrator status
- **AND** an invalid session's response contains no principal details

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
