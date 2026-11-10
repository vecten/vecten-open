# Correcting a business definition

A sales lead asks for revenue and notices that the answer includes refunds. Vecten can record
that report, group similar reports into a **[gap](GLOSSARY.md)**, and carry a reviewed definition
fix through [shipping](GLOSSARY.md), the warehouse build and activation. The server records
evidence and tracks progress; the `semantic` skill runs in an engineer's checkout to propose the change.

Tool names below refer to the [built-in MCP tools](mcp-tools.md), called through the person's
authenticated agent session.

## From a wrong answer to a review

1. The person's agent calls `report_feedback` with category `wrong definition`, the original
   question, expected and returned behavior, and the relevant tool calls. The response returns
   a report id. Categories use spaces: `wrong value`, `missing metric`, `missing dataset`,
   `wrong definition` and `access denied`.
2. Every 30 seconds, a background job folds stored reports and query audit records into gaps.
   An administrator reads them with `list_gaps` and `read_gap`. For example, a report about
   `total_revenue` over `orders` records the question and its expected treatment of refunds.
3. In the project's checkout, the engineer runs the `semantic` skill on that gap. If
   `orders.refund_amount` already exists, the proposed expression can become
   `SUM(orders.total_amount) - SUM(orders.refund_amount)`. If it is absent from the
   [mart](GLOSSARY.md), the skill hands the transformation to the `model` skill, rebuilds the datasets,
   and includes the mart and metric change in the same PR. Incorrect upstream data belongs
   to the `connector` skill; a metric must not hide it.
4. The fix adds the capped original question to the metric's `ai_context.examples` and a
   gap-linked [eval case](GLOSSARY.md) in `evals/semantic.yaml`. The case pins the request and
   compiled SQL hash so changing the definition back fails validation with the gap id.
5. The engineer runs `vecten mcp validate --evals --json`, and also
   `vecten dbt-build-local --json` when dbt changed. The PR names the exact gap id in both its
   branch (for example `semantic/gap-<id>`) and body, quotes the question and agent SQL, and
   records validation results. A human reviews and merges it.

The skill is available as `skills/semantic/SKILL.md` in generated projects and in the Vecten
plugin. It also authors metrics without a gap and refines `vecten mcp introspect` drafts for
existing warehouses without dbt. In a platform project, dbt owns the generated datasets;
the skill changes the dbt source and regenerates them.

## Which activity creates a gap

Implicit signals are query refusals, warehouse errors, and the agent SQL following a refusal
in the same [request chain](mcp-audit.md), which links a refusal to its fallback SQL.
Successful queries returning zero rows, repeated `describe` calls, feedback rate-limit
refusals and non-query tool failures do not create gaps.

Gaps match on the dataset set and category or refusal reason. Dataset ordering does not matter.
Physical tables are mapped to datasets only when their names identify a source unambiguously.
An unqualified `orders` does not identify a dataset when both `raw.orders` and `marts.orders`
exist. Source mappings are cached for each loaded model; compiled metric queries already
carry their dataset names.
When no dataset is known, a requested metric or field name is compared case-insensitively:
missing `churn_rate` and missing `net_revenue` stay separate. If neither dataset nor name is
known, the job keeps the occurrence separate instead of guessing equivalence.

A repeated occurrence increments the count and updates the last-seen time. A refusal's
same-session fallback SQL enriches that occurrence instead of counting the question twice.
The worker records processed ids in the same transaction as the gap update, so retries and
concurrent workers do not double-count. Out-of-order fallback rows wait for their refusal
without blocking newer rows. Each run processes up to 500 audit rows and 500 reports, plus
up to 500 waiting fallbacks whose refusal has arrived; a backlog drains across runs.

Statuses are `open`, `triaged`, `dismissed` and `resolved`. Administrators can change these
through the existing gap status operation; dismissal retains evidence. Repeated reports keep
the existing status. Reconciliation does not undo an administrator's status change after a
fix has already been recorded as live.

## When the correction becomes live

A deployed project's merge starts its configured shipping workflow. Before shipping any host,
the CLI collects review metadata once for the whole environment. It resolves the configured
GitHub repository's canonical name, searches merged PRs for gap ids present in eval cases,
and reads those PRs and their reviews.
It accepts a PR only when the body and branch name both reference the gap, its merge commit
belongs to the git ancestry of the bundle (the project content `vecten ship` packs for the hosts), and another reviewer approved its final head
commit before merge. A later changes-requested or dismissed review supersedes that approval.
The metadata follows GitHub's [PR API](https://docs.github.com/en/rest/pulls/pulls) and
[review API](https://docs.github.com/en/rest/pulls/reviews).
The [search API](https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests)
can return incomplete results or cap a search at 1,000 matches; either condition fails bundle
preparation rather than silently omitting verification. Newly merged PRs must be visible
in GitHub search; if a just-merged fix has no metadata yet, retry shipping after indexing.

The bundle contains `.vecten/ship/feedback.json`, mounted read-only into the
[MCP](GLOSSARY.md) container.
The server needs no GitHub credential or GitHub API access. Generated deploy workflows use
full git history and `pull-requests: read`, with GitHub's temporary token provided to the CLI.
No GitHub lookup is needed when there are no gap-linked evals. Failed metadata retrieval
fails bundle preparation; an unreviewed or unrelated PR supplies no resolution metadata.

### A fix merged without an approving review

A PR that references a gap in its body and branch, with its merge commit in the bundle's
ancestry, but without a qualifying approval does not resolve the gap. The ship prints one line
naming the gap and the PR, and the ship's manifest (`.vecten/ship/feedback.json`, below) lists the merge under `unreviewed`. The server then
shows the gap's `delivery_status` as `merged without an approving review; the gap stays open`,
with the PR and its author, so a missed review is not mistaken for a broken pipeline.

GitHub does not let an author approve their own PR, so a team with a single GitHub user can
never supply that review. In its place, a human admin confirms the fix on the gap's page in the
[admin panel](admin-panel.md), or with `POST /api/admin/gaps/confirm`. The confirmation names
the admin and the merge commit they saw, and the access events record it. On the next
reconciliation the merge counts as a verified fix with no reviewers and `confirmed_by` set to
the admin, and the gap follows the path below. A later unreviewed merge for the same gap needs
its own confirmation. No admin tool confirms a fix, so an agent cannot approve its own work.

Once this metadata reaches the server, the gap's `delivery_status` is
`merged, waiting for build`. Its lifecycle status remains unchanged. Platform projects keep
answering from the previous model until the warehouse's [built commit](GLOSSARY.md) covers the
shipped commit and model validation succeeds. MCP-only projects need model validation but
have no dbt build gate. At activation, the gap becomes `resolved`, with the PR, activated
commit, author and reviewers (or the confirming admin) recorded. The same verification appears
in metric provenance.
A merge by itself never resolves a gap; a failed build or invalid model keeps the old answer.

The reporter's first tool response in their next **login session** includes readable text and
`_meta["vecten/definition_changes"]`, naming the changed definitions, gap, commit and PR.
The current session receives no notice. Notices are private to that person, survive server
restart, and are delivered once within the retention window described below. Service
principals do not receive personal notices. Normal tool responses without a pending notice
use a filtered database read without acquiring a write lock. Reconciliation writes model
verification and gap state only when they change.

A laptop without a shipped verification manifest still records and exposes gaps and supports
local model authoring. It does not infer merge verification from edited local files.

## Evidence and recovery

Evidence is returned as typed fields marked `untrusted: true`: questions and other text are
capped at 4096 characters, SQL at 16384, and tool calls at 20 entries. The same question cap
applies when the skill copies it into examples, evals and PR quotes. Redaction is preserved;
turning on the redaction setting also redacts existing evidence in gap reads. A request
identifier is evidence too: redaction omits request-derived names, datasets and
columns from reports and gap views, and removes refusal context from new audit rows. A planted
instruction in a question is quoted as data, never executed. The skill changes only `osi/`,
the dbt project and `evals/`, and runs evidence SQL only through `sql-exec` or validation.

The audit path remains asynchronous. Folding reads persisted audit rows; dropped audit rows
cannot produce implicit signals. Explicit reports are stored separately. Check the audit
queue's dropped count when gaps are unexpectedly absent. Background failures log their error
class without evidence or credential values and retry on later maintenance ticks. A malformed
resolution manifest does not stop the server from serving the last valid model.

Reports, fallback SQL waiting for a missing refusal, and reporter notices expire after the
configured `audit_retention_days` (90 days by default). Gap summaries remain available.
Deduplication markers remain while their source audit row or report exists and are removed
by daily cleanup after that source disappears. Successful queries and discovery calls do
not allocate markers. Gaps record which reporters have already been queued for a notice,
so deleting an expired notice cannot make it reappear.

For a waiting gap, inspect `model_health`, the gap's verification fields and the dbt build
status. For a missing PR link, verify the exact gap id in both branch and body, an eval naming
that gap, a final-head approval and complete git ancestry in the shipping checkout. Do not
manually mark a gap resolved to conceal a failed build.

Back to the [docs index](README.md).
