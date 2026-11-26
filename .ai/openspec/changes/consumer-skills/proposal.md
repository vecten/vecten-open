# Distribute assistant skills to data users

## Why

Teams distribute instructions for working with warehouse data by hand. A project needs to publish the instructions to its analysts using its existing group memberships.

## What Changes

An extension can declare a named consumer skill. The CLI can package it as a ZIP. The MCP apps page lists and serves the packages the signed-in person’s current groups can download.

## Impact

Each package contains the skill instructions and the supporting files the author lists. Warehouse access continues to be evaluated for the person who queries it.
