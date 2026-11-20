# Initialize a project in the guide's working directory

Claude Code and Finder can create metadata before the guide finishes intake. Creating the
project in a subfolder leaves its MCP configuration and instructions outside the session root.
Allow the session metadata during initialization and report the names of any blocking entries.
Check the project name when the guide asks for it so the human can correct it immediately.

Addresses [issue 32](https://github.com/vecten/vecten-open/issues/32).
