# mcp/metric-compiler Specification

## Purpose
The `query_metrics` tool: a single-dataset compiler from metrics, dimensions, time, filters and ordering into parameterized SQL, with structured refusal and agent-written SQL as the fallback.

## Requirements

### Requirement: query_metrics request shape
`query_metrics` SHALL accept `metrics`, optional `dimensions`, optional `time` with `dimension`, `grain`, `start` and `end`, optional `filters`, optional `order_by` with `field` and `dir`, and optional `limit`. The row cap MUST apply even when `limit` is absent or larger.

#### Scenario: Full request
- **WHEN** a person asks for `total_revenue` by `country` and month of `order_date` for 2026, filtered to `status in [paid, shipped]`, ordered by `total_revenue` desc, limit 500
- **THEN** the tool returns at most 500 rows grouped by country and month

#### Scenario: Limit above the row cap
- **WHEN** `limit` exceeds the deployment's row cap
- **THEN** the result is capped at the row cap

### Requirement: All metrics share one home dataset
The compiler SHALL require every requested metric to resolve to the same home dataset, and every dimension, the time dimension and every filter field to belong to it.

#### Scenario: Two metrics on one dataset
- **WHEN** `total_revenue` and `order_count` both have home dataset `orders`
- **THEN** both are computed in one query over `orders`

#### Scenario: Ratio inside one dataset
- **WHEN** a metric's expression is `SUM(discount) / SUM(total_amount)` over columns of `orders`
- **THEN** it compiles like any other metric

### Requirement: Time dimension defaulting
When `time.dimension` is omitted the compiler SHALL use the dataset's time dimension if it has exactly one, and MUST refuse with `unknown_field` otherwise. Grain SHALL be one of day, week, month, quarter or year.

#### Scenario: Single time dimension
- **WHEN** `orders` has exactly one time dimension and the request omits `time.dimension` but sets grain month
- **THEN** the query truncates that dimension to month

#### Scenario: Unsupported grain
- **WHEN** the request sets grain `hour`
- **THEN** the tool returns an error listing the five allowed grains

### Requirement: Fixed filter operator set with bound parameters
Filter operators SHALL be limited to equals, not equals, comparisons, `in`, `between`, `like` and `is null`. Filter and time values MUST be bound as query parameters and never interpolated into the SQL text.

#### Scenario: In filter
- **WHEN** a filter is `status in [paid, shipped]`
- **THEN** the compiled SQL contains a placeholder per value and the values travel as parameters

#### Scenario: Operator outside the set
- **WHEN** a filter uses an operator such as `regex`
- **THEN** the tool returns an error listing the allowed operators

### Requirement: Compiled SQL uses dialect expressions and derived tables
The compiler SHALL replace field names with their dialect expressions, rewrite column identifiers inside metric expressions that match a field whose expression differs from its name, and wrap a dataset whose `source` is a subquery as a derived table.

#### Scenario: Subquery source
- **WHEN** the home dataset's `source` is a `SELECT` subquery
- **THEN** the compiled SQL selects from that subquery as a derived table

#### Scenario: Field with an expression
- **WHEN** dimension `country` has expression `UPPER(country_code)`
- **THEN** the select list and group by use `UPPER(country_code)` aliased as `country`

### Requirement: Response carries rows and provenance
The response SHALL carry the rows, the compiled SQL, and the definitions used: each metric's expression, each field's expression, the dataset source, and for each metric who verified it and when, or null. The call MUST run through the same adapter and identity as `sql-exec`.

#### Scenario: Provenance returned
- **WHEN** a request compiles and runs
- **THEN** the response includes the SQL and the metric, field and source definitions it used, with each metric's verification record
- **AND** the audit row records resolution `compiled` with the metric names, dimensions and filters

### Requirement: Every response carries a receipt
Every `query_metrics` response, including a refusal, SHALL carry a `receipt` with the audit row id, the commit of the loaded model, the SHA-256 of the SQL sent with parameters bound, and the issue time. The SQL sent to the warehouse SHALL begin with a comment holding the receipt id, and on Snowflake the session SHALL set `QUERY_TAG` to it.

#### Scenario: Receipt on a compiled answer
- **WHEN** a request compiles and runs
- **THEN** the response's `receipt.id` equals the audit row id, `receipt.model` is the loaded commit and `receipt.sql_sha256` matches the SQL in the response
- **AND** the warehouse's query history shows the SQL beginning with `/* vecten <receipt id> */`

#### Scenario: Receipt on a refusal
- **WHEN** a request is refused
- **THEN** the response carries a receipt with no `sql_sha256`
- **AND** the audit row id matches

### Requirement: Response states which row operations are safe
The response SHALL carry each metric's additivity class and a `rows_may` list derived from it: `filter`, `sort` and `sum_across_dimensions` for `additive`; `filter` and `sort` for `non_additive`; `filter`, `sort` and `sum_across_non_time` for `semi_additive`. It SHALL also state that further aggregation is done by calling `query_metrics` at a coarser grain.

#### Scenario: Ratio metric returned by month
- **WHEN** `product_share`, a division of two sums, is returned by month
- **THEN** the response marks it `non_additive` with `rows_may` of `filter` and `sort` and names the coarser-grain call as the way to a yearly figure

#### Scenario: Revenue returned by country and month
- **WHEN** `total_revenue`, a sum, is returned by country and month
- **THEN** the response marks it `additive` with `sum_across_dimensions` allowed

### Requirement: Structured refusal
When a request cannot be compiled the tool SHALL return a refusal with a reason code from `join_required`, `unknown_metric`, `unknown_field`, `ambiguous_dataset`, `dialect_missing`, `rollup_not_allowed`, a context object, and a SQL skeleton built from the dataset sources and the relationship join condition.

#### Scenario: Join required
- **WHEN** the metric lives on `orders` and a dimension lives on `customers`
- **THEN** the refusal has reason `join_required`, context naming both datasets and the relationship with its columns, and a skeleton joining the two sources on those columns

#### Scenario: Unknown metric
- **WHEN** the request names a metric that does not exist
- **THEN** the refusal has reason `unknown_metric` and context listing the nearest names from fields, metrics and synonyms

### Requirement: Refusal is audited and chained to the fallback
A refusal SHALL write an audit row with resolution `refused`. A `sql-exec` call that follows it in the same session SHALL be linked to it through the request chain id and recorded with resolution `agent_sql`.

#### Scenario: Agent falls through to SQL
- **WHEN** the agent receives a `join_required` refusal and then runs the finished skeleton through `sql-exec`
- **THEN** the two audit rows share a request chain id
- **AND** the second row's resolution is `agent_sql`

### Requirement: Semi-additive metrics are not rolled up over their guarded dimension
When a requested metric is `semi_additive` over a dimension and the request's grain on that dimension is coarser than the dataset's, the compiler SHALL refuse with reason `rollup_not_allowed` unless the metric's extension names a rollup rule of `last` or `first`, in which case it SHALL compile the rule.

#### Scenario: Balance by quarter without a rule
- **WHEN** `account_balance`, semi-additive over time on a dataset at month grain, is requested by quarter and names no rule
- **THEN** the tool refuses with `rollup_not_allowed` and context naming the dimension and the rules it accepts

#### Scenario: Balance by quarter with last
- **WHEN** the same metric names rule `last`
- **THEN** the compiled SQL takes the last month's value in each quarter

### Requirement: Out-of-scope metric shapes are refused, not approximated
Joins across relationships, metrics that reference other metrics across datasets, and cumulative or window metrics SHALL NOT be compiled. The tool MUST refuse rather than return a partial or approximate result.

#### Scenario: Cross-dataset metric reference
- **WHEN** a metric's expression references a metric whose home dataset differs
- **THEN** the tool refuses with reason `join_required`
