## MODIFIED Requirements

### Requirement: The gate signs people in for service routes

NGINX SHALL forward service-host gate start, callback and logout to the standalone login gate. Start SHALL bind a random verifier to a short-lived HttpOnly host cookie and redirect to the MCP issuer. The issuer SHALL authenticate the human and issue a one-time code bound to the known hostname, relative return path and challenge. The gate SHALL redeem it only with the matching verifier, set a host-only session cookie, store its hash and end that session on logout.

#### Scenario: Host-only code and cookie binding

- **WHEN** the gate starts the service login
- **THEN** it sets an HttpOnly state cookie with a random verifier, sends only its hash to `/gate/authorize`, and accepts the same-host callback with a code valid for at most 60 seconds
- **AND** the redeemed session cookie is named `__Host-vecten-gate`, uses Secure, HttpOnly and SameSite=Lax, and has no Domain attribute

#### Scenario: Ineligible principal

- **WHEN** the issuer receives a gate authorization request for a disabled, unprovisioned or service principal
- **THEN** it refuses to issue a code

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
