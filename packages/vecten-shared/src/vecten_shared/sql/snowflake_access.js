// The procedure's wrapper defines database, warehouse, analystRole, provisionerRole,
// loaderRole and suffix from the environment's Snowflake names.
function quoted(value) {
    if (typeof value !== 'string' || value.length === 0 || value.indexOf('\u0000') !== -1) {
        throw new Error('invalid identifier');
    }
    return '"' + value.replace(/"/g, '""') + '"';
}
function execute(sqlText) { return snowflake.execute({sqlText: sqlText}); }
// Snowflake passes a BOOLEAN argument as the number 1 or 0 and SQL NULL as undefined.
var enabled = ENABLED === true || ENABLED === 1 ? true
    : ENABLED === false || ENABLED === 0 ? false : null;
var relation = RELATION_NAME === undefined ? null : RELATION_NAME;
// A group's role is at most 63 characters before the environment's suffix.
if (enabled === null || typeof TARGET !== 'string' ||
    TARGET.length > 63 + suffix.length || !/^[A-Z][A-Z0-9_]*$/.test(TARGET) ||
    /^(ADMIN_|SVC_|PG_)/.test(TARGET) ||
    ['PUBLIC','ACCOUNTADMIN','SECURITYADMIN','SYSADMIN','USERADMIN',
     'ORGADMIN','IMPERSONATOR','WAREHOUSE_OWNER',
     provisionerRole,loaderRole].indexOf(TARGET) !== -1) {
    throw new Error('target is not a bounded reader role');
}
if (typeof SCHEMA_NAME !== 'string' ||
    (['CORE','MARTS','STAGING','SNAPSHOTS'].indexOf(SCHEMA_NAME) === -1 &&
     !/^RAW_[A-Z0-9_]+$/.test(SCHEMA_NAME))) {
    throw new Error('schema is outside the approved convention');
}
// A first filter only: another project's suffix can end in this one's (`_X_ACME_DEV` ends in
// `_ACME_DEV`), so ownership by this environment's provisioner stays the guard.
if (suffix !== '' && (TARGET.length <= suffix.length ||
    TARGET.slice(TARGET.length - suffix.length) !== suffix)) {
    throw new Error('target is not a role of this environment');
}
if (TARGET !== analystRole) {
    var grants = execute('SHOW GRANTS ON ROLE ' + quoted(TARGET));
    var owned = false;
    while (grants.next()) {
        if (grants.getColumnValue('privilege') === 'OWNERSHIP' &&
            grants.getColumnValue('granted_to') === 'ROLE' &&
            grants.getColumnValue('grantee_name') === provisionerRole) owned = true;
    }
    if (!owned) throw new Error('target is not owned by provisioner');
}
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
