# Verified sign-in extension hook

## Why

An installed extension needs a supported point to update human memberships after the server verifies an identity.

## What Changes

Expose a verified sign-in event and a group-scoped membership writer. The extension owns the policy and its groups. Access health identifies that owner.

## Impact

Membership management stays local in projects without a registered hook. The edition boundary continues to assign provider group mapping and directory synchronization to Enterprise.
