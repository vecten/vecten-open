## ADDED Requirements

### Requirement: Forwarded requests to the service login gate

A proxy SHALL be able to ask the gate to check access using the original request's
`X-Forwarded-Host`, `X-Forwarded-Uri` and `X-Forwarded-Method`. The gate SHALL match
the longest declared route on the host and verify the gate session against that route's
current admission. Unbound paths, ambiguous paths and public routes sent to this endpoint
SHALL be refused. Public routes remain the proxy's responsibility.

#### Scenario: Proxy requests browser sign-in

- **WHEN** the proxy calls `/gate/verify?forwarded=1&redirect=1` for a browser GET to a bound login route with no session
- **THEN** the gate returns a redirect to sign-in on the bound host, retaining the original path and query

#### Scenario: Proxy checks a restricted route

- **WHEN** a session is valid on a host but the person lacks the group required by the longest matching route
- **THEN** the gate returns 403 without identity headers or a redirect
