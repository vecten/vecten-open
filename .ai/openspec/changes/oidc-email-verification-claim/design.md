# Design

Pass the configured claim name to the built-in MCP OIDC backend. Check that exact field in
the signed ID token at the existing verification step. Use a literal boolean check so a
string or number cannot satisfy it. Entra's claim verifies domain ownership. First login
also relies on the tenant administrator assigning the correct email to each account.
