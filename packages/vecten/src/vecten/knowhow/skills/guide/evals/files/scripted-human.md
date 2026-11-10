# The scripted human for the guide evals

The person running the evaluation plays the human, through the prompt addition the
runbook describes. The human answers each `ask` item the agent puts to it with the
reply listed for that question's text, word for word as `vecten next --json` prints the
item's `title`; the tables shorten long titles with "..." and the reply matches the
full title. An `ask` item put in any other wording gets the reply "I do not understand
the question." and is recorded as a failed expectation. The person stores each secret
named below in their own terminal: the local ones with `vecten secret set <NAME>` before
the run, the `--env prod` ones when the agent hands the command over. The agent never
sees a value.

Questions that a dispatched skill asks in its own words get these replies: for the
`connector` skill, "companies and deals; the fixtures may keep every object"; for the
`model` skill, "a sales pipeline mart: every deal with its company"; for the `semantic`
skill, "pipeline value: the sum of open deal amounts", or for case 2 "order revenue:
the sum of order amounts". Any other question gets "go with what you think is right".

The cloud provider and the GitHub API are mocked: `vecten bootstrap`,
`vecten github init`, `vecten github push` and `vecten deploy` talk to the mocks, and
the mocked deploy records a deployed commit for `prod` and serves the MCP at
`https://mcp.data.acme.test`.

The cloud is the repository's mocked provider, in the environment of every `vecten`
process the agent starts:

    PYTHONPATH=<checkout>/packages/vecten/tests/fixtures/mock_cloud
    VECTEN_CONTROL_PLANE=vecten_mock_cloud:MockCloud
    VECTEN_MOCK_CLOUD_DIR=<an empty directory standing for the cloud account>

It holds the `--env prod` secrets, the bootstrap record, the environment's outputs and the
releases a ship places, under that directory. GitHub is the evaluation harness's `gh`
stand-in (`packages/vecten/tests/fixtures/eval_harness`) first on the `PATH`, which answers
the repository, workflow dispatch and run calls `vecten github`, `vecten bootstrap` and
`vecten deploy` make. The MCP at `https://mcp.data.acme.test` is the one the harness serves
from the shipped release's `osi/` over the warehouse the case uses, with that public URL, so
that Grace's client signs in to it with the password she set through her link.

## Case 1: no warehouse, one source, a confirmed deploy

| Question | Reply |
|---|---|
| Do you already have a data warehouse? | no |
| Do you have data sources to connect, ... or do you want to start empty? | demo, our CRM at https://api.demo.test/v1; the docs are in docs/demo-api.md |
| Should the warehouse be Postgres, run for you, or a Snowflake account you already have? | postgres |
| What should the project be called? | acme |
| How many people and agents will use the warehouse? | 5 |
| Who may read the demo raw data? | sales |
| Do you have a cloud account? Which provider? | aws |
| Do you have a registered domain? | yes, data.acme.test, and its zone is data.acme.test |
| Do you have a GitHub account? Which repository? (first time) | not yet |
| Do you have a GitHub account? Which repository? (after the guide) | acme/data-platform |
| Deploy to prod? This creates resources your cloud provider bills you for. | yes |
| Which OIDC issuer and client id should sign people in? | we have no identity provider; use the MCP's own password sign-in |
| Who is the first admin? | ada@acme.test |
| Who should have access first? Give an email address and a name. | grace@acme.test, Grace Hopper |
| Should sync failures be posted to a webhook? ... | no |

Secrets: `DEMO_API_KEY` is the demo stub's token.

Sign-in: when the agent gives Ada's set-password page and setup token, the person running
the evaluation opens the page, enters the token, sets Ada's password to
`eval-ada-sign-in-0b7e` and replies "done". When the agent gives the page and code of
`vecten login`, they sign in there as ada@acme.test with that password, approve, and reply
"done". When the agent gives Grace's page and setup token, they set Grace's password to
`eval-grace-sign-in-4c1d` and reply "done".

After the empty `next`, as Grace: "I'm Grace. Connect my AI assistant to our data and
ask it: what is our pipeline value by country?" When the agent asks Grace to sign in, the
person running the evaluation completes the browser sign-in as grace@acme.test and replies
"done". Grace accepts the figures and the receipt id as the answer.

The demo API stub follows `skills/connector/evals/files/demo-api.md`; the evaluation
harness copies that document to `docs/demo-api.md` as soon as `vecten init` has created the
project.

## Case 2: an existing warehouse

| Question | Reply |
|---|---|
| Do you already have a data warehouse? | yes |
| What is the locator of the existing warehouse? | postgresql://mcp_admin@warehouse.acme.test:5432/analytics |
| What should the project be called? | acme |
| How many people and agents will use the warehouse? | 20 |
| Do you have a cloud account? Which provider? | aws |
| Do you have a registered domain? | yes, data.acme.test, and its zone is data.acme.test |
| Do you have a GitHub account? Which repository? | acme/data-platform |
| Deploy to prod? This creates resources your cloud provider bills you for. | yes |
| Which OIDC issuer and client id should sign people in? | we have no identity provider; use the MCP's own password sign-in |
| Who is the first admin? | ada@acme.test |
| Who should have access first? Give an email address and a name. | grace@acme.test, Grace Hopper |

Secrets: `WAREHOUSE_ADMIN_PASSWORD`, `MCP_PROVISIONER_PASSWORD` and
`MCP_IMPERSONATOR_PASSWORD`. Sign-in goes as in case 1: Ada's
password `eval-ada-sign-in-0b7e`, Grace's `eval-grace-sign-in-4c1d`. When the agent hands the
provisioner script over for the administrator to run, the person running the evaluation runs it
on the warehouse and then replies "our administrator ran it". That reply follows an action, so
it comes from the person once the script has run, never from this file. The warehouse is a
Postgres container the person running the evaluation starts at `warehouse.acme.test`,
with a `marts` schema holding an `orders` table.

After the empty `next`, as Grace: "I'm Grace. Connect my AI assistant to our data and
ask it: what is our order revenue?" When the agent asks Grace to sign in, the person
running the evaluation completes the browser sign-in as grace@acme.test and replies
"done". Grace accepts the figure and the receipt id as the answer.

## Case 3: start empty, then no at the deploy gate

| Question | Reply |
|---|---|
| Do you already have a data warehouse? | no |
| Do you have data sources to connect, ... or do you want to start empty? | start empty |
| Should the warehouse be Postgres, run for you, or a Snowflake account you already have? | postgres |
| What should the project be called? | sandbox |
| How many people and agents will use the warehouse? | 2 |
| Do you have a cloud account? Which provider? (first time) | I do not have one |
| Do you have a cloud account? Which provider? (after the guide) | aws |
| Do you have a registered domain? | yes, sandbox.acme.test, and its zone is sandbox.acme.test |
| Do you have a GitHub account? Which repository? | acme/sandbox |
| Deploy to prod? This creates resources your cloud provider bills you for. | no, not today |
