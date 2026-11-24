## Why

Today an application container on the database setup VM can retrieve the administrator password for every managed Postgres instance in its environment. This includes Airflow DAGs when the components share a VM. A database password change alone would leave the same retrieval path open. [Issue #9](https://github.com/vecten/vecten-open/issues/9) · [Current security model](../../../../SECURITY.md)

## What Changes

Keep setup close to the private databases. Give the host agent a way to run setup that none of the workloads it starts can use. On AWS, keep the EC2 instance profile on the host and supply renewable runtime role sessions to the containers. On Scaleway, deliver a second key exclusively to the setup agent and narrow the existing host application's policy. Existing environments migrate one at a time with an explicit cutover.

## Impact

This is a proposal for review. The proposed requirements cover [cloud deployment](specs/shared/deploy/spec.md) and the [deployed platform stack](specs/platform/platform-stack/spec.md). The [design](design.md) traces the present identities, defines their proposed grants and orders the migration. The [tasks](tasks.md) group implementation and verification by PR. Replacing the AWS CI deploy role's PowerUserAccess policy has its own lifecycle tests and issue. [Issue #11](https://github.com/vecten/vecten-open/issues/11)
