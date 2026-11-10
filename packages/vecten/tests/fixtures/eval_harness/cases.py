"""One harness case per skill evaluation case: what it serves and what the human types.

The secret values are the evaluation's own, prescribed so the stubs accept them: the human
types each at the hidden prompt `vecten secret set` opens, and the agent never sees one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eval_harness.demo_api import READ_TOKEN
from eval_harness.warehouse import HOST as WAREHOUSE_HOST

SIGN_IN_PASSWORD = "eval-sign-in-6d0e2b44"
# The passwords the human sets through the product's own links in the guide cases.
ADA_PASSWORD = "eval-ada-sign-in-0b7e"
GRACE_PASSWORD = "eval-grace-sign-in-4c1d"
LOCAL, PROD = "local", "prod"


@dataclass(frozen=True)
class Secret:
    name: str
    value: str
    where: str
    when: str


@dataclass(frozen=True)
class Case:
    skill: str
    eval_id: int
    demo_api: bool = False
    github: bool = False
    mock_cloud: bool = False
    mcp: bool = False
    warehouse: bool = False
    issues: str | None = None
    # The run wrapper copies the demo API's documentation into the project once it exists.
    demo_doc: bool = False
    # The MCP signs everyone in with SIGN_IN_PASSWORD, for cases that do not test sign-in.
    harness_passwords: bool = True
    domain: str = "data.acme.test"
    secrets: tuple[Secret, ...] = ()
    notes: tuple[str, ...] = field(default=())

    def hostnames(self) -> list[str]:
        names = ["api.demo.test"] if self.demo_api else []
        names += [f"mcp.{self.domain}"] if self.mcp else []
        names += [WAREHOUSE_HOST] if self.warehouse else []
        return names

    @property
    def serves(self) -> bool:
        return bool(self.hostnames())


DEMO_KEY = Secret("DEMO_API_KEY", READ_TOKEN, LOCAL, "before the run, in the run's folder")
DEPLOY = {"github": True, "mock_cloud": True}
GUIDE_SIGN_IN = (
    "When the agent gives Ada's set-password page and setup token, open the page, enter the "
    f"token and set Ada's password to {ADA_PASSWORD}.",
    "When the agent gives the `vecten login` page and code, sign in there as ada@acme.test "
    "with that password and approve.",
    "When the agent gives Grace's set-password page and setup token, set her password to "
    f"{GRACE_PASSWORD}; she signs in with it when her client connects.",
)
OPERATE_NOTES = (
    "`vecten issues --env prod` and `vecten issues show` answer from the case's fixture; an "
    "issue leaves the list once a merged pull request names its id or gap id.",
    "The project holds its local stack's secrets, so the agent can start the stack with "
    "`vecten up`. Every project's stack publishes the same ports: keep one operate run at a "
    "time, and run `vecten down` in its project when the run ends.",
    "Build the baseline in a base of its own: `env <case> --without-skill operate --dir "
    "<another base>` builds a project that never vendored the skill.",
    "Merge a pull request as the human with `gh pr merge <number> --merge`, after "
    "`EVAL_HARNESS_GH_USER=reviewer gh pr review <number> --approve`, in a shell that sourced "
    "env.sh.",
)
SEMANTIC_NOTES = (
    "Pull requests go to the gh stand-in; review and merge them with "
    "`EVAL_HARNESS_GH_USER=reviewer gh pr review <n> --approve` and `gh pr merge <n> --merge`.",
)

CASES: dict[str, Case] = {
    "connector": Case("connector", 1, demo_api=True, secrets=(DEMO_KEY,)),
    "model": Case(
        "model",
        1,
        demo_api=True,
        notes=("The demo API answers in case the agent records the connector again.",),
    ),
    "guide-1": Case(
        "guide",
        1,
        demo_api=True,
        mcp=True,
        harness_passwords=False,
        demo_doc=True,
        secrets=(DEMO_KEY,),
        notes=GUIDE_SIGN_IN,
        **DEPLOY,
    ),
    "guide-2": Case(
        "guide",
        2,
        mcp=True,
        warehouse=True,
        secrets=(
            Secret(
                "WAREHOUSE_ADMIN_PASSWORD",
                "eval-warehouse-admin-5f2c81",
                LOCAL,
                "before the run, in the run's folder",
            ),
            Secret(
                "MCP_PROVISIONER_PASSWORD",
                "eval-provisioner-9a41d3",
                LOCAL,
                "when the agent hands `vecten secret set ...` over, before the provisioner script",
            ),
            Secret(
                "MCP_IMPERSONATOR_PASSWORD",
                "eval-impersonator-2e77b0",
                LOCAL,
                "when the agent hands `vecten secret set ...` over, before the provisioner script",
            ),
        ),
        notes=(
            "When the agent hands the provisioner script over, run the provision-warehouse "
            'command printed above, then reply "our administrator ran it".',
            *GUIDE_SIGN_IN,
        ),
        harness_passwords=False,
        **DEPLOY,
    ),
    "guide-3": Case("guide", 3, domain="sandbox.acme.test", **DEPLOY),
    "operate-1": Case("operate", 1, issues="three-failures", notes=OPERATE_NOTES, **DEPLOY),
    "operate-2": Case(
        "operate",
        2,
        issues="revoked-key",
        secrets=(
            Secret(
                "HUBSPOT_TOKEN",
                "eval-hubspot-token-3b9d",
                PROD,
                "when the agent runs `vecten secret set HUBSPOT_TOKEN --env prod`",
            ),
        ),
        notes=OPERATE_NOTES,
        **DEPLOY,
    ),
    "operate-3": Case("operate", 3, issues="injected-instruction", notes=OPERATE_NOTES, **DEPLOY),
    "semantic-1": Case("semantic", 1, mcp=True, notes=SEMANTIC_NOTES, **DEPLOY),
    "semantic-2": Case("semantic", 2, mcp=True, notes=SEMANTIC_NOTES, **DEPLOY),
    "semantic-3": Case(
        "semantic",
        3,
        mcp=True,
        warehouse=True,
        secrets=(
            Secret(
                "WAREHOUSE_ADMIN_PASSWORD",
                "eval-warehouse-admin-5f2c81",
                LOCAL,
                "before the run, in the project folder",
            ),
        ),
        notes=SEMANTIC_NOTES,
        **DEPLOY,
    ),
}
# What the warehouse administrator chose when they ran the provisioner script for semantic-3's
# project; the harness runs it and stores both, as the human did before the case begins.
SEMANTIC_PROVISIONER = {
    "MCP_PROVISIONER_PASSWORD": "eval-provisioner-7c20e1",
    "MCP_IMPERSONATOR_PASSWORD": "eval-impersonator-41b9aa",
}
