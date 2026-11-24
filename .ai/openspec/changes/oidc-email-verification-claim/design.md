# Design

Allow `email_verified` and `xms_edov` in the project configuration and the intake CLI.
Use that selection in the built-in MCP OIDC backend and the Airflow OIDC auth manager. Check that exact field in
the signed ID token at the existing verification step. Use a literal boolean check so a
string or number cannot satisfy it. Entra's claim verifies domain ownership. First login
also relies on the tenant administrator assigning the correct email to each account.
