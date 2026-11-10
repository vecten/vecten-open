"""A validated semantic snapshot used by offline compilation and the MCP runtime."""

import copy
import difflib
import json
import re
from dataclasses import dataclass
from functools import cached_property

from sqlglot import exp

from vecten_shared.osi.documents import list_documents, load_document, merge
from vecten_shared.osi.expressions import selected_expression
from vecten_shared.osi.extensions import read_vecten_extension
from vecten_shared.osi.issues import ModelError
from vecten_shared.osi.validate import validate_osi


def classification(field):
    if "dimension" not in field:
        return "fact"
    return "time_dimension" if field["dimension"].get("is_time") else "dimension"


def additivity(metric, tree):
    extension = read_vecten_extension(metric) or {}
    if "additivity" in extension:
        return extension["additivity"]
    aggregates = list(tree.find_all(exp.AggFunc))
    if (
        aggregates
        and all(isinstance(node, (exp.Sum, exp.Count)) for node in aggregates)
        and not any(tree.find_all(exp.Distinct, exp.Div, exp.Window))
    ):
        return "additive"
    return "non_additive"


@dataclass(frozen=True)
class SemanticModel:
    datasets: dict
    metrics: dict
    relationships: dict
    homes: dict
    kind: str
    commit: str
    report: dict

    @classmethod
    def load(cls, directory, *, kind="postgres", commit="local"):
        report = validate_osi(directory, warehouse_kind=kind)
        if not report.ok:
            raise ModelError(report.errors)
        merged = merge([load_document(path) for path in list_documents(directory)])
        datasets = {name: copy.deepcopy(data) for name, (data, _) in merged.datasets.items()}
        metrics = {name: copy.deepcopy(data) for name, (data, _) in merged.metrics.items()}
        for dataset in datasets.values():
            for field in dataset.get("fields", []):
                field["classification"] = classification(field)
                field["expression_sql"] = selected_expression(field, kind).sql(dialect=kind)
        for metric in metrics.values():
            tree = selected_expression(metric, kind)
            extension = read_vecten_extension(metric) or {}
            metric["expression_sql"] = tree.sql(dialect=kind)
            metric["additivity"] = additivity(metric, tree)
            rule = {k: extension[k] for k in ("non_additive_over", "rollup") if k in extension}
            if rule:
                metric["additivity_rule"] = rule
            metric["verified"] = extension.get("verified")
        return cls(
            datasets,
            metrics,
            {n: d for n, (d, _) in merged.relationships.items()},
            report.homes,
            kind,
            commit,
            report.as_data(),
        )

    def fields(self, dataset):
        return {f["name"]: f for f in self.datasets[dataset].get("fields", [])}

    @cached_property
    def source_table_index(self):
        """Physical source names, parsed once for this immutable model snapshot."""
        from vecten_shared.osi.sql_policy import read_statement, source_sql

        index = {}
        for name, dataset in self.datasets.items():
            statement = read_statement(
                "SELECT * FROM " + source_sql(dataset["source"], self.kind), kind=self.kind
            )
            for table in statement.tables:
                parts = tuple(table.replace('"', "").casefold().split("."))
                index.setdefault(parts, set()).add(name)
        return index

    @cached_property
    def field_references(self):
        return {
            dataset: {
                field["name"].casefold(): frozenset(
                    column.name.casefold()
                    for column in selected_expression(field, self.kind).find_all(exp.Column)
                )
                for field in definition.get("fields", [])
            }
            for dataset, definition in self.datasets.items()
        }

    @cached_property
    def metric_references(self):
        return {
            name: frozenset(
                (self.homes[name] + "." + column.name).casefold()
                for column in selected_expression(metric, self.kind).find_all(exp.Column)
            )
            for name, metric in self.metrics.items()
        }

    def metric_columns(self, name):
        return self.metric_references[name]

    def visibility(self, denied):
        hidden = {name.casefold() for name in denied}
        metadata_names = {}
        for dataset, references in self.field_references.items():
            prefix = dataset.casefold() + "."
            seeds = {name[len(prefix) :] for name in hidden if name.startswith(prefix)}
            blocked = set(seeds)
            while True:
                derived = {name for name, columns in references.items() if columns & blocked}
                if derived <= blocked:
                    break
                blocked.update(derived)
            hidden.update(prefix + name for name in references if name in blocked)
            metadata_names[dataset] = blocked
        return hidden, metadata_names

    @staticmethod
    def visible_metadata(definition, blocked):
        data = copy.deepcopy(definition)
        if blocked:
            pattern = re.compile(
                r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(blocked))) + r")(?!\w)"
            )
            for key in ("source", "description", "ai_context", "custom_extensions"):
                if key in data and pattern.search(
                    json.dumps(data[key], ensure_ascii=False).casefold()
                ):
                    data.pop(key)
        return data

    def visible_metrics(self, denied):
        denied, _ = self.visibility(denied)
        return {
            name: metric
            for name, metric in self.metrics.items()
            if not self.metric_columns(name) & set(denied)
        }

    def describe(self, dataset=None, *, denied=()):
        metrics = self.visible_metrics(denied)
        denied, metadata_names = self.visibility(denied)
        if dataset is not None and dataset not in self.datasets:
            raise ValueError("unknown dataset")
        names = {dataset} if dataset else set(self.datasets)
        datasets = []
        for name in sorted(names):
            data = self.visible_metadata(self.datasets[name], metadata_names[name])
            data["fields"] = [
                self.visible_metadata(f, metadata_names[name])
                for f in data.get("fields", [])
                if (name + "." + f["name"]).casefold() not in denied
            ]
            for key in ("primary_key", "unique_keys"):
                if key in data:
                    data.pop(key)
            datasets.append(data)
        relationships = []
        for relationship in self.relationships.values():
            columns = {
                relationship[end] + "." + col
                for end in ("from", "to")
                for col in relationship[end + "_columns"]
            }
            if (
                names & {relationship["from"], relationship["to"]}
                and not {name.casefold() for name in columns} & denied
            ):
                blocked = metadata_names[relationship["from"]] | metadata_names[relationship["to"]]
                relationships.append(self.visible_metadata(relationship, blocked))
        return {
            "model": self.commit,
            "datasets": datasets,
            "metrics": [
                self.visible_metadata(m, metadata_names[self.homes[n]])
                for n, m in metrics.items()
                if self.homes[n] in names
            ],
            "relationships": relationships,
        }

    def search(self, query, *, denied=()):
        words = query.casefold().split()
        if not words:
            return []
        visible = self.describe(denied=denied)
        entries = []
        for dataset in visible["datasets"]:
            name = dataset["name"]
            entries.append(
                {
                    "kind": "dataset",
                    "name": name,
                    "description": dataset.get("description", ""),
                    "ai_context": dataset.get("ai_context", ""),
                }
            )
            entries.extend(
                {"kind": "field", "dataset": name, **copy.deepcopy(field)}
                for field in dataset.get("fields", [])
            )
        entries.extend(
            {"kind": "metric", "dataset": self.homes[metric["name"]], **copy.deepcopy(metric)}
            for metric in visible["metrics"]
        )
        return [
            item
            for item in entries
            if all(
                word
                in json.dumps(
                    {k: item.get(k) for k in ("name", "description", "ai_context")}
                ).casefold()
                for word in words
            )
        ][:100]

    def suggestions(self, value, denied):
        """The nearest visible metric and field names, matched on names and synonyms."""
        offered = {}

        def offer(name, element):
            offered.setdefault(name, name)
            context = element.get("ai_context")
            synonyms = context.get("synonyms") if isinstance(context, dict) else None
            for synonym in synonyms or []:
                if isinstance(synonym, str):
                    offered.setdefault(synonym, name)

        for name, metric in self.visible_metrics(denied).items():
            offer(name, metric)
        denied, _ = self.visibility(denied)
        for dataset in self.datasets:
            for name, field in self.fields(dataset).items():
                if (dataset + "." + name).casefold() not in denied:
                    offer(name, field)
        matches = difflib.get_close_matches(value, sorted(offered), n=10, cutoff=0.4)
        return list(dict.fromkeys(offered[match] for match in matches))[:5]

    def compile(self, query, *, denied=(), row_cap=1000):
        from vecten_shared.osi.compiler import Compiler

        return Compiler(self, denied, row_cap).compile(query)
