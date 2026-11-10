"""Know-how shipped inside the package: guides, skills and the cost table."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from importlib import resources
from typing import Any, Literal

import yaml

GuideName = Literal["guide", "cloud", "domain", "github"]
GUIDES: tuple[str, ...] = ("guide", "cloud", "domain", "github")
ENTRY_SKILL = "guide"
SKILL_FILE = "SKILL.md"
FRONTMATTER_FENCE = "---"


def load_guide(name: GuideName) -> str:
    # The entry text is the guide skill's body, so `vecten guide` and the skill never diverge.
    if name == ENTRY_SKILL:
        return skill_body(ENTRY_SKILL)
    return resources.files("vecten.knowhow").joinpath(f"{name}.md").read_text(encoding="utf-8")


def _skills_root():
    return resources.files("vecten.knowhow").joinpath("skills")


def skill_names() -> list[str]:
    root = _skills_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.joinpath(SKILL_FILE).is_file())


def load_skill(name: str) -> str:
    # Only listed names reach the filesystem, so a name cannot walk out of `skills/`.
    if name not in skill_names():
        raise ValueError(f"no skill named {name!r}; skills: {', '.join(skill_names())}")
    return _skills_root().joinpath(name, SKILL_FILE).read_text(encoding="utf-8")


def _split(name: str) -> tuple[str, str]:
    text = load_skill(name)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        raise ValueError(f"skill {name} has no frontmatter")
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONTMATTER_FENCE:
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    raise ValueError(f"skill {name}: the frontmatter is never closed")


def skill_body(name: str) -> str:
    return _split(name)[1].lstrip("\n")


def skill_frontmatter(name: str) -> dict[str, Any]:
    meta = yaml.safe_load(_split(name)[0])
    if not isinstance(meta, dict):
        raise ValueError(f"skill {name}: the frontmatter is not a mapping")
    return meta


@dataclass(frozen=True)
class CostLine:
    item: str
    usd: float


@dataclass(frozen=True)
class Estimate:
    monthly_usd: float
    lines: list[CostLine]


@dataclass(frozen=True)
class Costs:
    gathered_on: date
    table: dict[str, Any]

    @property
    def clouds(self) -> list[str]:
        return [k for k in self.table if k != "gathered_on"]

    @property
    def tiers(self) -> list[str]:
        return ["small", "medium", "large"]

    def estimate(
        self,
        cloud: str,
        size: str,
        components: list[str],
        profile: str,
        warehouse: str = "postgres",
    ) -> Estimate:
        t = self.table[cloud]
        components = list(dict.fromkeys(components))
        both = set(components) == {"platform", "mcp"}
        lines: list[CostLine] = []
        if profile == "minimal":
            if both:
                lines.append(CostLine("VM (both components)", t["vm"]["minimal"]))
            else:
                only = components[0]
                vm_class = "small" if only == "platform" else size
                lines.append(CostLine(f"VM ({only})", t["vm"][vm_class]))
            builds_warehouse = "platform" in components and warehouse == "postgres"
            if builds_warehouse:
                # A cloud whose minimal instance class differs from the tier's warehouse
                # class prices it under `postgres_minimal`.
                usd = t.get("postgres_minimal", {}).get(size, t["postgres"][size])
            else:
                usd = t["postgres"]["state"]
            lines.append(CostLine("Postgres (all databases)", usd))
        else:
            if "platform" in components:
                lines.append(CostLine("platform VM", t["vm"]["small"]))
            if "mcp" in components:
                lines.append(CostLine("MCP VM", t["vm"][size]))
            lines.append(CostLine("state Postgres", t["postgres"]["state"]))
            if "platform" in components and warehouse != "snowflake":
                lines.append(CostLine("warehouse Postgres", t["postgres"][size]))
        lines.append(CostLine("object storage and DNS", t["fixed"]["storage_dns"]))
        return Estimate(monthly_usd=round(sum(line.usd for line in lines), 2), lines=lines)


def load_costs() -> Costs:
    text = resources.files("vecten.knowhow").joinpath("costs.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    return Costs(gathered_on=raw["gathered_on"], table=raw)
