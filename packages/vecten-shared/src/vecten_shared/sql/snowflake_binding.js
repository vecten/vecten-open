// The installer supplies the unquoted database name for this shared-account environment.
if (typeof TARGET !== 'string' || !/^[A-Z_][A-Z0-9_]{0,62}$/.test(TARGET)) {
    throw new Error('invalid bound role');
}
function execute(sql) { return snowflake.execute({sqlText: sql}); }
function hierarchy(role, future) {
    var identifier = '"' + role.replace(/"/g, '""') + '"';
    return execute('SHOW ' + (future ? 'FUTURE ' : '') + 'GRANTS TO ROLE ' +
                   identifier + ' INCLUDE HIERARCHY');
}
// SHOW can silently omit grants after the procedure's owner loses MANAGE GRANTS.
var owner = execute('SELECT INVOKER_ROLE()');
owner.next();
var rights = hierarchy(owner.getColumnValue(1), false);
var visible = false;
while (rights.next()) {
    if (rights.getColumnValue('granted_on') === 'ACCOUNT' &&
        rights.getColumnValue('privilege') === 'MANAGE GRANTS') {
        visible = true;
        break;
    }
}
if (!visible) return 'the binding check requires a procedure owner with MANAGE GRANTS';

var base = database.slice(0, database.lastIndexOf('_'));
// The shared layout restricts the base to uppercase letters, digits and underscores.
var projectDatabase = new RegExp('^' + base + '_[A-Z][A-Z0-9]*$');
var projectRole = new RegExp('.+_' + base + '_[A-Z][A-Z0-9]*$');
var admins = ['ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN'];
function check(rows, future) {
    while (rows.next()) {
        var kind = rows.getColumnValue(future ? 'grant_on' : 'granted_on');
        var name = rows.getColumnValue('name');
        var privilege = rows.getColumnValue('privilege');
        if (kind === 'ACCOUNT') return 'the role hierarchy holds ' + privilege + ' on the account';
        if (kind === 'ROLE' && (projectRole.test(name) || admins.indexOf(name) !== -1)) {
            return 'the role hierarchy reaches protected role ' + name;
        }
        // Snowflake does not expand these memberships in INCLUDE HIERARCHY.
        if (['INSTANCE_ROLE', 'SERVICE_ROLE', 'CLASS_ROLE'].indexOf(
                kind.replace(/ /g, '_')) !== -1) {
            return 'the role hierarchy contains a ' + kind + ' whose grants cannot be verified';
        }
        // Only the first identifier denotes the database. A dot inside a quoted name is literal.
        var first = /^(?:"((?:[^"]|"")*)"|([^."]+))(\.|$)/.exec(name);
        if (first && (kind === 'DATABASE' || first[3] === '.')) {
            var catalog = first[1] === undefined ? first[2] : first[1].replace(/""/g, '"');
            if (catalog !== database && projectDatabase.test(catalog)) {
                return 'the role hierarchy reaches another environment database ' + catalog;
            }
            if (kind === 'DATABASE' && catalog === database && privilege === 'OWNERSHIP') {
                return 'the role hierarchy owns the environment database ' + database;
            }
        }
    }
    return null;
}
return check(hierarchy(TARGET, false), false) || check(hierarchy(TARGET, true), true) || 'ok';
