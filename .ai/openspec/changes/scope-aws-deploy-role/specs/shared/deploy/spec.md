## ADDED Requirements

### Requirement: The AWS CI deploy role is scoped to one environment

AWS bootstrap SHALL grant only the non-IAM permissions needed for this environment's Terraform and ship operations, constrained by resources, tags, prefixes and DNS records wherever supported. Every wildcard-resource action SHALL have a documented reason. Bootstrap SHALL preserve the conditioned host IAM policy and separate plan role. A new or successfully migrated deploy role SHALL have no PowerUserAccess attachment.

#### Scenario: First bootstrap and deploy

- **WHEN** the operator bootstraps a new environment and an authorized workflow applies it and ships the first bundle
- **THEN** the job provisions the environment, writes its state and outputs, and delivers the bundle using the scoped role
- **AND** the deploy role never receives PowerUserAccess

#### Scenario: Resume an existing deployment's migration

- **WHEN** bootstrap is interrupted while reconciling an existing role and the operator reruns it
- **THEN** it installs and verifies the intended scoped grants before detaching the legacy policy
- **AND** it keeps the role identity and conditioned IAM rules, reports residual broad grants, and never restores broad access as an automatic fallback

#### Scenario: Sibling environment

- **WHEN** a scoped deploy session requests another environment's secret value, state object, DNS record change, resource ownership change or SSM command execution
- **THEN** each request is denied even when the sibling's name begins with this environment's name

#### Scenario: Replacement, restore and teardown

- **WHEN** an authorized environment undergoes a host replacement, adopts an operator-restored database and is later destroyed through the operator runbook
- **THEN** the scoped deploy session can reconcile and ship after each infrastructure change using its existing conditioned IAM actions to update the host policy for the restored database, with no increase in the deploy role’s own permissions
- **AND** teardown preserves the state bucket and retained snapshots, and the sibling environment remains intact
