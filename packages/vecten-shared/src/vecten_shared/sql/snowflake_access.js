// The procedure's wrapper defines database, warehouse, analystRole, provisionerRole,
// loaderRole, suffix, version and mode from the environment's Snowflake names.
function quoted(value) {
    if (typeof value !== 'string' || value.length === 0 || value.indexOf('\u0000') !== -1) {
        throw new Error('invalid identifier');
    }
    return '"' + value.replace(/"/g, '""') + '"';
}
function execute(sqlText) { return snowflake.execute({sqlText: sqlText}); }
// Every relation kind a model can be built as, in the plural GRANT ... ON ALL takes.
var KINDS = ['TABLES', 'VIEWS', 'MATERIALIZED VIEWS', 'DYNAMIC TABLES', 'EXTERNAL TABLES',
             'ICEBERG TABLES'];
// A group's role is at most 63 characters before the environment's suffix. The environment's
// READER is reserved for a reader of every schema, which no grant here may create.
function bounded(target) {
    if (typeof target !== 'string' ||
        target.length > 63 + suffix.length || !/^[A-Z][A-Z0-9_]*$/.test(target) ||
        /^(ADMIN_|SVC_|PG_)/.test(target) ||
        ['PUBLIC','ACCOUNTADMIN','SECURITYADMIN','SYSADMIN','USERADMIN','ORGADMIN',
         'IMPERSONATOR','WAREHOUSE_OWNER',provisionerRole,loaderRole,
         'READER' + suffix].indexOf(target) !== -1) {
        throw new Error('target is not a bounded reader role');
    }
}
function owned(target) {
    // A first filter only: another project's suffix can end in this one's (`_X_ACME_DEV` ends
    // in `_ACME_DEV`), so ownership by this environment's provisioner stays the guard.
    if (suffix !== '' && (target.length <= suffix.length ||
        target.slice(target.length - suffix.length) !== suffix)) {
        throw new Error('target is not a role of this environment');
    }
    if (target === analystRole) return;
    var grants = execute('SHOW GRANTS ON ROLE ' + quoted(target));
    var found = false;
    while (grants.next()) {
        if (grants.getColumnValue('privilege') === 'OWNERSHIP' &&
            grants.getColumnValue('granted_to') === 'ROLE' &&
            grants.getColumnValue('grantee_name') === provisionerRole) found = true;
    }
    if (!found) throw new Error('target is not owned by provisioner');
}
function inDatabase(name) {
    return typeof name === 'string' &&
        (name.indexOf(database + '.') === 0 || name.indexOf(quoted(database) + '.') === 0);
}
function readGrant() {
    // Snowflake passes a BOOLEAN argument as the number 1 or 0 and SQL NULL as undefined.
    if (TARGET === undefined && SCHEMA_NAME === undefined && RELATION_NAME === undefined &&
        ENABLED === undefined) {
        return 'vecten-access ' + version;
    }
    var enabled = ENABLED === true || ENABLED === 1 ? true
        : ENABLED === false || ENABLED === 0 ? false : null;
    var relation = RELATION_NAME === undefined ? null : RELATION_NAME;
    if (enabled === null) throw new Error('target is not a bounded reader role');
    bounded(TARGET);
    // An area's schemas match a pattern rather than a list, so declaring an area needs no
    // rerun of this script.
    if (typeof SCHEMA_NAME !== 'string' ||
        (['CORE','MARTS','STAGING','SNAPSHOTS'].indexOf(SCHEMA_NAME) === -1 &&
         !/^RAW_[A-Z0-9_]+$/.test(SCHEMA_NAME) &&
         !/^(CORE|MARTS)_[A-Z][A-Z0-9]*$/.test(SCHEMA_NAME))) {
        throw new Error('schema is outside the approved convention');
    }
    owned(TARGET);
    var target = ' ROLE ' + quoted(TARGET);
    var schema = quoted(database) + '.' + quoted(SCHEMA_NAME);
    var action = enabled ? 'GRANT' : 'REVOKE';
    var direction = enabled ? ' TO' : ' FROM';
    var relationTypes = [];
    if (relation !== null) {
        quoted(relation);
        var table = snowflake.execute({
            sqlText: 'SELECT TABLE_TYPE FROM ' + quoted(database) + '.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=? AND TABLE_NAME=?',
            binds: [SCHEMA_NAME, relation]
        });
        if (!table.next()) throw new Error('dataset relation does not exist');
        var tableType = table.getColumnValue(1);
        // A base table may be a dynamic or Iceberg table, whose GRANT names another object
        // type; a statement naming the wrong one can succeed and change nothing, so each type
        // is tried until the grant reads back as wanted.
        relationTypes = ['VIEW', 'MATERIALIZED VIEW', 'EXTERNAL TABLE'].indexOf(tableType) !== -1
            ? [tableType] : ['TABLE', 'DYNAMIC TABLE', 'ICEBERG TABLE'];
    }
    if (enabled) {
        execute('GRANT USAGE ON DATABASE ' + quoted(database) + ' TO' + target);
        if (warehouse !== null) execute('GRANT USAGE ON WAREHOUSE ' + quoted(warehouse) + ' TO' + target);
        execute('GRANT USAGE ON SCHEMA ' + schema + ' TO' + target);
    }
    var prefix = database + '.' + SCHEMA_NAME + '.';
    if (relation !== null) {
        var first = null;
        for (var i = 0; i < relationTypes.length; i++) {
            try {
                execute(action + ' SELECT ON ' + relationTypes[i] + ' ' + schema + '.' +
                        quoted(relation) + direction + target);
            } catch (e) {
                if (first === null) first = e;
                continue;
            }
            if (selectsIn(prefix + relation, false) === enabled) return 'ok';
        }
        throw first !== null ? first
            : new Error('no object type changed SELECT on ' + prefix + relation);
    }
    // A refusal on tables or views, which hold dbt's default marts, fails the call.
    var refused = [];
    KINDS.forEach(function(kind, index) {
        ['ALL','FUTURE'].forEach(function(shape) {
            var statement = action + ' SELECT ON ' + shape + ' ' + kind + ' IN SCHEMA ' +
                schema + direction + target;
            if (index < 2) { execute(statement); return; }
            try { execute(statement); }
            catch (e) { refused.push({kind: shape + ' ' + kind, error: String(e.message || e)}); }
        });
    });
    // A grant reports another refused kind and keeps the rest; a revoke fails while the
    // target still reads anything in the schema.
    if (enabled) return refused.length ? 'refused: ' + JSON.stringify(refused) : 'ok';
    execute('REVOKE USAGE ON SCHEMA ' + schema + ' FROM' + target);
    if (refused.length && selectsIn(prefix, true)) {
        throw new Error('the target still holds SELECT in ' + database + '.' + SCHEMA_NAME +
            ' after refusals: ' + refused.map(function(r) {
                return r.kind + ': ' + r.error;
            }).join('; '));
    }
    return 'ok';
}
// Whether the target holds SELECT on the relation `name`, or, with `within`, on any relation
// or future relation whose name starts with it.
function selectsIn(name, within) {
    name = name.replace(/"/g, '');
    var shows = ['SHOW GRANTS TO ROLE ', 'SHOW FUTURE GRANTS TO ROLE '];
    for (var i = 0; i < (within ? 2 : 1); i++) {
        var rows = execute(shows[i] + quoted(TARGET));
        while (rows.next()) {
            var held = String(rows.getColumnValue('name')).replace(/"/g, '');
            if (rows.getColumnValue('privilege') === 'SELECT' &&
                (within ? held.indexOf(name) === 0 : held === name)) return true;
        }
    }
    return false;
}
// What one bounded target holds in this database, read and never changed: USAGE and SELECT
// grants, and the future grants made at schema or database level.
function readGrants() {
    bounded(TARGET);
    owned(TARGET);
    var held = {version: version, grants: [], future: []};
    var rows = execute('SHOW GRANTS TO ROLE ' + quoted(TARGET));
    while (rows.next()) {
        var privilege = rows.getColumnValue('privilege');
        var name = rows.getColumnValue('name');
        if ((privilege === 'USAGE' || privilege === 'SELECT') && inDatabase(name)) {
            held.grants.push({privilege: privilege, granted_on: rows.getColumnValue('granted_on'),
                              name: name});
        }
    }
    rows = execute('SHOW FUTURE GRANTS TO ROLE ' + quoted(TARGET));
    while (rows.next()) {
        var futureName = rows.getColumnValue('name');
        if (rows.getColumnValue('privilege') === 'SELECT' && inDatabase(futureName)) {
            held.future.push({privilege: 'SELECT', grant_on: rows.getColumnValue('grant_on'),
                              name: futureName});
        }
    }
    return held;
}
return mode === 'report' ? readGrants() : readGrant();
