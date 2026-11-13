-- Passwords, or SCRAM verifiers Postgres stores unchanged, come through \getenv, never psql's
-- argv (psql 15+). Reruns keep roles, passwords and databases and reapply the grants.
\set ON_ERROR_STOP on

-- psql treats an undefined or non-boolean \if argument as false and carries on with exit 0,
-- so every flag is checked before the first \if reads it.
\if :{?with_airflow}
\else
\set with_airflow ''
\endif
\if :{?with_mcp_state}
\else
\set with_mcp_state ''
\endif
\if :{?with_warehouse}
\else
\set with_warehouse ''
\endif
\if :{?with_mcp}
\else
\set with_mcp ''
\endif
SELECT format('DO $check$ BEGIN RAISE EXCEPTION %L; END $check$',
              name || ' must be exactly true or false, got: ' || value)
FROM (VALUES ('with_airflow', :'with_airflow'), ('with_mcp_state', :'with_mcp_state'),
             ('with_warehouse', :'with_warehouse'), ('with_mcp', :'with_mcp')) AS flags (name, value)
WHERE value NOT IN ('true', 'false') \gexec

-- RDS's master is CREATEROLE without superuser: from Postgres 16 it holds ADMIN OPTION on the
-- roles it creates but no SET, which CREATE DATABASE ... OWNER and SET ROLE need. Each owner
-- role is granted to it WITH INHERIT FALSE, SET TRUE, so it reads no data by inheritance.
\getenv master_role MASTER_ROLE
\if :{?master_role}
\else
SELECT current_user AS master_role \gset
\endif

-- RDS's master owns postgres and template1; REVOKE by a non-owner only warns.
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;

\if :with_airflow
\getenv airflow_password AIRFLOW_DB_PASSWORD
SELECT format('CREATE ROLE airflow LOGIN PASSWORD %L', :'airflow_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'airflow') \gexec
SELECT format('GRANT airflow TO %I WITH INHERIT FALSE, SET TRUE', :'master_role')
WHERE NOT pg_has_role(:'master_role', 'airflow', 'SET') \gexec
SELECT 'CREATE DATABASE airflow OWNER airflow'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'airflow') \gexec
SET ROLE airflow;
REVOKE CONNECT ON DATABASE airflow FROM PUBLIC;
GRANT CONNECT ON DATABASE airflow TO airflow;
RESET ROLE;
\endif

\if :with_mcp_state
\getenv mcp_state_password MCP_STATE_DB_PASSWORD
SELECT format('CREATE ROLE mcp_state LOGIN PASSWORD %L', :'mcp_state_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_state') \gexec
SELECT format('GRANT mcp_state TO %I WITH INHERIT FALSE, SET TRUE', :'master_role')
WHERE NOT pg_has_role(:'master_role', 'mcp_state', 'SET') \gexec
SELECT 'CREATE DATABASE mcp_state OWNER mcp_state'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mcp_state') \gexec
SELECT format('SET ROLE %I', r.rolname) FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba
WHERE d.datname='mcp_state' \gexec
REVOKE CONNECT ON DATABASE mcp_state FROM PUBLIC;
GRANT CONNECT ON DATABASE mcp_state TO mcp_state;
SELECT format('GRANT CONNECT ON DATABASE mcp_state TO %I', :'master_role') \gexec
RESET ROLE;
SELECT 'CREATE ROLE mcp_state_maintenance NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_state_maintenance') \gexec
SELECT format('GRANT mcp_state_maintenance TO %I WITH INHERIT FALSE, SET TRUE', :'master_role')
WHERE NOT pg_has_role(:'master_role', 'mcp_state_maintenance', 'SET') \gexec
SELECT CASE WHEN coalesce(bool_or(m.inherit_option), false) THEN 'TRUE' ELSE 'FALSE' END AS state_master_inherit
FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member
WHERE r.rolname='mcp_state' AND u.rolname=:'master_role' \gset
SELECT format('GRANT mcp_state TO %I WITH INHERIT TRUE', :'master_role') \gexec
-- Only the installer temporarily inherits the old owner to migrate existing installations.
SELECT 'ALTER DATABASE mcp_state OWNER TO mcp_state_maintenance'
WHERE (SELECT datdba FROM pg_database WHERE datname='mcp_state') = 'mcp_state'::regrole \gexec
\connect mcp_state
SELECT 'ALTER TABLE public.access_events OWNER TO mcp_state_maintenance'
WHERE EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='public' AND c.relname='access_events'
                AND c.relowner='mcp_state'::regrole) \gexec
SELECT format('GRANT mcp_state TO %I WITH INHERIT %s', :'master_role', :'state_master_inherit') \gexec
SET ROLE mcp_state_maintenance;
GRANT CONNECT ON DATABASE mcp_state TO mcp_state;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO mcp_state;
CREATE SCHEMA IF NOT EXISTS vecten_audit AUTHORIZATION mcp_state_maintenance;
REVOKE ALL ON SCHEMA vecten_audit FROM PUBLIC;
GRANT USAGE ON SCHEMA vecten_audit TO mcp_state;

DO $audit_tables$
BEGIN
  IF EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname='access_events' AND c.relkind='r') THEN
    ALTER TABLE public.access_events RENAME TO access_events_legacy;
  END IF;
  CREATE TABLE IF NOT EXISTS public.audit_log (
    id text NOT NULL, at double precision NOT NULL, body jsonb NOT NULL,
    PRIMARY KEY (id, at)
  ) PARTITION BY RANGE (at);
  CREATE TABLE IF NOT EXISTS public.audit_log_default
    PARTITION OF public.audit_log DEFAULT;
  CREATE TABLE IF NOT EXISTS public.access_events (
    id text NOT NULL, at double precision NOT NULL, principal_id text,
    operation text NOT NULL, subject text, outcome text NOT NULL,
    PRIMARY KEY (id, at)
  ) PARTITION BY RANGE (at);
  CREATE TABLE IF NOT EXISTS public.access_events_default
    PARTITION OF public.access_events DEFAULT;
  IF to_regclass('public.access_events_legacy') IS NOT NULL THEN
    INSERT INTO public.access_events SELECT * FROM public.access_events_legacy;
    DROP TABLE public.access_events_legacy;
  END IF;
END;
$audit_tables$;
REVOKE ALL ON public.audit_log, public.access_events FROM PUBLIC, mcp_state;
GRANT INSERT, SELECT ON public.audit_log, public.access_events TO mcp_state;

CREATE OR REPLACE FUNCTION vecten_audit.maintain(retention_days integer DEFAULT 90)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $maintenance$
DECLARE
  parent_name text;
  partition_name text;
  day_number bigint;
  first_day bigint := floor(extract(epoch FROM clock_timestamp()) / 86400);
  cutoff double precision := extract(epoch FROM clock_timestamp()) - retention_days * 86400.0;
  old_partition record;
BEGIN
  IF retention_days IS NULL OR retention_days < 1 OR retention_days > 3650 THEN
    RAISE EXCEPTION 'retention must be between 1 and 3650 days' USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(8804011);
  FOREACH parent_name IN ARRAY ARRAY['audit_log', 'access_events'] LOOP
    EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE', parent_name);
    FOR day_number IN first_day-1..first_day+1 LOOP
      partition_name := parent_name || '_day_' || day_number;
      IF to_regclass(format('public.%I', partition_name)) IS NULL THEN
        EXECUTE format('CREATE TABLE public.%I (LIKE public.%I INCLUDING ALL)',
                       partition_name, parent_name);
        EXECUTE format('INSERT INTO public.%I SELECT * FROM public.%I WHERE at >= $1 AND at < $2',
                       partition_name, parent_name || '_default')
          USING day_number * 86400.0, (day_number+1) * 86400.0;
        EXECUTE format('DELETE FROM public.%I WHERE at >= $1 AND at < $2', parent_name || '_default')
          USING day_number * 86400.0, (day_number+1) * 86400.0;
        EXECUTE format('ALTER TABLE public.%I ATTACH PARTITION public.%I FOR VALUES FROM (%L) TO (%L)',
                       parent_name, partition_name, day_number * 86400.0, (day_number+1) * 86400.0);
      END IF;
    END LOOP;
    FOR old_partition IN
      SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
      WHERE i.inhparent=to_regclass(format('public.%I', parent_name))
        AND c.relname ~ ('^' || parent_name || '_day_[0-9]+$')
    LOOP
      day_number := substring(old_partition.relname FROM '[0-9]+$')::bigint;
      IF (day_number+1) * 86400.0 <= cutoff THEN
        EXECUTE format('DROP TABLE public.%I', old_partition.relname);
      END IF;
    END LOOP;
    EXECUTE format('DELETE FROM public.%I WHERE at < $1', parent_name || '_default') USING cutoff;
  END LOOP;
END;
$maintenance$;
REVOKE ALL ON FUNCTION vecten_audit.maintain(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vecten_audit.maintain(integer) TO mcp_state;
SELECT vecten_audit.maintain(90);
RESET ROLE;
\connect postgres
\endif

-- Every warehouse role lives only on the instance holding warehouse, so the state instance
-- holds no role a person or the impersonator could log in with.
\if :with_warehouse
\getenv loader_password LOADER_PASSWORD
-- No one logs in as the warehouse's owner; the master reaches it through SET ROLE.
SELECT 'CREATE ROLE warehouse_owner NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_owner') \gexec
SELECT 'ALTER ROLE warehouse_owner NOLOGIN PASSWORD NULL'
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warehouse_owner' AND rolcanlogin) \gexec
SELECT format('CREATE ROLE loader LOGIN PASSWORD %L', :'loader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'loader') \gexec
SELECT 'CREATE ROLE analyst NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst') \gexec
\if :with_mcp
\getenv provisioner_password PROVISIONER_PASSWORD
\getenv impersonator_password IMPERSONATOR_PASSWORD
SELECT format('CREATE ROLE provisioner LOGIN CREATEROLE PASSWORD %L', :'provisioner_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'provisioner') \gexec
SELECT format('CREATE ROLE impersonator LOGIN NOINHERIT PASSWORD %L', :'impersonator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'impersonator') \gexec
\endif
SELECT format('GRANT %I TO %I WITH INHERIT FALSE, SET TRUE', owner, :'master_role')
FROM unnest(ARRAY['warehouse_owner', 'loader']) AS owner
WHERE NOT pg_has_role(:'master_role', owner, 'SET') \gexec
SELECT 'CREATE DATABASE warehouse OWNER warehouse_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'warehouse') \gexec
SET ROLE warehouse_owner;
REVOKE CONNECT ON DATABASE warehouse FROM PUBLIC;
GRANT CONNECT ON DATABASE warehouse TO warehouse_owner, loader, analyst;
GRANT CREATE ON DATABASE warehouse TO loader;
SELECT 'GRANT CONNECT ON DATABASE warehouse TO provisioner, impersonator' WHERE :'with_mcp' = 'true' \gexec
-- A master without superuser holds the owner role without INHERIT, so it needs CONNECT of its
-- own for the \connect below.
SELECT format('GRANT CONNECT ON DATABASE warehouse TO %I', :'master_role')
WHERE NOT has_database_privilege(:'master_role', 'warehouse', 'CONNECT') \gexec
RESET ROLE;

\connect warehouse
-- The public schema belongs to pg_database_owner, whose privileges warehouse_owner holds.
SET ROLE warehouse_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SET ROLE loader;
CREATE SCHEMA IF NOT EXISTS staging AUTHORIZATION loader;
CREATE SCHEMA IF NOT EXISTS snapshots AUTHORIZATION loader;
CREATE SCHEMA IF NOT EXISTS core AUTHORIZATION loader;
CREATE SCHEMA IF NOT EXISTS marts AUTHORIZATION loader;
-- With the MCP, the default group's grants in its state are the only source of analyst's
-- reads, applied by its reconciliation, so a revoke made there survives the next ship.
SELECT 'GRANT USAGE ON SCHEMA core, marts TO analyst' WHERE :'with_mcp' = 'false' \gexec
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE loader IN SCHEMA core GRANT SELECT ON TABLES TO analyst'
WHERE :'with_mcp' = 'false' \gexec
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE loader IN SCHEMA marts GRANT SELECT ON TABLES TO analyst'
WHERE :'with_mcp' = 'false' \gexec
SELECT 'GRANT USAGE ON SCHEMA core, marts TO provisioner' WHERE :'with_mcp' = 'true' \gexec
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE loader IN SCHEMA core GRANT SELECT ON TABLES TO provisioner WITH GRANT OPTION'
WHERE :'with_mcp' = 'true' \gexec
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE loader IN SCHEMA marts GRANT SELECT ON TABLES TO provisioner WITH GRANT OPTION'
WHERE :'with_mcp' = 'true' \gexec
RESET ROLE;
-- The master's ADMIN OPTION on analyst, held since it created the role, lets it pass analyst on.
SELECT 'GRANT analyst TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE' WHERE :'with_mcp' = 'true' \gexec
\if :with_mcp
SET ROLE loader;
-- BEGIN VECTEN ACCESS HELPERS
CREATE SCHEMA IF NOT EXISTS vecten_access;
REVOKE ALL ON SCHEMA vecten_access FROM PUBLIC;
GRANT USAGE ON SCHEMA vecten_access TO provisioner;
CREATE TABLE IF NOT EXISTS vecten_access.helpers (
    schema_name text NOT NULL,
    owner_oid oid NOT NULL,
    function_name text NOT NULL UNIQUE,
    PRIMARY KEY (schema_name, owner_oid)
);
REVOKE ALL ON vecten_access.helpers FROM PUBLIC, provisioner;
GRANT SELECT ON vecten_access.helpers TO provisioner;
DO $install$
DECLARE
    scope record;
    fn text;
    body text;
    installer text := current_user;
    had_create boolean;
    had_usage boolean;
BEGIN
    FOR scope IN
        SELECT n.oid AS schema_oid, n.nspname, r.oid AS owner_oid, r.rolname
        FROM pg_catalog.pg_namespace n CROSS JOIN pg_catalog.pg_roles r
        WHERE (n.nspname IN ('core','marts','staging','snapshots')
               OR n.nspname ~ '^raw_[a-zA-Z0-9_]+$')
        AND (r.oid=n.nspowner
             OR EXISTS (SELECT FROM pg_catalog.pg_class c
                        WHERE c.relnamespace=n.oid AND c.relowner=r.oid)
             OR EXISTS (SELECT FROM pg_catalog.pg_default_acl d
                        WHERE d.defaclnamespace=n.oid AND d.defaclrole=r.oid)
             -- RDS lets rds_superuser and every role inheriting it create in any schema without
             -- an ACL entry, so they count as superusers here; the installer cannot become them.
             OR (NOT r.rolsuper
                 AND NOT EXISTS (SELECT FROM pg_catalog.pg_roles s
                                 WHERE s.rolname='rds_superuser'
                                   AND pg_catalog.pg_has_role(r.oid,s.oid,'USAGE'))
                 AND pg_catalog.has_schema_privilege(r.oid,n.oid,'CREATE')))
    LOOP
        IF scope.rolname IN ('provisioner','impersonator','analyst') THEN
            RAISE EXCEPTION 'unsafe schema owner or creator: %', scope.rolname;
        END IF;
        fn := format('read_%s_%s', scope.schema_oid, scope.owner_oid);
        body := format($body$
DECLARE
    obj record;
    action text := CASE WHEN enabled THEN 'GRANT' ELSE 'REVOKE' END;
    direction text := CASE WHEN enabled THEN 'TO' ELSE 'FROM' END;
    schema_id oid := %s;
BEGIN
    IF enabled IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles r
        JOIN pg_catalog.pg_auth_members m ON m.roleid=r.oid
        JOIN pg_catalog.pg_roles p ON p.oid=m.member
        WHERE r.rolname=target AND p.rolname='provisioner'
          AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option
          AND NOT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolcreatedb
          AND NOT r.rolcreaterole AND NOT r.rolreplication AND NOT r.rolbypassrls
          AND r.rolname !~ '^(admin_|svc_|pg_)'
          AND r.rolname NOT IN ('provisioner','impersonator','loader','warehouse_owner')
    ) THEN
        RAISE EXCEPTION 'target is not a bounded reader role' USING ERRCODE='42501';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_namespace
                   WHERE oid=schema_id AND nspname=%L) THEN
        RAISE EXCEPTION 'schema changed; administrator must reinstall access helpers';
    END IF;
    IF (relation_name IS NULL OR enabled) AND EXISTS (
        SELECT FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles r ON r.oid=n.nspowner
        WHERE n.oid=schema_id AND r.rolname=current_user
    ) THEN
        EXECUTE format('%%s USAGE ON SCHEMA %%I %%s %%I', action,%L,direction,target);
    END IF;
    FOR obj IN SELECT c.relname FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_roles r ON r.oid=c.relowner
               WHERE c.relnamespace=schema_id AND r.rolname=current_user
                 AND c.relkind IN ('r','p','v','m','f')
                 AND (relation_name IS NULL OR c.relname=relation_name)
    LOOP
        EXECUTE format('%%s SELECT ON TABLE %%I.%%I %%s %%I',
                       action,%L,obj.relname,direction,target);
    END LOOP;
    IF relation_name IS NULL THEN
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %%I IN SCHEMA %%I %%s SELECT ON TABLES %%s %%I',
                       current_user,%L,action,direction,target);
    END IF;
END
$body$, scope.schema_oid, scope.nspname, scope.nspname, scope.nspname, scope.nspname);
        had_create := pg_catalog.has_schema_privilege(scope.owner_oid,'vecten_access','CREATE');
        IF NOT had_create THEN
            EXECUTE format('GRANT CREATE ON SCHEMA vecten_access TO %I', scope.rolname);
        END IF;
        had_usage := pg_catalog.has_schema_privilege(scope.owner_oid,'vecten_access','USAGE');
        IF NOT had_usage THEN
            EXECUTE format('GRANT USAGE ON SCHEMA vecten_access TO %I', scope.rolname);
        END IF;
        -- SET ROLE is checked against the session user. An `admin_` login inherits CREATE here
        -- through the owner roles, and the session user created it, so it holds ADMIN on it but,
        -- since Postgres 16, not SET; it grants itself SET without INHERIT.
        IF NOT pg_catalog.pg_has_role(session_user, scope.owner_oid, 'SET') THEN
            EXECUTE format('SET LOCAL ROLE %I', session_user);
            EXECUTE format('GRANT %I TO %I WITH INHERIT FALSE, SET TRUE',
                           scope.rolname, session_user);
            EXECUTE format('SET LOCAL ROLE %I', installer);
        END IF;
        EXECUTE format('SET LOCAL ROLE %I', scope.rolname);
        EXECUTE format('CREATE OR REPLACE FUNCTION vecten_access.%I(target text, enabled boolean, relation_name text)'
                       ' RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS %L', fn,body);
        EXECUTE format('REVOKE ALL ON FUNCTION vecten_access.%I(text,boolean,text) FROM PUBLIC', fn);
        EXECUTE format('GRANT EXECUTE ON FUNCTION vecten_access.%I(text,boolean,text) TO provisioner', fn);
        EXECUTE format('SET LOCAL ROLE %I', installer);
        IF NOT had_create THEN
            EXECUTE format('REVOKE CREATE ON SCHEMA vecten_access FROM %I', scope.rolname);
        END IF;
        IF NOT had_usage THEN
            EXECUTE format('REVOKE USAGE ON SCHEMA vecten_access FROM %I', scope.rolname);
        END IF;
        INSERT INTO vecten_access.helpers VALUES (scope.nspname,scope.owner_oid,fn)
        ON CONFLICT (schema_name,owner_oid) DO UPDATE SET function_name=EXCLUDED.function_name;
    END LOOP;
END
$install$;
-- END VECTEN ACCESS HELPERS
RESET ROLE;
\endif
\endif
