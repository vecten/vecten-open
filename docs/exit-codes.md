# Exit codes

Every `vecten` command exits with one of these codes. The set is stable within a minor
release. `vecten dbt <args>` forwards dbt's own exit code and is the one exception.

| Code | Name | Meaning |
|---|---|---|
| 0 | OK | The command did what was asked. |
| 1 | INTERNAL | An unexpected error inside vecten; please report it. |
| 2 | USAGE | Invalid input: an unknown key, a value outside the allowed set, a missing answer. |
| 3 | PREREQUISITE | A tool or login the command needs is missing: Docker, `gh auth login`, a cloud CLI. |
| 4 | REFUSED | The command's preconditions are not met: not in a project, not bootstrapped, a changed cloud. |
| 5 | TOOL_FAILED | An external tool vecten ran exited with an error: docker compose, terraform, gh, dbt. |
| 6 | CONFIG | `vecten.toml` or `vecten-answers.yml` could not be parsed. |

Back to the [docs index](README.md).
