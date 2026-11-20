## Why

The GitHub Actions deploy role for one Vecten environment currently has AWS's `PowerUserAccess`. Its non-IAM access extends to other workloads in the account. An environment should be able to apply its Terraform and ship a release with a policy whose resources and remaining account-wide actions an operator can review. [Issue #11](https://github.com/vecten/vecten-open/issues/11) · [Current AWS guidance](../../../../docs/deploy-aws.md)

## What Changes

This design proposes replacing that managed-policy attachment at operator bootstrap. It preserves the existing GitHub trust, IAM rules for host roles, and separate plan role. A fresh install and a rerun on an existing role must converge on the same scoped permissions. The current dedicated-account guidance applies while the implementation and live verification are pending.

## Impact

Start with the [inventory by caller and AWS service](inventory.md), then review the [policy and migration decisions](design.md), [proposed deploy requirement](specs/shared/deploy/spec.md) and [implementation and release checks](tasks.md). This PR contains the design only. Issue #9 owns the separate change to how a VM obtains database setup and runtime credentials. [#9 design PR](https://github.com/vecten/vecten-open/pull/149)
