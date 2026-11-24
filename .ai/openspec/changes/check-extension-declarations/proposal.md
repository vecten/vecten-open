# Read-only in-repo declaration check

`vecten lock --check` SHALL compare each registered in-repo extension's manifest, services and apps against `extensions.lock.json` without updating project records or resolving packages. Generated project CI SHALL run this check without private Git credentials.
