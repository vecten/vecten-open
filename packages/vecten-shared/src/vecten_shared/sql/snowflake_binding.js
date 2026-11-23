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
function roleKey(type, name) {
    return type.replace(/ /g, '_') + ':' + name;
}
function grants(rows, future, members) {
    var result = [];
    while (rows.next()) {
        var kind = rows.getColumnValue(future ? 'grant_on' : 'granted_on').replace(/ /g, '_');
        var name = rows.getColumnValue('name');
        var holder = roleKey(rows.getColumnValue(future ? 'grant_to' : 'granted_to'),
                             rows.getColumnValue('grantee_name'));
        var privilege = rows.getColumnValue('privilege');
        if (!future && privilege === 'USAGE' &&
            ['ROLE', 'DATABASE_ROLE', 'APPLICATION_ROLE'].indexOf(kind) !== -1) {
            (members[holder] || (members[holder] = [])).push(roleKey(kind, name));
        }
        result.push({holder: holder, kind: kind, name: name, privilege: privilege});
    }
    return result;
}
function reachable(start, members) {
    var result = Object.create(null);
    var pending = [roleKey('ROLE', start)];
    while (pending.length) {
        var role = pending.pop();
        if (result[role]) continue;
        result[role] = true;
        (members[role] || []).forEach(function(child) { pending.push(child); });
    }
    return result;
}
var members = Object.create(null);
var current = grants(hierarchy(TARGET, false), false, members);
var baseline = reachable('PUBLIC', members);
var candidate = reachable(TARGET, members);
function check(rows) {
    for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        // INCLUDE HIERARCHY adds PUBLIC independently of the candidate's own memberships.
        if (baseline[row.holder] && !candidate[row.holder]) continue;
        var kind = row.kind, name = row.name, privilege = row.privilege;
        if (kind === 'ACCOUNT') return 'the role hierarchy holds ' + privilege + ' on the account';
        if (kind === 'ROLE' && (projectRole.test(name) || admins.indexOf(name) !== -1)) {
            return 'the role hierarchy reaches protected role ' + name;
        }
        // Snowflake does not expand these memberships in INCLUDE HIERARCHY.
        if (['INSTANCE_ROLE', 'SERVICE_ROLE', 'CLASS_ROLE'].indexOf(
                kind) !== -1) {
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
return check(current) || check(grants(hierarchy(TARGET, true), true, members)) || 'ok';
