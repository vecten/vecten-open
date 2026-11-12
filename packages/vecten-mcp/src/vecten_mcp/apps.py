"""Analytics apps the shipped bundle binds, the scope the server holds their principals to, and
the groups each app admits.

`vecten.toml` binds an app to groups and `extensions.lock.json` records the datasets it
declares; both come with the bundle, and a ship restarts the server with the new pair. An app
principal is a service principal stored under APP_PRINCIPALS with the app it serves (`vecten
app connect` stores it). It reads only its app's declared datasets: describe, search and
query_metrics see a model holding only those, and sql-exec reads only their tables (app_sql).
"""

import copy
import dataclasses
import re
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from vecten_mcp.app_sql import relation, sql_problem
from vecten_mcp.model import source_tables
from vecten_shared.osi.sql_policy import source_sql

# Records kind: principal id -> {"extension", "app"} for each app principal.
APP_PRINCIPALS = "app_principals"
# Records kind: "<extension>.<app>" -> the last admission computed for that app.
ADMISSION = "app_admission"
# An email as X-Vecten-User carries it: RFC 5321's length limit, visible characters only.
VIEWER_LIMIT = 254
# Why a snowflake app has no URL: Vecten does not create the objects that would publish it.
SNOWFLAKE_UNPUBLISHED = (
    "Streamlit in Snowflake needs warehouse resources Vecten does not create yet: an apps "
    "schema, a stage per app and an app role owning the Streamlit object"
)


@dataclass(frozen=True)
class BoundApp:
    extension: str
    name: str
    title: str
    kind: str
    datasets: tuple[str, ...]
    groups: tuple[str, ...]
    version: str = ""
    description: str = ""
    # A container app's hostname label below the environment's domain; None for any other app.
    hostname: str | None = None

    @property
    def key(self):
        return f"{self.extension}.{self.name}"


@dataclass(frozen=True)
class AppScope:
    app: str
    datasets: frozenset[str]


def read_bound_apps(root, config):
    """Each bound app by name (one name binds once across extensions); an app the record does
    not declare is kept with no datasets, so its principal reads nothing."""
    from vecten_shared.extension_record import read_record

    record = read_record(root) or {}
    found = {}
    for extension, bindings in sorted(config.apps.items()):
        entry = record.get(extension)
        for name, binding in sorted(bindings.items()):
            declared = entry.apps.get(name) if entry else None
            container = declared is not None and declared.service is not None
            found[name] = BoundApp(
                extension=extension,
                name=name,
                title=declared.title if declared else name,
                kind=declared.kind if declared else "undeclared",
                datasets=tuple(declared.datasets) if declared else (),
                groups=tuple(binding.groups),
                version=entry.version if entry else "",
                description=declared.description if declared else "",
                hostname=(binding.hostname or name) if container else None,
            )
    return found


def app_url(issuer, label):
    """Where a container app with hostname `label` is served: beside the MCP, whose own name
    is `mcp.<domain>` (with the laptop's published port), so `@` is the domain itself."""
    if label is None:
        return None
    parts = urlsplit(issuer)
    host = parts.hostname or ""
    if not host.startswith("mcp."):
        return None
    domain = host.removeprefix("mcp.")
    name = domain if label == "@" else f"{label}.{domain}"
    port = f":{parts.port}" if parts.port else ""
    return f"https://{name}{port}/"


def principal_name(app):
    """The warehouse name `vecten app connect` gives app `app`'s principal."""
    return "svc_app_" + re.sub(r"[^a-z0-9]+", "_", app.lower()).strip("_")


def scoped_model(model, datasets):
    """The model as an app principal sees it: its declared datasets, their metrics and the
    relationships between them."""
    kept = {name: data for name, data in model.datasets.items() if name in datasets}
    homes = {name: home for name, home in model.homes.items() if home in kept}
    return dataclasses.replace(
        model,
        datasets=kept,
        metrics={name: data for name, data in model.metrics.items() if name in homes},
        homes=homes,
        relationships={
            name: data
            for name, data in model.relationships.items()
            if data["from"] in kept and data["to"] in kept
        },
    )


def declared_relations(model, datasets):
    """The tables sql-exec may read for these datasets. A dataset whose source is a query
    names no table: its rows are read through query_metrics, which applies that query."""
    relations = set()
    for name in datasets:
        dataset = model.datasets.get(name)
        if dataset is None:
            continue
        source = source_sql(dataset["source"], model.kind)
        if not source.startswith("("):
            relations.add(relation(source, model.kind))
    return frozenset(relations)


def viewer(value):
    """The email an app passes as X-Vecten-On-Behalf-Of, percent-decoded, or None."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 3 * VIEWER_LIMIT:
        return None
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return None
    if (
        len(decoded) > VIEWER_LIMIT
        or decoded.count("@") != 1
        or not all(char.isprintable() and not char.isspace() for char in decoded)
    ):
        return None
    local, domain = decoded.split("@")
    return decoded.lower() if local and domain else None


class AppScopes:
    def __init__(self, state, apps, *, kind, database=None):
        self.state, self.apps, self.kind = state, dict(apps), kind
        # The warehouse database a schema grant applies in (Snowflake); None where a source
        # cannot name another database (Postgres).
        self.database = database

    def mark(self, principal_id, extension, app):
        """Records `principal_id` as the principal of `extension`'s app `app`."""
        principal = self.state.principal(principal_id)
        if not principal or principal["kind"] != "service":
            raise ValueError("an app principal must be a service principal")
        self.state.put(APP_PRINCIPALS, principal_id, {"extension": extension, "app": app})

    def principal(self, app):
        """The service principal named as `app`'s, with whether it is marked as it, or None."""
        target = principal_name(app.name)
        found = next(
            (
                p
                for p in self.state.principals()
                if p["kind"] == "service" and p["warehouse_name"] == target
            ),
            None,
        )
        if found is None:
            return None
        marker = self.state.get(APP_PRINCIPALS, found["id"])
        return {
            "id": found["id"],
            "warehouse_name": target,
            "marked": marker == {"extension": app.extension, "app": app.name},
            "enabled": bool(found["enabled"]),
        }

    def listing(self, issuer):
        """Every bound app as the Analytics screen, the admin API and the admin tool show it."""
        failures = self.health()
        entries = []
        for app in sorted(self.apps.values(), key=lambda a: a.key):
            published = app.kind == "container"
            entries.append(
                {
                    "app": app.key,
                    "extension": app.extension,
                    "version": app.version,
                    "name": app.name,
                    "title": app.title,
                    "description": app.description,
                    "kind": app.kind,
                    "url": app_url(issuer, app.hostname) if published else None,
                    "groups": list(app.groups),
                    "admitted": [g for g in app.groups if g in self.admitted(app.name)],
                    "datasets": list(app.datasets),
                    "state": "bound" if published else "not published",
                    "reason": None if published else self._unpublished(app),
                    # `vecten ship` reports a service's health to the person who ships;
                    # nothing hands it to the MCP.
                    "health": None,
                    "binding": f"[apps.{app.extension}.{app.name}] in vecten.toml",
                    "principal": self.principal(app),
                    "failures": [f for f in failures if f["app"] == app.key],
                }
            )
        return entries

    @staticmethod
    def _unpublished(app):
        if app.kind == "snowflake":
            return SNOWFLAKE_UNPUBLISHED
        return f"extensions.lock.json declares no app {app.name} for {app.extension}"

    def scope(self, principal_id):
        """The scope of an app principal, or None for any other principal. A principal whose
        app is no longer bound, or whose record is malformed, has a scope with no datasets."""
        marker = self.state.get(APP_PRINCIPALS, principal_id)
        if marker is None:
            return None
        if not isinstance(marker, dict):
            return AppScope("", frozenset())
        extension, name = marker.get("extension"), marker.get("app")
        key = f"{extension}.{name}"
        bound = self.apps.get(name)
        if bound is None or bound.key != key:
            return AppScope(key, frozenset())
        return AppScope(key, frozenset(bound.datasets))

    def sql_problem(self, scope, model, text):
        relations = declared_relations(model, scope.datasets) if model else frozenset()
        return sql_problem(text, kind=self.kind, relations=relations)

    def missing(self, model):
        """Model-health warnings: each declared dataset the loaded model lacks."""
        return [
            {
                "code": "app_dataset_missing",
                "app": app.key,
                "dataset": dataset,
                "message": f"app {app.key} declares {dataset}, which the model does not hold",
            }
            for app in self.apps.values()
            for dataset in app.datasets
            if model is None or dataset not in model.datasets
        ]

    # Grant kinds and the schemas each covers. A restricted-area grant (#51) adds
    # "area": lambda target: {f"core_{target}", f"marts_{target}"} here.
    SCHEMAS = {"schema": lambda target: {target}}

    def _folded(self, name):
        """A grant's schema name as the warehouse resolves it unquoted."""
        return normalize_identifiers(exp.to_identifier(name), dialect=self.kind).name

    def _covers(self, grants, dataset, source):
        # A dataset grant counts only on the table the dataset reads now: after a move whose
        # revoke failed, the warehouse may still hold it on the old one alone.
        if any(
            g["kind"] == "dataset" and g["target"] == dataset and g.get("source") == source
            for g in grants
        ):
            return True
        schemas = set()
        for grant in grants:
            covered = self.SCHEMAS.get(grant["kind"])
            if covered:
                schemas |= {self._folded(name) for name in covered(grant["target"])}
        try:
            tables = [
                normalize_identifiers(table, dialect=self.kind)
                for table in source_tables(source, self.kind)
            ]
        except ValueError:
            return False
        return bool(tables) and all(
            table.db
            and table.db in schemas
            and (
                not table.catalog
                or self.database is None
                or table.catalog.casefold() == self.database.casefold()
            )
            for table in tables
        )

    def admit(self, datasets):
        """Recomputes every bound app's admitted groups from the grants the warehouse holds
        and the model's dataset sources (`datasets`: name -> source). A grant counts once it
        is both desired and applied (as applied, with the table it was applied to), so a
        revocation or a failed grant admits no one."""
        desired = dict(self.state.scan("grants"))
        applied = dict(self.state.scan("applied_grants"))
        held = [row for key, row in applied.items() if key in desired]
        known = {group for app in self.apps.values() for group in app.groups}
        existing = {group for group in known if self.state.group(group)}
        now = time.time()
        for app in self.apps.values():
            admitted, excluded = [], []
            for group in app.groups:
                if group not in existing:
                    excluded.append({"group": group, "reason": "no such group"})
                    continue
                grants = [row for row in held if row["group"] == group]
                # A dataset the model does not name has no source to check grants against,
                # so it admits no one until the model holds it.
                blocked = next(
                    (
                        name
                        for name in app.datasets
                        if name not in datasets or not self._covers(grants, name, datasets[name])
                    ),
                    None,
                )
                if blocked is None:
                    admitted.append(group)
                elif blocked in datasets:
                    excluded.append(
                        {"group": group, "dataset": blocked, "source": datasets[blocked]}
                    )
                else:
                    excluded.append(
                        {"group": group, "dataset": blocked, "reason": "not in the model"}
                    )
            self.state.put(
                ADMISSION,
                app.key,
                {"admitted": admitted, "excluded": excluded, "checked_at": now},
            )
        current = {app.key for app in self.apps.values()}
        for key, _ in self.state.scan(ADMISSION):
            if key not in current:
                self.state.remove(ADMISSION, key)

    def admitted(self, name):
        """The groups through which the gate admits a person to app `name`: the last computed
        admission, within the groups the running bundle binds; none before one is computed."""
        app = self.apps.get(name)
        record = self.state.get(ADMISSION, app.key) if app else None
        if not record:
            return frozenset()
        return frozenset(record.get("admitted", [])) & set(app.groups)

    def health(self):
        """Access-health entries: each bound group an app does not admit, and why."""
        entries = []
        for app in self.apps.values():
            record = self.state.get(ADMISSION, app.key) or {}
            for item in record.get("excluded", []):
                entry = {"app": app.key, **copy.deepcopy(item)}
                if "source" in item:
                    entry["message"] = (
                        f"group {item['group']} may open {app.key} but cannot read {item['source']}"
                    )
                elif "dataset" in item:
                    entry["message"] = (
                        f"{app.key} declares {item['dataset']}, which the model does not hold, "
                        f"so it admits no group"
                    )
                else:
                    entry["message"] = (
                        f"{app.key} is bound to {item['group']}, which does not exist"
                    )
                entries.append(entry)
        entries.extend(self._unmarked())
        return entries

    def _unmarked(self):
        """A service principal named as a bound app's principal but not marked as one reads
        everything its groups can; each is listed so an admin marks or removes it."""
        marked = {key for key, _ in self.state.scan(APP_PRINCIPALS)}
        names = {principal_name(app.name): app for app in self.apps.values()}
        return [
            {
                "app": names[p["warehouse_name"]].key,
                "principal_id": p["id"],
                "warehouse_name": p["warehouse_name"],
                "reason": "not marked as the app's principal",
                "message": (
                    f"service principal {p['warehouse_name']} is named as "
                    f"{names[p['warehouse_name']].key}'s principal but is not marked as it, so "
                    "it is not held to the app's datasets; review who uses it, then run "
                    f"`vecten app connect {names[p['warehouse_name']].key} --env <env> --adopt` "
                    "or delete it"
                ),
            }
            for p in self.state.principals()
            if p["kind"] == "service" and p["warehouse_name"] in names and p["id"] not in marked
        ]
