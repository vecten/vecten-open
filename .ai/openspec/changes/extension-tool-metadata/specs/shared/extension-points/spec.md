## ADDED Requirements

### Requirement: Extension tools may describe themselves to MCP clients

An entry in `vecten.mcp.tools` MAY declare a string `title`, a string `description` and an
`annotations` dictionary of Model Context Protocol tool hints. The server SHALL include
the supplied metadata in the tool listing for callers who can use the tool. Loading the
extensions SHALL refuse a declaration with an unknown key, an annotation name MCP does not
define, an empty title or description, or a value of the wrong type.

#### Scenario: A read-only tool supplies a display title

- **WHEN** an authorized caller requests `tools/list` for a tool declaring a title, description and `readOnlyHint`
- **THEN** the listing includes those values and the input schema derived from the handler

#### Scenario: A declaration omits metadata

- **WHEN** the declaration supplies only a handler and its groups
- **THEN** the tool uses the handler's docstring for its description and its signature for its input schema

#### Scenario: A caller lacks access

- **WHEN** a caller outside the declared groups lists or calls an annotated tool
- **THEN** the server omits the tool from discovery and refuses the call

#### Scenario: A declaration misspells its metadata

- **WHEN** a declaration carries `anotations`, or an annotation named `readonlyHint`, or `readOnlyHint: "true"`
- **THEN** the server refuses to load its extensions with a configuration error naming the tool and the field
