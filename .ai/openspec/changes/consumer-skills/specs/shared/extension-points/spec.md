## ADDED Requirements

### Requirement: Skills for people who use the data

A listed extension SHALL be able to export a directory containing SKILL.md and an explicit list of supporting files through a named vecten.consumer_skills entry point. The CLI SHALL package those files in a ZIP under the skill name. The MCP SHALL list and serve the same archive to signed-in members of the skill’s configured groups, checking current membership for every download. Every authenticated download attempt SHALL record the skill name and the access decision. Skill bindings SHALL require the MCP component and a valid skill name. Both locking and checking the project SHALL refuse a bound skill whose installed extension cannot produce its archive.

#### Scenario: A skill author packages a release

- **WHEN** an author runs `vecten extension pack-skill monthly-orders --output monthly-orders.zip` with the declaring extension installed
- **THEN** the archive contains its SKILL.md and its listed files, and a pre-existing destination is preserved

#### Scenario: An analyst loses access

- **WHEN** a signed-in analyst loses the last group that allows downloading a skill
- **THEN** the skill disappears from their apps page and its download URL returns the same response as an unknown skill
