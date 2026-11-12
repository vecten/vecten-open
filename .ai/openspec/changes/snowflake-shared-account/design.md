## Context

A Snowflake environment is built in three steps, and each step names objects in the account:

1. `deploy` applies the Snowflake root (`terraform/snowflake`), with its state at `<env>/snowflake.tfstate`. It creates the database, the convention schemas, the warehouse, the roles and the loader user.
2. An ACCOUNTADMIN runs the SQL that `vecten mcp provisioner-script --env <env>` prints. It creates the MCP's provisioner user, the `VECTEN_ACCESS` procedures and the External OAuth security integration.
3. The MCP's provisioner then creates a user for every person and service and a role for every group.

### Where the fixed names live

Each name below was checked in the code on `release/0.1.1`. The first nine are the ones listed in #34; the rest were found while checking them.

| Name or rule | Where | What it fixes |
|---|---|---|
| Roles `ANALYST`, `LOADER`, `PROVISIONER`; service user `LOADER` | `terraform/snowflake/main.tf`: `local.roles`, `snowflake_service_user.loader` | Account-level objects, the same in every environment |
| Database and warehouse | `vecten_shared.provider.snowflake_names` | Database `[warehouse] database` or the project name, the same in every environment; only the warehouse carries it (`<PROJECT>_<ENV>_WH`) |
| `SNOWFLAKE_LOADER_ROLE = "LOADER"` | `vecten_platform.sync.destination.loader_connection` | dlt and dbt sign in as `LOADER` |
| User name equals role name | `vecten_platform.sync.destination.build_destination` and `vecten_platform.dbt.profile.profile_env` pass `spec.role` as the user | The loader's user and role must share a name, because `ConnectionSpec` has no user field |
| `SNOWFLAKE_ANALYST_ROLE = "ANALYST"` | `vecten_shared.provider.runtime.OutputsRuntimeProvider._snowflake_warehouse` | The warehouse connection's role |
| User `VECTEN_PROVISIONER`, role `PROVISIONER` | `vecten_mcp.runtime_setup.configured_runtime`, the `provisioner()` connection | The MCP's control connection |
| `ANALYST`, `PROVISIONER`, `VECTEN_PROVISIONER`, `VECTEN_EXTERNAL_OAUTH` | `vecten_shared.identity_setup.provisioner_script`, `snowflake_access_helper`, `_leftover_grants` | `CREATE ROLE IF NOT EXISTS` takes over a role of the same name; `CREATE SECURITY INTEGRATION IF NOT EXISTS` followed by `ALTER … SET EXTERNAL_OAUTH_ISSUER` points an existing integration at this environment's issuer |
| `ANALYST`, `PROVISIONER`, `LOADER` | `vecten_shared/sql/snowflake_access.js`, the body of `READ_GRANT` | `ANALYST` is accepted without an ownership check, and any other target must be owned by `PROVISIONER` |
| `PROVISIONER`, `analyst` | `vecten_mcp.warehouse.snowflake.SnowflakeAdapter.check_scope` and `_grant` | The startup check reads `SHOW GRANTS TO ROLE PROVISIONER` and accepts grants only on the configured database and warehouse |
| Default group's role `analyst` | `vecten_mcp.state.StateStore.__init__` seeds it; `vecten_mcp.administration.Administration._create` grants it | Account-level role |
| Group role named after the group | `vecten_mcp.access.AccessService.create_group` (`warehouse_name(name, override=name)`) | Two environments' `finance` groups are one role |
| Person and service users named `<first>_<last>` and `svc_<name>` | `vecten_mcp.identity.warehouse_name`, `SnowflakeAdapter.create_account` | Account-level users; a second environment finds the first one's user and offers adoption |
| One database in every generated source | `vecten_shared.osi.generate.dataset_database` | `osi/datasets.json` sources are `<DATABASE>.<SCHEMA>.<TABLE>`, committed once for every environment; `SnowflakeAdapter.dataset_grant` and `source_exists` refuse another database |
| Every environment names `[warehouse] account` | `vecten.deploy.tfvars.check_tfvars` | Refuses a tfvars `snowflake_account` that differs from `vecten.toml` |
| Account from `vecten.toml` only | `OutputsRuntimeProvider._snowflake_warehouse`, `configured_runtime`, `vecten.mcp_onboarding.setup_script` | The runtime cannot reach an environment in another account |
| `VECTEN_PROVISIONER` in messages | `vecten.deploy.snowflake.identity_pending_note`; `vecten.cli.commands.deploy._destroy_message`; `vecten.deploy.run._clear_state_facts` | Text only |
| The runbook's "One environment per Snowflake account" | `scaffold/template/docs/runbook.md.jinja`, section "Snowflake" | Text only |

`vecten_platform.dbt.local_warehouse.snowflake_emulation` also names `LOADER` and `LOCAL_WH`, but only for the laptop's DuckDB emulation, so it stays as it is.

### Three findings that shape the design

- **One account per environment is not reachable today.** `check_tfvars` requires every environment's `snowflake_account` to equal `[warehouse] account`, and the runtime reads the account from `vecten.toml` only. A second environment must therefore name the first one's account, and there it collides. The requirement "one Snowflake account SHALL serve one environment" describes an option that the code allows for exactly one environment.
- **The committed semantic model names one database.** Sources in `osi/datasets.json` carry the database. Several environments in one account need several databases, so the same commit must serve different databases.
- **External OAuth trust covers the whole account.** A security integration accepts a token for any user in the account whose login name matches the token's subject, with any role the token's scope names that the user holds. Only the integration's blocked roles (`ACCOUNTADMIN`, `SECURITYADMIN` and `ORGADMIN` by default) are excluded. Two environments' MCPs in one account can therefore each sign in as any user the other created. With one account per environment, the account boundary contains this. With a shared account, it does not, unless something binds each user to one integration.

## Goals / Non-Goals

**Goals:**

- Several environments of a project in one Snowflake account, each with its own database, warehouse, roles, loader user, provisioner, security integration and users for people and services.
- No credential of one environment reaches another environment's data: not the loader key, not the provisioner key, and not the MCP's signing key.
- `destroy` and `--delete-data <env>` touch only the named environment's objects.
- Any number of environments, not only dev and prod.
- An existing deployment keeps every name it has.
- One account per environment works for more than one environment.

**Non-Goals:**

- An MCP-only project over an existing Snowflake account (Decision 13).
- Moving an environment between the two layouts in place (see the migration plan).
- A configurable prefix or suffix, or a naming convention imposed by the account's owner.
- Postgres, whose deployments already give each environment its own instance.
- Restricted marts (#51) and one reader role per person (#4). The naming leaves room for both; see "Interactions".

## Decisions

Every decision below was accepted by the maintainer on 2026-11-12, as recommended except Decision 3, where the maintainer chose the suffix scheme. They are ordered with the hardest to reverse first.

### Decision 1: How the layout is chosen, and the default (Accepted)

The choice decides every Snowflake name the environment will ever have, so it must be made before the first deploy and must not drift afterwards.

| Option | Consequences |
|---|---|
| **A. Project setting `[warehouse] shared_account` in `vecten.toml`, `false` by default, recorded per environment at its first deploy** | The runtime already reads `vecten.toml` from the bundle, so the shared naming function computes the same names on the laptop, in CI and on the VMs. A new environment takes the setting; a deployed one keeps what it recorded (`[environments.<env>] snowflake_shared_account`), and `deploy` refuses an environment whose record differs from the setting, unless the environment's account holds no other environment of the project (Decision 9). An existing fixed-name `prod` can stay where it is while new environments go to a shared account. |
| B. Per environment in tfvars, `snowflake_shared_account = true` | Most flexible. But the tfvars do not reach the VMs, so `ship` would have to render the value into every host, and every caller of `snowflake_names` would need the tfvars. A fixed-name and a shared-layout environment of the same project could also sit in one account, which reopens the cross-environment sign-in (Decision 4). |
| C. Inferred: shared when two environments' tfvars name the same account | Adding a second environment would silently rename the first. Rejected. |

**Accepted: A.** `next` asks the question after the Snowflake account, in these words: "Does this Snowflake account hold anything besides this one environment, such as another environment of this project or another team's data?" A yes also suits a single environment in a shared corporate account, where fixed names such as `ANALYST` and `LOADER` could already belong to someone else. `vecten answer snowflake_shared_account yes|no` records the answer, and `init --deploy` writes it to `vecten.toml`.

### Decision 2: Existing deployments keep their names (Accepted)

| Option | Consequences |
|---|---|
| **A. The fixed layout is exactly today's names** | No plan change, no rename and no new step for a running project. The fixed layout keeps its rule that the environment's account holds nothing else of the project. |
| B. One suffixed scheme for everyone, with a migration | Every grant, the integration, the users' identities, the loader's key registration, the Terraform resource names and every MCP state would have to move together across live accounts. A rename of the database or of a user that people's tools address is visible outside Vecten. |

**Accepted: A.** The fixed layout produces `ANALYST`, `LOADER` (user and role), `PROVISIONER`, `VECTEN_PROVISIONER`, `VECTEN_EXTERNAL_OAUTH`, the database `[warehouse] database` or the project name, the warehouse `<PROJECT>_<ENV>_WH`, person, service and group names without a suffix, and the `VECTEN_ACCESS` schema. The tests compare the function's output with these literals, so a later edit cannot rename a running environment.

### Decision 3: The naming scheme in a shared account (Accepted)

Snowflake keeps users, roles, warehouses, databases and integrations in separate namespaces, so only names of the same kind can collide. The database and the warehouse already belong to one environment; the question is how the account-level users, roles and integration carry it.

| Option | Consequences |
|---|---|
| A. Prefix `<BASE>_<ENV>_<NAME>`, single underscores | `ACME_DEV_ANALYST`, `ACME_DEV_JANE_DOE`. Users and roles sort by environment, so a person's user sits among its environment's objects rather than beside the person's other users. A shared account refuses an environment name with a hyphen. |
| B. Double underscore after a prefix, `ACME_DEV__ANALYST` | No restriction on environment names, but every name a person reads in query history carries `__`. |
| **C. Suffix `<NAME>_<BASE>_<ENV>`, single underscores, environment names without a hyphen** | `BASE` is `[warehouse] database`, or the project name when unset, in upper case with hyphens as underscores; `ENV` is the environment's name in upper case. Users and roles lead with their own name (`JANE_DOE_ACME_DEV`, `FINANCE_ACME_DEV`), so they sort by person and group, and a person finds their users under their own name. The database is `<BASE>_<ENV>` and the warehouse `<BASE>_<ENV>_WH`, the warehouse's name today; objects inside the database keep their names. A shared account refuses an environment name with a hyphen (`dev-2`); `dev2` works. |
| D. A prefix or suffix chosen per environment in tfvars | Meets an account owner's own conventions. The value would have to be shipped to the VMs (Decision 1, B). Left for later. |

**Accepted: C.** The maintainer chose the suffix so that a person's user and roles sort by their own name. The names for project `acme`, environment `dev`:

| Object | Fixed layout (today) | Shared layout |
|---|---|---|
| Database | `ACME` (or `[warehouse] database`) | `ACME_DEV` |
| Warehouse | `ACME_DEV_WH` | `ACME_DEV_WH` |
| `analyst` role (the default group's) | `ANALYST` | `ANALYST_ACME_DEV` |
| `loader` role and service user | `LOADER` | `LOADER_ACME_DEV` |
| `provisioner` role | `PROVISIONER` | `PROVISIONER_ACME_DEV` |
| Provisioner service user | `VECTEN_PROVISIONER` | `PROVISIONER_ACME_DEV` |
| External OAuth integration | `VECTEN_EXTERNAL_OAUTH` | `EXTERNAL_OAUTH_ACME_DEV` |
| Access procedures' schema | `ACME.VECTEN_ACCESS` | `ACME_DEV.VECTEN_ACCESS` |
| Authentication policy | none | `ACME_DEV.VECTEN_ACCESS.MCP_SIGN_IN` |
| Group `finance`'s role | `FINANCE` | `FINANCE_ACME_DEV` |
| Jane Doe's user | `JANE_DOE` | `JANE_DOE_ACME_DEV` |
| Service `reporting`'s user | `SVC_REPORTING` | `SVC_REPORTING_ACME_DEV` |
| #4's reader role for Jane (future) | `P_JANE_DOE` | `P_JANE_DOE_ACME_DEV` |

The suffix `_<BASE>_<ENV>` SHALL be at most 64 characters, so every name fits Snowflake's 255-character limit with the name before it, which is at most 63 characters. Within a project no two names collide: the environment is the last segment and holds no underscore, and the base is the same for every environment, so two names with the same suffix differ only in the name before it. Two projects in one account can produce the same name: project `acme`, environment `dev`, group `x_finance` and project `finance_acme`, environment `dev`, group `x` both give `X_FINANCE_ACME_DEV`. Collisions across projects are refused rather than shared: Terraform fails to create an object that exists, the provisioner script stops when an object it would create exists with another project's or environment's comment (every object carries `vecten <project> <env>`), and the MCP never adopts in a shared account (Decision 4).

### Decision 4: The MCP's identities for people and services in a shared account (Accepted)

This is the decision with the largest effect on security, and on the people using the warehouse.

| Option | Consequences |
|---|---|
| A. One user per person, shared by every environment in the account | Smallest change: the first environment creates `JANE_DOE` and the others adopt it. Row access and masking policies keyed on the user keep working, and a person is one Snowflake user. But any environment's MCP signing key can mint a token for `JANE_DOE` naming any role she holds, `prod`'s included (third finding). Deleting the person in the environment that created the user drops it under the others. |
| **B. One user per person and environment, `<NAME>_<BASE>_<ENV>`, bound to the environment's integration by an authentication policy** | The administrator's script creates `<BASE>_<ENV>.VECTEN_ACCESS.MCP_SIGN_IN` with `AUTHENTICATION_METHODS = ('OAUTH')` and `SECURITY_INTEGRATIONS = ('EXTERNAL_OAUTH_<BASE>_<ENV>')`, and grants the provisioner `APPLY` on it. The provisioner sets the policy on every user it creates. Snowflake then refuses a token from another environment's integration for that user, so a stolen `dev` signing key reaches no `prod` identity. Query history shows `JANE_DOE_ACME_PROD`. Policies keyed on a person's own corporate user do not apply to it, while policies keyed on roles do. In a shared account the MCP adopts no existing user, because setting the policy on one would change how its owner signs in. |
| C. B without the authentication policy | No isolation gained over A, because the integration still accepts any user. Rejected. |

**Accepted: B**, on two conditions that only a live account can confirm: that an authentication policy's `SECURITY_INTEGRATIONS` limits External OAuth sign-in, and that `APPLY` on the policy together with ownership of the user is enough to set it, without `APPLY AUTHENTICATION POLICY` on the account. If either fails, the fallback is A, with the runbook stating that every environment's signing key in the account reaches every environment's data. Falling back to A needs the maintainer's explicit sign-off on that runbook warning before shared accounts ship.

In the fixed layout nothing changes. There, the account holds one environment, so the integration's reach is bounded by the account. That reach still covers every role an adopted user holds, which #4 addresses.

### Decision 5: The provisioner and its scope check (Accepted)

| Option | Consequences |
|---|---|
| **A. One provisioner role and user per environment** | `PROVISIONER_<BASE>_<ENV>` signs in with the environment's own `MCP_PROVISIONER_PRIVATE_KEY`, which already sits in that environment's secrets store. The startup check (`SnowflakeAdapter.check_scope`) compares grants with the environment's names: its database, warehouse and `VECTEN_ACCESS` procedures, its `analyst` role, the roles and users in its registry, and `APPLY` on its authentication policy. `READ_GRANT` is generated with the environment's database, warehouse, provisioner role and suffix, and refuses a target role whose name does not end in the suffix before it checks ownership. The suffix check is a first filter: `_ACME_DEV` is also the tail of `_X_ACME_DEV`, another project's suffix, so ownership by the environment's `provisioner` stays the guard. |
| B. One `PROVISIONER` shared by all environments | The scope check would accept several databases, and one compromised MCP host would reach every environment. This contradicts the issue. Rejected. |

**Accepted: A.** `CREATE USER` and `CREATE ROLE` remain account-wide privileges, so a `dev` provisioner can create an object named with `prod`'s suffix. That object gives it no data, since it holds no grant option on `prod`'s tables, but it can block `prod` from creating a user or role of that name. `prod` then refuses rather than adopts, and the runbook names this.

### Decision 6: The External OAuth integration (Accepted)

| Option | Consequences |
|---|---|
| **A. One integration per environment, `EXTERNAL_OAUTH_<BASE>_<ENV>`** | Each integration names its own environment's issuer and JWKS URL (the MCP URL that #56 resolves from the environment's tfvars) and the account's audience. Re-running the script re-points only its own integration. The fixed layout keeps `VECTEN_EXTERNAL_OAUTH`, created if absent and then re-pointed, because there the account holds one environment. |
| B. One shared integration | A `CUSTOM` integration has a single `EXTERNAL_OAUTH_ISSUER`, and each environment's MCP is its own issuer with its own signing key. Sharing would mean sharing one signing key. Rejected. |

**Accepted: A.** `EXTERNAL_OAUTH_ANY_ROLE_MODE` stays `ENABLE` until #4 lands, as today. That several `CUSTOM` integrations with different issuers coexist in one account, and that Snowflake picks one by the token's `iss`, is a live check.

### Decision 7: Group roles (Accepted)

Group roles, like users, are account-level objects.

| Option | Consequences |
|---|---|
| **A. Suffixed `<GROUP>_<BASE>_<ENV>`, created and owned by the environment's provisioner** | A `finance` group in `dev` and one in `prod` are two roles, and `READ_GRANT` refuses a target without the environment's suffix. |
| B. One role without a suffix, shared by the environments | A `dev` membership would read `prod`'s marts. Rejected. |

**Accepted: A**, with the suffix applied in the Snowflake adapter rather than stored. `mcp_state` keeps the names it holds today: the default group's `analyst`, `finance`, `jane_doe`. `SnowflakeAdapter` appends the suffix whenever it names a Snowflake user or role, and strips it from what `SHOW GRANTS` returns before looking it up in the registry. The panel, the registry, name derivation and the Postgres path stay as they are. The audit names the principal, and the documentation states the Snowflake name.

### Decision 8: Where the names come from (Accepted)

| Option | Consequences |
|---|---|
| **A. `snowflake_names(config, env)` in `vecten-shared` returns every name** | It returns the account, suffix, database, warehouse, `analyst`, `loader` and `provisioner` roles, loader and provisioner users, integration, access schema and authentication policy. It is the only place a Snowflake name is spelled. `deploy` passes the role and user names to the Snowflake root as `-var`, as it does the database and warehouse today, so no tfvars change. `destination.py`, `runtime.py`, `runtime_setup.py`, `identity_setup.py`, the MCP adapter and the CLI messages read it instead of holding constants. |
| B. Terraform outputs as the source | An MCP-only project has no Snowflake root, and a fresh checkout has no outputs file (`.vecten/` is ignored, which is why #56 reads tfvars instead). |
| C. Keep the constants and add the suffix at each site | Ten sites, and nothing keeps them in step. |

**Accepted: A.** The loader's user and role keep one name in both layouts, because `build_destination` and `profile_env` pass `spec.role` as the user. Adding a user field to `ConnectionSpec` is possible but not needed. The module's `local.roles` becomes a map variable, so in the fixed layout the plan is unchanged; a `terraform test` and a plan of an existing sandbox prove it.

### Decision 9: `[warehouse] account` and `database` (Accepted)

| Option | Consequences |
|---|---|
| **A. The tfvars `snowflake_account` is each environment's account; `[warehouse] account` names `prod`'s; `ship` hands the account to the hosts** | `check_tfvars` compares the account with `vecten.toml` for `prod` only, as it already does for the domain, and #56 does for the MCP URL. `ship` writes the environment's account into the Compose environment of every host (`VECTEN_SNOWFLAKE_ACCOUNT`), the same path #58 uses for `dags_paused_at_creation`. This needs no change to the VM agent's `HOST_KEYS` or to `apply.py`. In the fixed layout `check_tfvars` refuses an environment whose account another environment's tfvars also name; in the shared layout it refuses a shared environment whose account a fixed environment of the project names. |
| B. One account per project, as today | The shared layout works, but the fixed layout stays limited to one environment. |
| C. A per-environment table in `vecten.toml` | A second copy of a value that the tfvars already hold per environment. |

**Accepted: A.** `[warehouse] database` stays a project setting and becomes the base of the database name and of the suffix in the shared layout. A per-environment database override is not needed, because the environment's name already separates them.

### Decision 10: The committed semantic model names one database (Accepted)

| Option | Consequences |
|---|---|
| **A. Sources keep naming the project's database; the MCP reads it as its own** | `dataset_database` still writes the base name (`ACME`). When the MCP loads the model, a source whose database part is that name is read as the environment's database (`ACME_DEV`), for source checks, compiled SQL and grants. In the fixed layout the two names are equal, so nothing changes. `describe` shows the environment's name, so SQL an agent writes for `sql-exec` names the right database. That SQL is not rewritten. |
| B. Generate two-part sources (`MARTS.ORDERS`), resolved by the session's database | Every project's `datasets.json` changes, and sources in an MCP-only project may span databases. |
| C. One `osi/` per environment | Defeats shipping one commit everywhere. Rejected. |

**Accepted: A.**

### Decision 11: `destroy` and `--delete-data` (Accepted)

Terraform already keeps each environment's Snowflake state apart, so an apply or destroy touches only that environment's database, warehouse, roles and loader user. Four kinds of object live outside Terraform: the provisioner user, the integration, the authentication policy (in the environment's database), and the users and roles the provisioner created.

| Option | Consequences |
|---|---|
| **A. `--delete-data` refuses while the environment's provisioner owns a user or role, and prints the statements an administrator runs** | The list is read by ownership (`SHOW GRANTS TO ROLE PROVISIONER_<BASE>_<ENV>`, `OWNERSHIP`), never by a name pattern, so an environment or project whose suffix ends with this one's is never matched. The statements drop those users and roles, the provisioner user and the integration. A database is never dropped while users still carry its policy. Without `--delete-data`, `destroy` keeps the database as today and reports the objects that stay. |
| B. Terraform manages the integration and the provisioner user | The deploy role would need `CREATE INTEGRATION`, which today only the administrator's script holds. |
| C. Drop everything with the suffix | A name pattern would also match objects of an environment or project whose suffix ends with this one's (`_X_ACME_DEV` ends with `_ACME_DEV`). Rejected. |

**Accepted: A.** Whether Snowflake refuses `DROP DATABASE` while a policy in it is set on users is a live check. The refusal guards against it either way.

### Decision 12: dbt profiles, the rendered runbook and the deploy checks (Accepted)

- **dbt.** `profile_env` receives its connection from `loader_connection`, which will take the loader's names from `snowflake_names`. The rendered `profiles.yml` reads only environment variables, so it does not change. `vecten dbt --env <env>` (#53) gets the right names through the same function.
- **Runbook.** The "Snowflake" section renders per layout. Fixed layout: the account holds this environment and nothing else of the same names. Shared layout: the suffixed names, the authentication policy, the cleanup on `--delete-data`, and the advice to give each environment its own deploy user. The module grants every role it creates to the deploy role, so a deploy user shared by environments is one credential that reaches all of them. The runbook advises separate deploy users; `deploy` cannot enforce it, because it reads no other environment's secrets.
- **Deploy checks.** `check_tfvars` gains the account rules of Decision 9 and the environment-name rule of Decision 3. `deploy` refuses an environment whose recorded layout differs from the setting (Decision 1). `identity_pending_note` names the environment's provisioner user. The deploy role needs the same privileges as today: `CREATE DATABASE`, `CREATE WAREHOUSE`, `CREATE ROLE`, `CREATE USER` and `MANAGE GRANTS`.

**Accepted: as listed.**

### Decision 13: MCP-only projects over an existing Snowflake account (Accepted)

The same identity names (`ANALYST`, `PROVISIONER`, `VECTEN_PROVISIONER`, `VECTEN_EXTERNAL_OAUTH`) collide when two MCP-only environments share an existing account. There, though, the database belongs to the client and is usually the same for every environment, so the access schema, the default grants and the procedures would have to be separated inside one database.

| Option | Consequences |
|---|---|
| **A. Out of scope; `shared_account` is refused without the `platform` component** | This change stays the size of #34. An MCP-only project keeps one environment per account, and #62 covers it. |
| B. In scope | Adds an access schema per environment (`VECTEN_ACCESS_<ENV>`) and per-environment default grants in a shared database. |

**Accepted: A**, with the follow-up filed as #62.

## Interactions

- **#51, restricted marts**, decisions accepted. The MCP becomes the only owner of the default group's grants on `CORE` and `MARTS`, and a client's existing role is bound by nesting it under the group's role. The default grant's role comes from `snowflake_names` (`analyst`) in both layouts, so moving the grant keeps the name. `READ_GRANT` never targets a bound role, so no allow-list of adopted roles is needed; in a shared account a bound role that holds privileges on another environment's database of the project is refused. A restricted area's schemas live inside the environment's database and need no suffix.
- **#4, one reader role per person.** The per-person role becomes `P_<LOGIN>_<BASE>_<ENV>` in the shared layout and `P_<LOGIN>` in the fixed one. Both are roles, like group roles, so #4 must reserve the group name prefix `p_`, or a group named `p_jane_doe` would collide with Jane's reader role. If #4 lands first, the shared layout's integrations are created with `EXTERNAL_OAUTH_ANY_ROLE_MODE = DISABLE`. With Decision 4 B, #4's single-role tokens also stay inside their environment, because the user's policy names one integration.
- **#53, `vecten dbt --env`**, merged in #60. This change edits its call site so that it takes the account, database, warehouse and loader user and role from `snowflake_names(config, env)`, not from `[warehouse] account` or a fixed `LOADER`.
- **#56, each environment's MCP URL**, merged. The per-environment integration takes its issuer from `mcp_url(root, config, env)`.
- **#58, per-environment settings in tfvars**, merged. The account reaches the hosts the way `dags_paused_at_creation` does. Both changes edit `check_tfvars` and `vecten.stack.compose.generate_compose`. This change adds requirements to `shared/deploy` and does not modify "Per-environment configuration and state", which #58 modifies, so the delta specs do not conflict.

## Risks / Trade-offs

- [Snowflake behaviour not yet proven] → Five live checks gate the shared layout (tasks, section 9): several `CUSTOM` integrations in one account, authentication policies binding External OAuth, the provisioner's right to set them, dropping a database that holds a policy in use, and that the fixed layout's plan is empty after the module change. If the policy checks fail, Decision 4 falls back to A only with the maintainer's sign-off, and the runbook says so.
- [Person-keyed warehouse policies] → In the shared layout a person's MCP queries run as `<NAME>_<BASE>_<ENV>`, not as their corporate user, so row access and masking policies must key on roles. The runbook and `docs/mcp-identity.md` say so.
- [Name squatting between environments] → A provisioner can create a name with another environment's suffix. The other environment refuses rather than adopts, and the runbook names the cause.
- [Suffix collisions between projects] → Creation fails instead of sharing an object.
- [The setting is fixed per environment] → Changing the layout of a deployed environment means a new deployment (migration plan).
- [The VM agent] → Nothing here touches `apply.py` or `HOST_KEYS`: the account travels in the Compose environment.

## Migration Plan

- **Existing projects.** Nothing changes. `shared_account` defaults to `false`; `vecten update` re-renders the Snowflake thin root, which declares the new name variables, and `deploy` passes today's names, so the plan is empty. An environment deployed before this change has no recorded layout and is read as fixed.
- **A project adding environments in a shared account while `prod` stays in its own account.** Set `shared_account = true`, then deploy the new environments. `prod` keeps its recorded fixed layout and its account, which no other environment of the project names.
- **Moving a deployed environment into a shared account.** This is a new deployment of that environment: destroy it, keeping its snapshots, point its tfvars at the shared account, and deploy again with the shared layout. Raw data is loaded again by the connectors, or copied with Snowflake's database replication, which the runbook describes. The new `mcp_state` starts empty: people are added again with `vecten user add`, and groups and grants are recreated. A tool to carry principals and grants across is not part of this change.
- **Rollback.** Before any environment records the shared layout, setting `shared_account` back to `false` restores today's behaviour. After that, the environment must be destroyed with `--delete-data` and the cleanup statements run.
