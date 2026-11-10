# mcp/admin-panel Specification

## Purpose
The admin panel: the screens an admin uses to run the deployment, served by the same process and the same service layer as the admin tools and admin API.

## Requirements

### Requirement: Panel is served by the same process behind admin login
The panel SHALL be served by the same process as the MCP endpoint, under `/admin`, with `SameSite=Strict` cookies and CSRF tokens, and SHALL require an authenticated person with the admin flag. A deployment setting SHALL allow an NGINX allow-list on `/admin` and taking `/admin` off the public listener.

#### Scenario: Non-admin opens the panel
- **WHEN** a person without the admin flag opens a panel URL
- **THEN** access is refused

### Requirement: Users and groups screen
The panel SHALL let an admin manage people, service principals and groups through the admin API: create a person from email and display name, set and clear the admin flag, create and delete groups and membership, create `svc_<name>` principals with their client credentials, and provision, disable and delete a principal's warehouse user. Adoption follows the warehouse-identity rules.

#### Scenario: Provision from the panel
- **WHEN** an admin provisions a person from the users screen and no account of that name exists
- **THEN** the person's warehouse user is created and the screen shows it as active

#### Scenario: Existing account found
- **WHEN** an admin provisions a person and an unbound warehouse account of the derived name already exists
- **THEN** the screen states that the account exists and asks the admin to confirm adoption
- **AND** nothing changes in the warehouse until the admin confirms

#### Scenario: Service principal created
- **WHEN** an admin creates the service principal `reporting` from the users screen
- **THEN** `svc_reporting` is provisioned and its client id and secret are shown once

### Requirement: Grants and deny lists screen
The panel SHALL let an admin grant and revoke datasets and convention schemas per group, edit each group's column deny list, and run the grant reconciliation.

#### Scenario: Deny a column
- **WHEN** an admin adds `customers.email` to the `analysts` deny list
- **THEN** the next `describe` by an analyst omits that column

### Requirement: Model view is read-only
The panel SHALL show the loaded model, its health as errors and warnings including "shipped, waiting for build", usage per field and metric, and the gap list. It MUST NOT edit definitions and SHALL offer no form that writes to the repository; the `semantic` skill in a checkout is the write path.

#### Scenario: Admin wants a description changed
- **WHEN** an admin opens a field in the model view
- **THEN** the view shows where the description lives in `schema.yml` and names the `semantic` skill, and offers no edit

### Requirement: Gaps and feedback screen
The panel SHALL list gaps with their evidence and let an admin set a gap's status. A gap whose fix merged without an approving review SHALL show why it stays open and offer the admin a confirmation of that fix.

#### Scenario: Triage a gap
- **WHEN** an admin opens a gap and sets it to `triaged`
- **THEN** the status is saved and shown in the list

#### Scenario: Confirm an unreviewed fix
- **WHEN** an admin opens a gap whose fix merged without an approving review
- **THEN** the page reads `merged without an approving review; the gap stays open`, names the pull request and its author, and offers a button that confirms the fix in place of a review

### Requirement: Audit browser and connected clients
The panel SHALL let an admin browse audit rows by user, tool, resolution and time, follow a request chain, and list the connected clients across people.

#### Scenario: Follow a chain
- **WHEN** an admin opens a refused row
- **THEN** the row's chain shows the `sql-exec` call that followed it

### Requirement: Settings screen
The panel SHALL expose deployment settings including audit retention and the redaction toggle, with their current values and defaults.

#### Scenario: Change retention
- **WHEN** an admin sets retention to 30 days
- **THEN** the setting is saved and audit expiry uses 30 days

### Requirement: Panel, admin tools and admin API share one service layer
Every action available in the panel SHALL be available through the admin API with the same effect, so that a different frontend can replace the panel without changing behavior. Read operations SHALL also be admin tools; mutations MUST NOT be.

#### Scenario: Same action, two surfaces
- **WHEN** a gap status is set through the admin tool and another through the panel
- **THEN** both gaps show the new status in both surfaces

#### Scenario: Mutation through the agent
- **WHEN** an admin asks their agent to disable a person
- **THEN** the agent finds no tool for it and runs `vecten user disable` instead
