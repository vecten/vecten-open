from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.knowhow import GUIDES, load_costs, load_guide
from vecten_shared.output import ExitCode, Failure, Result

VALID_SIZES = ("small", "medium", "large")
VALID_PROFILES = ("standard", "minimal")
VALID_COMPONENTS = ("platform", "mcp")
VALID_WAREHOUSES = ("postgres", "snowflake")


def register(app: typer.Typer) -> None:
    @app.command("guide", help="Print the entry skill, or the cloud, domain or github setup guide.")
    def guide(topic: str = typer.Argument("guide"), json_output: bool = JsonFlag) -> None:
        def run(ctx: CliContext) -> Result:
            if topic not in GUIDES:
                raise Failure(
                    ExitCode.USAGE, f"unknown guide {topic!r}; one of: {', '.join(GUIDES)}"
                )
            text = load_guide(topic)
            return Result(data={"topic": topic, "text": text}, message=text)

        run_command(json_output, run)

    @app.command("estimate", help="Approximate monthly hosting cost per cloud and tier.")
    def estimate(
        cloud: str | None = typer.Option(None, "--cloud"),
        size: str = typer.Option("small", "--size"),
        components: str = typer.Option("platform,mcp", "--components"),
        profile: str = typer.Option("standard", "--profile"),
        warehouse: str = typer.Option("postgres", "--warehouse"),
        json_output: bool = JsonFlag,
    ) -> None:
        def run(ctx: CliContext) -> Result:
            costs = load_costs()
            if size not in VALID_SIZES:
                raise Failure(ExitCode.USAGE, f"size must be one of: {', '.join(VALID_SIZES)}")
            if profile not in VALID_PROFILES:
                raise Failure(
                    ExitCode.USAGE, f"profile must be one of: {', '.join(VALID_PROFILES)}"
                )
            comps = list(dict.fromkeys(c.strip() for c in components.split(",") if c.strip()))
            if not comps or any(c not in VALID_COMPONENTS for c in comps):
                raise Failure(
                    ExitCode.USAGE, f"components must be from: {', '.join(VALID_COMPONENTS)}"
                )
            if warehouse not in VALID_WAREHOUSES:
                raise Failure(
                    ExitCode.USAGE, f"warehouse must be one of: {', '.join(VALID_WAREHOUSES)}"
                )
            if cloud is not None and cloud not in costs.clouds:
                raise Failure(ExitCode.USAGE, f"cloud must be one of: {', '.join(costs.clouds)}")
            other_profile = "minimal" if profile == "standard" else "standard"
            out = {}
            for c in [cloud] if cloud is not None else costs.clouds:
                chosen = costs.estimate(c, size, comps, profile, warehouse)
                other = costs.estimate(c, size, comps, other_profile, warehouse)
                out[c] = {
                    "monthly_usd": chosen.monthly_usd,
                    "lines": [{"item": line.item, "usd": line.usd} for line in chosen.lines],
                    f"{other_profile}_monthly_usd": other.monthly_usd,
                }
            gathered_on = costs.gathered_on.isoformat()
            lines = [f"{c}: ~${v['monthly_usd']:.0f}/month ({profile})" for c, v in out.items()]
            return Result(
                data={
                    "size": size,
                    "profile": profile,
                    "components": comps,
                    "warehouse": warehouse,
                    "gathered_on": gathered_on,
                    "clouds": out,
                },
                message="\n".join([*lines, f"figures gathered on {gathered_on}"]),
            )

        run_command(json_output, run)
