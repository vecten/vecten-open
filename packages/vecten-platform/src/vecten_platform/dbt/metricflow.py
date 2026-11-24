"""Check the Ossie-derived manifest with the semantic rules dbt runs when metrics exist."""

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
from metricflow_semantic_interfaces.validations.semantic_manifest_validator import (
    SemanticManifestValidator,
)
from metricflow_semantic_interfaces.validations.unique_valid_name import (
    PrimaryEntityDimensionPairs,
    UniqueAndValidNameRule,
)
from metricflow_semantic_interfaces.validations.validator_helpers import ValidationError

from vecten_shared.osi.issues import Issue


def semantic_manifest_issues(osi_dir: Path) -> list[Issue]:
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
    remedies = {
        PrimaryEntityRule: "Declare a single unique, non-null key column for the mart.",
        ElementConsistencyRule: "Give unrelated columns distinct aliases in the mart models.",
        PrimaryEntityDimensionPairs: "Give the overlapping key or dimension a distinct alias.",
        UniqueAndValidNameRule: "Use distinct, lowercase MetricFlow identifiers.",
    }
    for rule in SemanticManifestValidator.DEFAULT_RULES:
        for finding in rule.validate_manifest(manifest):
            if isinstance(finding, ValidationError):
                message = finding.message
                if remedy := remedies.get(type(rule)):
                    message = f"{message} {remedy}"
                issues.append(Issue("dbt_semantic_model", message))
    return issues
