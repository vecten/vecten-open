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
         !/^(CORE|MARTS|STAGING|SNAPSHOTS)_[A-Z][A-Z0-9]*$/.test(SCHEMA_NAME))) {
        throw new Error('schema is outside the approved convention');
    }
    // Revocation remains possible after removing an area from the installed list.
    if (enabled && /^(STAGING|SNAPSHOTS)_/.test(SCHEMA_NAME) &&
        sourceAreas.indexOf(SCHEMA_NAME.split('_')[1]) === -1) {
        throw new Error('schema is outside the installed source area list; rerun the provisioner script');
    }
    owned(TARGET);
    try {
        return change(enabled, relation);
    } catch (e) {
        // A schema the owner cannot list may be gone or only invisible to it, so a revoke's
        // failure stands; a grant then waits, which grants nothing.
        if (enabled && !schemaListed()) return 'absent';
        throw e;
    }
}
// The provisioner sees no schema it holds nothing on, and INFORMATION_SCHEMA lists only what
// this procedure's owner may use; it is asked only once a statement has failed.
function schemaListed() {
    var schemata = snowflake.execute({
        sqlText: 'SELECT COUNT(*) FROM ' + quoted(database) +
            '.INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=?',
        binds: [SCHEMA_NAME]
    });
    return schemata.next() && schemata.getColumnValue(1) > 0;
}
// The grant or revoke itself, once the target and the schema have passed every check.
function change(enabled, relation) {
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
        var listed = table.next();
        if (!listed && enabled) {
            throw new Error('dataset relation does not exist or is not visible: ' +
                SCHEMA_NAME + '.' + relation);
        }
        var tableType = listed ? table.getColumnValue(1) : null;
        // A base table may be a dynamic or Iceberg table, whose GRANT names another object
        // type; a statement naming the wrong one can succeed and change nothing, so each type
        // is tried until the grant reads back as wanted. A relation the owner cannot list may
        // still hold the grant, so its revoke tries every type and fails if none applies.
        relationTypes = tableType === null
            ? ['TABLE', 'VIEW', 'MATERIALIZED VIEW', 'DYNAMIC TABLE', 'EXTERNAL TABLE',
               'ICEBERG TABLE']
            : ['VIEW', 'MATERIALIZED VIEW', 'EXTERNAL TABLE'].indexOf(tableType) !== -1
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
// Every relation of the modelled schemas, read and never changed, with the object type a DROP
// of it names: INFORMATION_SCHEMA calls a dynamic or an Iceberg table a base table. The
// provisioner can list only what it holds a privilege on, which leaves out views and areas.
function relations() {
    var dynamic = namedIn('SHOW DYNAMIC TABLES IN DATABASE ' + quoted(database));
    var iceberg = namedIn('SHOW ICEBERG TABLES IN DATABASE ' + quoted(database));
    // An area's schemas match a pattern rather than a list, as in readGrant.
    var rows = execute('SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM ' + quoted(database) +
        ".INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA IN ('CORE','MARTS')" +
        " OR TABLE_SCHEMA RLIKE '(CORE|MARTS)_[A-Z][A-Z0-9]*'");
    var listed = {version: version, relations: []};
    while (rows.next()) {
        var schema = rows.getColumnValue(1), name = rows.getColumnValue(2);
        var type = rows.getColumnValue(3), key = schema + '.' + name;
        var kind = ['VIEW', 'MATERIALIZED VIEW', 'EXTERNAL TABLE'].indexOf(type) !== -1 ? type
            : dynamic[key] ? 'DYNAMIC TABLE' : iceberg[key] ? 'ICEBERG TABLE' : 'TABLE';
        listed.relations.push({schema: schema, name: name, kind: kind});
    }
    return listed;
}
function namedIn(statement) {
    var found = Object.create(null);
    var rows = execute(statement);
    while (rows.next()) {
        found[rows.getColumnValue('schema_name') + '.' + rows.getColumnValue('name')] = true;
    }
    return found;
}
// Snowflake hands SQL NULL to a procedure as undefined: the report with no target lists.
if (mode === 'report') return TARGET === undefined ? relations() : readGrants();
return readGrant();
