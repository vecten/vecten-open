# Sign in to deployed Airflow with the project's OpenID Connect provider

## Why

Platform operators currently keep a separate Airflow administrator password alongside the identity provider used for MCP sign-in.

## What Changes

- Add an opt-in Airflow auth manager for deployed hosts that uses the project's OIDC issuer and administrator list.
- Keep laptop sign-in and authenticated read-only access for automation.

## Impact

A project opting in must register its Airflow callback with its provider before its next ship.
