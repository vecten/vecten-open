An MCP client stands for the human's AI assistant: the command `mcp-client`, on the PATH inside the run wrapper.
- `mcp-client login <url>` registers the client, prints a sign-in page for the human to open, and waits up to 10 minutes for their browser sign-in. Start it in the background and read the page from its output.
- `mcp-client tools <url>` lists the server's tools.
- `mcp-client call <url> <tool> '<json arguments>'` calls one tool and prints its result with its `_meta`.
- The client keeps its own sign-in; you never see a password or token. Run it with no arguments for its usage.
