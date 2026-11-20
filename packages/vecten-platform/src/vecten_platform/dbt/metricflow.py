"""Check dataset rules that dbt activates when its manifest contains metrics."""

from pathlib import Path

from metricflow.converters.models import OSIDocument
from metricflow.converters.osi_to_msi import OSIToMSIConverter
from metricflow_semantic_interfaces.implementations.project_configuration import (
    PydanticProjectConfiguration,
)
from metricflow_semantic_interfaces.implementations.semantic_manifest import (
    PydanticSemanticManifest,
)
from metricflow_semantic_interfaces.validations.element_const import ElementConsistencyRule
from metricflow_semantic_interfaces.validations.primary_entity import PrimaryEntityRule
from metricflow_semantic_interfaces.validations.unique_valid_name import (
    PrimaryEntityDimensionPairs,
    UniqueAndValidNameRule,
)

from vecten_shared.osi.issues import Issue


def dataset_issues(osi_dir: Path) -> list[Issue]:
    # dbt converts each document independently, then validates all its models together.
    models = []
    metrics = []
    for path in sorted(osi_dir.rglob("*.json")):
        try:
            document = OSIDocument.parse_raw(path.read_text(encoding="utf-8"))
            converted = OSIToMSIConverter().convert(document).output
        except (ValueError, RuntimeError) as exc:
            return [Issue("dbt_osi_document", str(exc), path.name)]
        models.extend(converted.semantic_models)
        metrics.extend(converted.metrics)
    if not models or not metrics:
        return []
    manifest = PydanticSemanticManifest(
        semantic_models=models,
        metrics=metrics,
        project_configuration=PydanticProjectConfiguration(),
    )
    issues = []
    for rule, remedy in (
        (PrimaryEntityRule, "Declare a single unique, non-null key column for the mart."),
        (ElementConsistencyRule, "Give unrelated columns distinct aliases in the mart models."),
        (PrimaryEntityDimensionPairs, "Give the overlapping key or dimension a distinct alias."),
        (UniqueAndValidNameRule, "Use distinct, lowercase MetricFlow identifiers."),
    ):
        for error in rule.validate_manifest(manifest):
            issues.append(Issue("dbt_semantic_model", f"{error.message} {remedy}"))
    return issues
