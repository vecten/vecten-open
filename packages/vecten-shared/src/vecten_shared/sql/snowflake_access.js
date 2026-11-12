function quoted(value) {
    if (typeof value !== 'string' || value.length === 0 || value.indexOf('\u0000') !== -1) {
        throw new Error('invalid identifier');
    }
    return '"' + value.replace(/"/g, '""') + '"';
}
function execute(sqlText) { return snowflake.execute({sqlText: sqlText}); }
// READER is reserved for a reader of every schema, which no grant here may create.
function bounded(target) {
    if (typeof target !== 'string' ||
        !/^[A-Z][A-Z0-9_]{0,62}$/.test(target) ||
        /^(ADMIN_|SVC_|PG_)/.test(target) ||
        ['PUBLIC','ACCOUNTADMIN','SECURITYADMIN','SYSADMIN','USERADMIN','ORGADMIN',
         'PROVISIONER','IMPERSONATOR','LOADER','WAREHOUSE_OWNER','READER'].indexOf(target) !== -1) {
        throw new Error('target is not a bounded reader role');
    }
}
function owned(target) {
    if (target === 'ANALYST') return;
    var grants = execute('SHOW GRANTS ON ROLE ' + quoted(target));
    var found = false;
    while (grants.next()) {
        if (grants.getColumnValue('privilege') === 'OWNERSHIP' &&
            grants.getColumnValue('granted_to') === 'ROLE' &&
            grants.getColumnValue('grantee_name') === 'PROVISIONER') found = true;
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
    var relationType = 'TABLE';
    if (relation !== null) {
        quoted(relation);
        var table = snowflake.execute({
            sqlText: 'SELECT TABLE_TYPE FROM ' + quoted(database) + '.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=? AND TABLE_NAME=?',
            binds: [SCHEMA_NAME, relation]
        });
        if (!table.next()) throw new Error('dataset relation does not exist');
        if (table.getColumnValue(1) === 'VIEW') relationType = 'VIEW';
    }
    if (enabled) {
        execute('GRANT USAGE ON DATABASE ' + quoted(database) + ' TO' + target);
        if (warehouse !== null) execute('GRANT USAGE ON WAREHOUSE ' + quoted(warehouse) + ' TO' + target);
        execute('GRANT USAGE ON SCHEMA ' + schema + ' TO' + target);
    }
    if (relation !== null) {
        execute(action + ' SELECT ON ' + relationType + ' ' + schema + '.' + quoted(relation) + direction + target);
    } else {
        ['TABLES','VIEWS'].forEach(function(kind) {
            ['ALL','FUTURE'].forEach(function(shape) {
                execute(action + ' SELECT ON ' + shape + ' ' + kind + ' IN SCHEMA ' + schema + direction + target);
            });
        });
        if (!enabled) execute('REVOKE USAGE ON SCHEMA ' + schema + ' FROM' + target);
    }
    return 'ok';
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
