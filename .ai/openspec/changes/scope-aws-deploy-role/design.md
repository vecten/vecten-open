# Proposed replacement for PowerUserAccess

Bootstrap should give the GitHub deploy role enough authority to apply the selected environment and ship bundles to its hosts. Its session still has substantial authority *inside* that environment, including the ability to change host code via SSM. The boundary to prove is the one around other environments, unrelated workloads and account administration. [Current security limitation](../../../../SECURITY.md) · [Permissions inventory](inventory.md)

## Policy construction

Have the operator's `vecten bootstrap --env <env>` derive policy documents from the verified AWS account, environment tfvars, configured project, enabled components, exact resource names and public hosted-zone ID. A bootstrap preflight should fail on ambiguous zones, conflicting ownership, invalid names or missing prerequisites before changing attachments. Validate lengths and characters before interpolating ARN or IAM condition patterns.

1. Separate regional read-only discovery, resource creation, changes to existing resources, object/secret values and command execution. Keep EC2 and RDS discovery to the named Describe/List calls that the pinned provider uses. Constrain wildcard-resource calls to the selected region wherever supported. DNS is global and needs its own statements. Prevent broader list permissions from implicitly granting data reads.
2. On EC2, require both ownership tags on new resources where the API supports request tags. For resources that already exist, require both resource tags on every authorized side of an association. For EC2 APIs supporting tags at creation, require exact `aws:RequestTag/vecten:project` and `aws:RequestTag/vecten:env` values, then permit `ec2:CreateTags` with `ec2:CreateAction` restricted to the required operations. [EC2 tag-on-create authorization](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/supported-iam-actions-tagging.html) For later retagging, limit edits to already-owned resources and protect both ownership keys from removal or reassignment. A caller must never gain control of a sibling by tagging it first. A create permission on a new child must not authorize using an unrelated parent. Check the AWS action tables per resource type. Some provider calls act on untaggable associations or AWS-created defaults. If an operation cannot be safely expressed using tags, bind it to a verified parent/resource ID or move that operation to operator bootstrap. Record any residual exposure before accepting a broader grant. [EC2 authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonec2.html)
3. Use exact bucket names, RDS identifiers and the slash-delimited secret prefix wherever names are available. For `RunInstances`, separate image and existing network inputs from new tagged resources. Resolve the supported public AMI family through the existing SSM parameter and restrict allowable image ownership. RDS may need service-mediated operations against dynamically named secrets when a new DB has no known resource ID yet. Verify authorization context and independent denial of direct access before choosing a condition. Retain dedicated-account guidance for any unproven exception.
4. Bind DNS writes to the single zone ARN and the exact normalized record names from the desired environment configuration. For a rename or removal, bootstrap authorizes the union of previously managed and desired records for one apply, then reconciles down to the desired list. Record the last operator-authorized zone IDs and normalized names in the bootstrap-owned policy statements. Treat that list as the ceiling for old records. Terraform state, which CI can write, cannot expand it. During initial adoption obtain old names from the operator-approved deployment configuration and verify the live A records point to its known host addresses. Require explicit ownership resolution for a collision. A deploy job cannot widen its own list. Adding, renaming or removing a service hostname therefore requires an operator to rerun `vecten bootstrap --env <env>` with the desired configuration before apply. Have deploy compare the planned records with the operator-authorized list before applying and name the bootstrap command on mismatch. Include this prerequisite in the generated runbook and AWS guide. A hosted-zone or domain move requires operator reconciliation covering the old and new targets. The extension label `@` means the environment domain itself. It is allowed only when that domain is below the DNS zone, so an extension cannot claim the zone apex. Preserve the enabled-component and reserved-label checks. [Input validation](../../../../terraform/aws/variables.tf)
5. Keep `vecten-deploy-iam` (the conditioned IAM policy allowing Terraform to manage this environment's host roles), the OIDC trust rules and the PR plan policy. Narrow the host permissions boundary as described below. Use operator-managed customer policies split by service group if necessary. Test the 63-character project name, 32-character environment name, both supported components, 63-character DNS labels and enough bound names to reach each policy size limit. Count the rendered characters and attachments. [Module input limits](../../../../terraform/aws/variables.tf) IAM limits a customer-managed policy to 6,144 characters excluding whitespace. For a project with more DNS records than fit, report the failing limit before changing policies. AWS's policy generator, Access Analyzer and policy simulator can check syntax and individual decisions, but they cannot exercise a Terraform apply. [IAM quotas](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html) · [IAM policy validation](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-policy-validation.html)

## Limit what CI can grant a host

The GitHub deploy role can update the inline policy of its environment’s EC2 host role
and send commands to that host. Its
effective reach therefore includes anything the operator-managed host permissions
boundary allows. Change the boundary at bootstrap as part of the same migration.

- **RDS-managed master secrets.** Only the current state database and, when configured,
  the managed warehouse database belong in this environment's master-read allowance.
  Prefer a condition tying a secret to the exact owning DB instance ARNs only if an
  approved sandbox verifies that AWS supplies an immutable ownership attribute on a
  previously deployed database, a newly created database and a restored database. Test
  actual authorization and ensure CI cannot change the ownership association. The
  existence of a tag in a listing does not establish that IAM enforces it.
- **When ownership cannot be proved at request time**, bootstrap limits the boundary to
  exact master secret ARNs. The operator compares the DB identifier against the project and environment’s
  [configured state and warehouse identifiers](../../../../terraform/aws/postgres.tf)
  in the expected AWS account and region, and reads its secret ARN from RDS metadata. CI-controlled Terraform outputs cannot
  authorize a new ARN. On first creation, pause after provisioning RDS and before the host runs
  database setup. Direct the operator to run `vecten bootstrap --env <env>` to
  authorize the verified ARN, then retry `vecten deploy --env <env>` to finish setup
  and ship. After a restore,
  bootstrap must replace the old ARN with the verified replacement before shipping.
  Leave master reads denied until authorization succeeds. Test that the retired ARN is
  denied after cutover.
- **CloudWatch Logs.** Remove unused host log writes. For any required write, document
  its caller and limit the boundary to the verified group and its streams in this
  account and region. Deny writes to the sibling's log group.

The deploy role retains its existing right to update a host's inline policy within that
boundary. Operator bootstrap alone can change the boundary. Apply the same master-read
limit to the setup host in the [setup/runtime identity design](https://github.com/vecten/vecten-open/pull/149).

## Reconcile new and existing environments

- **Fresh bootstrap.** Install the narrowed host boundary and only the scoped non-IAM policies plus the existing IAM rules on the environment deploy role. Read back the role and expected policy versions. The plan role, host profiles and operator credentials serve their existing purposes.
- **Existing role.** Quiesce CI operations against the environment. Inventory the exact legacy attachment, Vecten-managed documents and other grants. Check existing resource tags, adopted databases and DNS records before reducing permissions. Have the operator verify any missing ownership tags before repairing them. A deployment session must never be allowed to claim an untagged resource. Report unknown attachments/inline policies for operator review and leave them intact. If they preserve broad access, report an incomplete migration.
- **Cut over.** Stage and read back the new documents first. Detach only the exact AWS-managed PowerUserAccess ARN from this deploy role, then verify the final attachment set, effective policy versions and unchanged conditioned IAM rules. A test under both grants proves nothing about the new policy. Obtain a new OIDC session after detachment and allow for IAM propagation before running acceptance checks.
- **Handle interruption.** An interrupted migration may still have PowerUserAccess attached. Report its actual state, retry idempotently and serialize operator bootstrap runs for the same environment. An interrupted update of a customer-managed policy must leave a valid default version. Give each policy a deterministic environment-specific path and name. Include project, environment and a deploy-policy purpose tag in the IAM CreatePolicy request. IAM then holds the ownership record from the moment the policy exists. A retry adopts that exact ARN only if all ownership tags match and it is attached solely to the intended role or is still unattached. Refuse conflicting tags or other attached principals and report the ARN for operator resolution. Only operator bootstrap has permission to create or tag these policies. Check ownership again before retiring a version or attachment. The AWS-managed legacy attachment is identified by its exact ARN. On an unexpected denial, keep evidence of the action, resource type, phase and caller, repair the specific grant and rerun. Bootstrap must never silently reattach PowerUserAccess.

## Relationship to #9

The scoped deploy policy and narrowed host boundary can be constructed and tested against the existing release. The proposed #9 host-to-runtime STS call is made by a host role. Its runtime role and permission ceiling would be owned by operator bootstrap. Neither creates a deploy-role requirement for general AssumeRole or PassRole. Before release, rebase on the accepted #9 design and run the AWS lifecycle against the resulting host roles, SSM delivery and setup path. If #9 changes the caller of an AWS operation, update its inventory row and tests explicitly. [#9 design under review](https://github.com/vecten/vecten-open/pull/149)

## Limits of the design evidence

The [inventory](inventory.md) traces code and configurations. The exact dependent actions for RDS-managed passwords, EC2 compound operations, first-use service-linked roles, and provider refresh/teardown remain implementation and sandbox proof points. Keep the current user-facing AWS limitation until the replacement and authorized live checks are complete. The [task list](tasks.md) identifies the evidence needed to close #11.
