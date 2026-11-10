# mcp/feedback-loop Specification

## Purpose
The loop from what people ask back into the semantic model: signals and reports fold into gaps, an admin reviews them, the `semantic` skill turns a gap into a pull request, and merge resolves it.

## Requirements

### Requirement: Implicit signals from the audit
The server SHALL derive implicit signals from the audit: refusals, the `sql-exec` that follows a refusal in the same chain, and warehouse errors. Zero-row results and repeated `describe` calls MUST NOT produce signals, because they are usually correct behaviour and would bury real gaps.

#### Scenario: Refusal followed by agent SQL
- **WHEN** a `join_required` refusal is followed in the same chain by a `sql-exec` on the two datasets
- **THEN** a signal is produced carrying the reason, both datasets and the agent's SQL

#### Scenario: Zero rows
- **WHEN** a compiled query returns no rows
- **THEN** no signal is produced

### Requirement: Explicit reports from report_feedback
Each `report_feedback` call SHALL become an explicit signal with its category and the fields supplied.

#### Scenario: User says the answer is wrong
- **WHEN** the agent calls `report_feedback` with category wrong value raised by user
- **THEN** the report is stored with the question, expected, returned and the tool calls involved

### Requirement: Signals fold into gaps
A job SHALL fold signals and reports into a gaps table holding the question, reason code or category, datasets and columns involved, the agent's SQL when there was one, occurrence count, first seen, last seen, status and the resolving pull request.

#### Scenario: First occurrence
- **WHEN** a new `unknown_metric` signal arrives for a question about churn
- **THEN** a gap is created with occurrence count 1, status `open` and first seen equal to last seen

### Requirement: Reports attach to a matching gap
A new signal or report SHALL attach to an existing gap when the datasets and the category or reason match, incrementing its occurrence count and updating last seen. A signal that names no dataset, such as `unknown_metric`, SHALL match on the requested metric or field name instead, normalised to lower case, so unrelated missing definitions never fold into one gap.

#### Scenario: Recurring join requirement
- **WHEN** a second `join_required` signal arrives for `orders` and `customers`
- **THEN** the existing gap's occurrence count becomes 2 and last seen is updated

#### Scenario: Two different unknown metrics
- **WHEN** an `unknown_metric` signal for `churn_rate` arrives while an open gap exists for `unknown_metric` on `net_revenue`
- **THEN** a second gap is created for `churn_rate` and the `net_revenue` gap is unchanged

### Requirement: Gap status lifecycle
A gap's status SHALL be one of `open`, `triaged`, `dismissed` and `resolved`, and an admin SHALL be able to set it from the panel or the admin tools.

#### Scenario: Admin dismisses a gap
- **WHEN** an admin sets a gap to `dismissed`
- **THEN** it leaves the open list and keeps its evidence

### Requirement: The semantic skill opens one pull request per gap
The shipped `semantic` skill, run in a checkout of the project repository, SHALL read a gap through the admin tools, draft the change under `osi/`, run `vecten mcp validate --evals`, and open a pull request with the gap id in the branch name and body, the triggering questions quoted as evidence, and the agent's SQL as the starting point for a metric expression. The merged pull request's author and reviewer are the record of who verified the definition.

#### Scenario: Missing metric gap
- **WHEN** an admin runs the `semantic` skill on an `unknown_metric` gap in a checkout of the project repository
- **THEN** a pull request is opened adding one metric to `osi/metrics.json`, its branch and body naming the gap id, with the questions quoted and the agent SQL in the expression

#### Scenario: Fix needs a mart column
- **WHEN** the gap's fix requires a column the mart does not have
- **THEN** the `semantic` skill hands the mart change to the `model` skill, `dbt-build-local` regenerates `osi/datasets.json`, and the metric edit follows
- **AND** both changes land in the same pull request

#### Scenario: Draft fails validation
- **WHEN** the drafted change does not pass `vecten mcp validate`
- **THEN** no pull request is opened and the validation errors are shown

### Requirement: Every resolved gap leaves an eval case and an example
The pull request for a gap SHALL add one case to `evals/semantic.yaml` holding the triggering question and the request the fix makes compilable, and SHALL append the question to the metric's `ai_context.examples`.

#### Scenario: Case added with the fix
- **WHEN** the `semantic` skill drafts the fix for a `wrong_definition` gap on `total_revenue`
- **THEN** the pull request also adds a case expecting `compiled` for the request behind the gap's question, and `total_revenue` gains the question as an example

#### Scenario: Fix stays fixed
- **WHEN** the pull request for a gap merges and a later change alters the metric so the case no longer compiles the same
- **THEN** CI fails on `vecten mcp validate --evals` naming the case and the gap id it came from

### Requirement: Gap resolves when the fix is live
When the server swaps to a model from a bundle whose commit contains the merged pull request that references a gap, it SHALL mark the gap `resolved` and record the pull request and the commit. Between merge and swap the gap SHALL show as `merged, waiting for build`.

#### Scenario: Merge, build, swap
- **WHEN** the pull request for a gap merges, the ship workflow runs, the dbt DAG builds that commit and the server swaps
- **THEN** the gap's status is `resolved` with the pull request and commit recorded

#### Scenario: Merged but not yet built
- **WHEN** the pull request has merged and the dbt DAG has not built that commit
- **THEN** the gap reads `merged, waiting for build` and the old definition still serves

### Requirement: An unreviewed merged fix says why its gap stays open
A merged pull request that names a gap in its branch and body but has no approving review by someone other than its author, on the merged head and submitted before the merge, SHALL NOT resolve the gap. The ship SHALL print one line naming the gap and the pull request, and the server SHALL show the gap as `merged without an approving review; the gap stays open` with that pull request.

#### Scenario: Author merges their own fix
- **WHEN** the pull request for a gap is merged by its author with no review, and the ship runs
- **THEN** the ship prints a line naming the gap and the pull request, the gap stays `open`, and the panel's gap page shows `merged without an approving review; the gap stays open` with the pull request

### Requirement: An admin can confirm an unreviewed merged fix
An admin SHALL be able to confirm an unreviewed merged fix from the panel or the admin API, as an explicit substitute for the review a one-person team cannot give. The audit log and the verification SHALL record the admin as `confirmed_by`, with no reviewers, and the gap then resolves as a reviewed fix does. Confirmation MUST NOT be an admin tool, and it applies only to the merge the admin saw.

#### Scenario: One-person team resolves a gap
- **WHEN** an admin confirms the unreviewed fix on a gap's page and the server then serves a model built from that merge
- **THEN** the gap is `resolved`, its verification names the admin under `confirmed_by`, and the audit log holds the confirmation

#### Scenario: A later unreviewed merge needs its own confirmation
- **WHEN** an admin confirmed one merge for a gap and a later unreviewed pull request for the same gap merges
- **THEN** the later merge does not resolve the gap until it is confirmed too

### Requirement: Reporter is told on the next session
A person who reported a resolved gap SHALL be told in their next session that the definition changed.

#### Scenario: Reporter returns
- **WHEN** a person who raised a report on a now-resolved gap starts a new session
- **THEN** the session surfaces a notice naming the changed definition

### Requirement: Evidence is untrusted input
Everything a gap carries that came from a person or from warehouse data, questions, reports, agent SQL and quoted values, SHALL be returned by the admin tools, the admin API and `issues` as typed, length-capped fields marked `untrusted: true`, and the question copied into an eval case or `ai_context.examples` SHALL be capped the same way. `report_feedback` SHALL be rate-limited per principal.

#### Scenario: Evidence read by an admin's agent
- **WHEN** an admin's agent reads a gap through the read-gap tool
- **THEN** every question, report and SQL field is capped in length and the evidence object carries `untrusted: true`

### Requirement: The semantic skill treats evidence as data
The `semantic` skill SHALL treat evidence fields as data, edit only `osi/`, the dbt project and `evals/`, never run evidence SQL except through `sql-exec` or `validate`, and quote the triggering question verbatim in the pull request, where review is the gate.

#### Scenario: Instruction planted in a question
- **WHEN** a report's question contains text addressed to the agent
- **THEN** the skill quotes it as data in the pull request and takes no action from it

#### Scenario: Report flood
- **WHEN** one principal sends reports faster than the limit
- **THEN** further reports are refused for the window and the refusals are counted in the audit

### Requirement: No model calls inside the product
The loop SHALL NOT call a language model from inside the server. Proposals MUST come from the `semantic` skill the admin runs.

#### Scenario: Gap job runs
- **WHEN** the folding job runs on a schedule
- **THEN** it makes no request to any model provider
