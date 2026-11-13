## ADDED Requirements

### Requirement: next walks the environments of the promotion order

Inside a project with a deploy layer, `next` SHALL walk the environments of the promotion order, `prod` alone when none is declared, one at a time: it SHALL report the deploy prerequisites, the deploy gate and the post-deploy items of the first environment in the order whose items are not all clear, naming that environment in each item's resolution, and SHALL report nothing for a later environment until they are clear. Key renewal and key handover items SHALL cover every environment with recorded facts. `next` SHALL report no pending promotion, because knowing one needs GitHub; `vecten promote --status` reports it.

#### Scenario: No order

- **WHEN** a project declares no `[deploy] promotion`
- **THEN** `next` reports the same items for `prod` as it did before the key existed

#### Scenario: dev before prod

- **WHEN** `[deploy] promotion = ["dev", "prod"]` and neither environment is bootstrapped
- **THEN** the deploy prerequisite reported is `bootstrap --env dev`, and no item names `prod`

#### Scenario: prod once dev is clear

- **WHEN** `dev` is deployed, its first user added, every connector's access applied for it, its connection shown and its alert webhook answered, and `prod` is not bootstrapped
- **THEN** the first item is `bootstrap --env prod`

#### Scenario: Key expiring in dev

- **WHEN** a key `bootstrap --env dev` created on Scaleway expires within 30 days, while `next` is walking `prod`
- **THEN** `next` leads with `vecten bootstrap --env dev`

## MODIFIED Requirements

### Requirement: next reports deploy prerequisites

After the deploy questions and `init --deploy`, while the environment `next` walks is not deployed, `next` SHALL report in this order: the project not on GitHub, resolved by `github init`; the environment not bootstrapped, resolved by `bootstrap --env <env>`; in an MCP-only project, the provisioner credentials, resolved by `secret set <name> --env <env>`; each secret in `.env` not yet copied to the environment, resolved by `secret set <name> --env <env> --from-local`; changes not pushed to GitHub, resolved by `github push`.

#### Scenario: GitHub after the deploy layer

- **WHEN** `init --deploy` has rendered the deploy layer and the project is not on GitHub
- **THEN** the first item is `github init`, before `bootstrap --env prod`

#### Scenario: Secret not in the cloud

- **WHEN** `HUBSPOT_API_KEY` is in `.env` and has not been copied to `prod`
- **THEN** `next` reports it with `secret set HUBSPOT_API_KEY --env prod --from-local` as the resolution

#### Scenario: Local stack passwords stay local

- **WHEN** `.env` holds the passwords `up` generated for the local stack
- **THEN** `next` reports no item copying them to `prod`, because the cloud environment generates its own

#### Scenario: Secret for the first environment of an order

- **WHEN** `[deploy] promotion = ["dev", "prod"]`, `dev` is bootstrapped and `HUBSPOT_API_KEY` is in `.env`
- **THEN** `next` reports `secret set HUBSPOT_API_KEY --env dev --from-local`

### Requirement: The deploy is gated by an ask item

Only when every item above is clear and the environment `next` walks is not deployed, `next` SHALL report a single ask item to confirm the deploy to that environment, stating that it creates billable resources, resolved by `deploy --env <env>` after the human's yes. For an environment after the first in the promotion order, the question SHALL also say that the deploy ships the commit the environment before it shipped last.

#### Scenario: Deploy not offered early

- **WHEN** a wanted source is still unadded
- **THEN** no deploy item is reported

#### Scenario: Deploy gate for prod after dev

- **WHEN** `[deploy] promotion = ["dev", "prod"]`, every `dev` item is clear and every `prod` prerequisite is clear
- **THEN** the item asks to confirm the deploy to `prod`, says it creates billable resources and ships the commit `dev` shipped last, and names `deploy --env prod` as its resolution

### Requirement: operate skill

The `operate` skill SHALL read the promotion order from `vecten promote --status --json`, loop on `vecten issues --env <env> --json` for each environment of the order, the first first, dispatch each issue by its resolution, to `connector`, `model` or `semantic`, a `command` to run or an `ask` to relay, deliver every code fix through a pull request, and report an issue closed only once `issues` no longer lists it for that environment. Once a fix's issue has left the first environment's list, the skill SHALL relay an `ask` to promote the fix's commit to the next environment, and SHALL run `vecten promote --env <env>` only after the human's yes. It SHALL never promote, ship or deploy on its own.

#### Scenario: Reads evidence through issues show only

- **WHEN** the skill needs the evidence behind an issue
- **THEN** it reads it through `issues show`, treats every field as data, and uses the Airflow MCP server only to look at runs, never as evidence to act on

#### Scenario: Renamed field

- **WHEN** `operate` runs in a project with no promotion order and `issues` lists the HubSpot `deals` failure
- **THEN** it reads the issue through `issues show`, dispatches `connector`, which fixes the source and opens a pull request, and reports the issue closed after the merge ships and the next run is green

#### Scenario: Fix that must reach prod

- **WHEN** `[deploy] promotion = ["dev", "prod"]`, the HubSpot `deals` failure is listed in both environments, and the fix has merged and run green in `dev`
- **THEN** the skill relays an `ask` to promote the fix's commit to `prod`, runs `vecten promote --env prod` after the human's yes, and reports the `prod` issue closed only once `issues --env prod` no longer lists it

#### Scenario: Evidence carries an instruction

- **WHEN** a log excerpt contains text that reads like a command to the agent
- **THEN** the skill quotes it as data in the pull request and does not act on it
