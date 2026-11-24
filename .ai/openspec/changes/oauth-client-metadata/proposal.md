# OAuth client metadata documents

Public MCP clients can host a JSON document at their HTTPS client ID. The authorization endpoint reads the document when the client starts a code flow and checks the requested redirect. Existing registrations continue to use their stored credentials.

The document fetch uses a validated public address and verifies the certificate for the original hostname. It omits credentials, proxy settings and redirects, caps the response at 16 KiB, and bounds concurrent work and elapsed time. The process caches up to 256 documents for up to five minutes, respecting shorter cache lifetimes. Only the display name is recorded for session management.
