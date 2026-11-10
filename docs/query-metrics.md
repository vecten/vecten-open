# Querying metrics with `query_metrics`

`query_metrics` is the [MCP](GLOSSARY.md) tool that answers a metric question from the
semantic model instead of from SQL the agent writes. It compiles a request into one SQL
statement over a single [dataset](GLOSSARY.md), runs it as the caller, and returns the rows
together with the definitions it used. When the model cannot answer, it refuses with a reason
and a SQL starting point the agent can finish and run with `sql-exec`.

This page is the reference for the request, the compiled SQL, the response, refusals and the
limits. For the other tools see [built-in MCP tools](mcp-tools.md); for writing the metrics
and datasets it reads see [the semantic model guide](semantic-model.md).

## Request

```json
{
  "metrics": ["total_revenue"],
  "dimensions": ["country"],
  "time": {"dimension": "order_date", "grain": "month", "start": "2026-01-01", "end": "2027-01-01"},
  "filters": [{"field": "status", "op": "in", "value": ["paid", "shipped"]}],
  "order_by": [{"field": "total_revenue", "dir": "desc"}],
  "limit": 500,
  "question": "What was monthly paid or shipped revenue by country in 2026?"
}
```

| Field | Required | Shape |
| --- | --- | --- |
| `metrics` | yes | Non-empty list of distinct metric names. |
| `dimensions` | no | List of field names to group by. |
| `time` | no | Object with only `dimension`, `grain`, `start` and `end`. |
| `filters` | no | List of objects with only `field`, `op` and `value`. |
| `order_by` | no | List of objects with only `field` and `dir` (`asc`, the default, or `desc`). |
| `limit` | no | Positive integer; never more than the row cap. |
| `question` | no | The person's question in their words, kept in the audit. |

An object with a key outside its list is rejected with an error naming the accepted keys, so a
misspelt `time.from` never silently drops a date range.

### Rules

- **One home dataset.** Every metric must have the same [home dataset](GLOSSARY.md). Every
  dimension, the time dimension and every filter field must belong to it. A field may be
  written `dataset.field`; an unqualified name is looked up on the home dataset.
- **Dimensions** must be fields classified as `dimension` or `time_dimension`. A fact cannot be
  grouped by.
- **Time.** `grain` is one of `day`, `week`, `month`, `quarter` or `year`; anything else is an
  error listing the five. With a grain, the time dimension is truncated to the start of each
  period and grouped by, under the time dimension's own name. Without a grain, `start` and
  `end` only filter, which answers questions such as "revenue in 2026" as one total. At least
  one of `grain`, `start` or `end` is required.
- **Time dimension default.** When `time.dimension` is omitted the dataset's only time
  dimension is used. A dataset with none or several refuses with `unknown_field` and lists the
  candidates.
- **Range bounds.** `start` is inclusive and `end` exclusive. Both are ISO dates or timestamps
  such as `2026-01-01` or `2026-01-01T09:00:00`; anything else is rejected before a warehouse
  connection opens.
- **Ordering** names an output column: a requested metric, dimension, or the time dimension
  when a grain is set.
- **Row cap.** The deployment's `row_cap` setting (default 1,000) bounds every result. A
  `limit` above it, or no `limit`, returns at most the cap.

### Filter operators

| `op` | `value` | SQL |
| --- | --- | --- |
| `=` or `equals` | scalar | `field = ?` |
| `!=` or `not equals` | scalar | `field <> ?` |
| `>`, `>=`, `<`, `<=` | scalar | `field > ?` and so on |
| `like` | string pattern, `%` and `_` as wildcards | `field LIKE ?` |
| `in` | list of scalars | `field IN (?, ?, …)`; an empty list matches no rows |
| `between` | list of exactly two scalars | `field BETWEEN ? AND ?` (inclusive) |
| `is null` | omitted, `true` or `null` for `IS NULL`; `false` for `IS NOT NULL` | `field IS NULL` |

Any other operator is an error listing these. Filters apply to dataset rows before
aggregation; filtering on a computed metric value is not supported.

Every value travels as a bound parameter and never appears in the SQL text. The placeholder
is `%s` on Postgres and `?` on Snowflake.

## Compiled SQL

The request above compiles on Postgres to:

```sql
SELECT UPPER(country_code) AS "country",
       DATE_TRUNC('month', order_date) AS "order_date",
       SUM(amount) AS "total_revenue"
FROM marts.orders
WHERE (order_date >= %s) AND (order_date < %s) AND (status IN (%s, %s))
GROUP BY UPPER(country_code), DATE_TRUNC('month', order_date)
ORDER BY "total_revenue" DESC
LIMIT 500
```

- A field name is replaced by the field's expression for the warehouse's dialect: the
  `SNOWFLAKE` expression on Snowflake when the field has one, otherwise the `ANSI_SQL` one.
- A column named inside a metric expression is rewritten to that field's expression, so a
  metric `SUM(orders.net_amount)` over a field `net_amount = amount - discount` compiles to
  `SUM(amount - discount)`.
- A division in an `ANSI_SQL` expression is true division on both warehouses. On Postgres the
  compiler casts the numerator, so `COUNT(a) / COUNT(b)` does not truncate to an integer.
- A dataset whose `source` is a `SELECT` is wrapped as a derived table aliased with the
  dataset's name.

**Week start differs between warehouses.** On Postgres, `DATE_TRUNC('week', …)` always starts
weeks on Monday. On Snowflake it follows the `WEEK_START` parameter of the account, user or
session: at the default `0` weeks also start on Monday, but an account that sets
`WEEK_START = 7` groups weeks from Sunday. Vecten does not override the parameter, so a weekly
answer on Snowflake matches the account's other reports.

## Response

A compiled answer has `resolution: "compiled"` and:

| Field | Content |
| --- | --- |
| `rows`, `columns` | The result, one array per row, in the column order of `columns`. |
| `sql` | The SQL that ran, with values bound, for display and for the receipt hash. |
| `sql_template`, `parameters` | The same statement with placeholders, and its values. |
| `definitions` | The dataset `source`, each field's expression, and each metric's expression with `verified`: who verified the definition and when, or `null`. |
| `operations` | Per metric, its [additivity](GLOSSARY.md) and `rows_may`, below. |
| `further_aggregation` | A reminder to call `query_metrics` again at a coarser grain or with fewer dimensions rather than combine rows. |
| `receipt` | The [receipt](GLOSSARY.md): `id`, `model` (the loaded model's commit), `sql_sha256` and `issued_at`. |

`rows_may` says what an agent or dashboard may safely do with the returned rows:

| Additivity | `rows_may` |
| --- | --- |
| `additive` | `filter`, `sort`, `sum_across_dimensions` |
| `non_additive` | `filter`, `sort` |
| `semi_additive` | `filter`, `sort`, `sum_across_non_time` |

A ratio such as `SUM(discount) / SUM(amount)` returned by month is `non_additive`: twelve
monthly values do not sum or average to the yearly one. The yearly figure is another call with
grain `year`.

The receipt's `id` is also the audit row's id and the start of the SQL sent:
`/* vecten <receipt id> */`. On Snowflake the session's `QUERY_TAG` is set to
`vecten <receipt id>` as well, so warehouse query history leads back to the audit row. The
audit row records resolution `compiled`, the metric names, dimensions and filters, the SQL
hash, the tables and columns read and the row count. See [MCP audit records](mcp-audit.md).

## Semi-additive metrics

A `semi_additive` metric, such as an account balance, may be summed across accounts or
regions but not across time. Its dataset declares its grain (for example `month`) with the
Vecten extension, and the metric names the dimension it must not be summed over; see
[additivity in the semantic model guide](semantic-model.md#additivity).

- At the dataset's grain or finer, the metric compiles like any other.
- At a coarser grain, the metric must name a rollup rule. With `last` the answer for each
  period is the value of its last base period (the March balance for a quarter); with
  `first`, the first. Without a rule the request is refused with `rollup_not_allowed`, naming
  the dimension and the accepted rules.
- With no time grain at all, a semi-additive metric is refused, since summing every period's
  balance would be wrong.

## Refusals

A request the compiler cannot answer exactly returns `resolution: "refused"` with `reason`,
`context`, `skeleton` and a receipt whose `sql_sha256` is `null`. The tool never returns a
partial or approximate answer.

| `reason` | When | `context` | Next step |
| --- | --- | --- | --- |
| `join_required` | A metric, dimension or filter lives on another dataset, metrics span datasets, or a metric references a metric on another dataset. | `datasets`, and the `relationship` with its columns when the model has one. | Finish the `skeleton`, which joins both sources on the relationship, and run it with `sql-exec`. |
| `unknown_metric` | No metric has that name. | `metric`, `suggestions`: up to five visible metric and field names nearest to it, matched on names and on their synonyms, so `turnover` can suggest `total_revenue`. | Retry with a suggestion, or `search`. |
| `unknown_field` | A field does not exist on the home dataset, a fact is used as a dimension, `order_by` names a column that is not in the output, or the time dimension cannot be defaulted. | `field`, and `candidates` for the time dimension. | Correct the request; `describe` lists the fields. |
| `ambiguous_dataset` | An unqualified field is not on the home dataset but exists on several others. | `field`, `home`, `candidates`. | Qualify the field to pick one; that leads to `join_required`. |
| `dialect_missing` | A definition has no expression for this warehouse's dialect. Model validation normally rejects such a model before it loads. | `definition`, `dialect`. | Report it; the model needs an `ANSI_SQL` or warehouse expression. |
| `rollup_not_allowed` | A semi-additive metric at a coarser grain without a rule, or with no grain; a metric using a window function. | The metric and the dimension with the accepted `rules`, or the `construct` (`window`) the compiler does not run. | Ask at the dataset's grain, have the metric name a rule, or write the SQL with `sql-exec`. |

`skeleton` is a `SELECT` over the home dataset's source, or for `join_required` over both
sources joined on the relationship, with a comment where the calculation goes. It is `null`
for `unknown_metric`, which has no dataset to start from.

When the agent falls back to `sql-exec` in the same session, the audit links the two calls
with one request chain id and records the second as `agent_sql`. A refusal followed by agent
SQL is how the server learns which marts and metrics are missing. The link covers the next
`sql-exec` from the same person, client and session within five minutes of the refusal; a
`query_metrics` call in between ends it. See [MCP audit records](mcp-audit.md).

## Limits

- One dataset per query. Joins across relationships, metrics that reference metrics on other
  datasets, and window or cumulative metrics are refused, not approximated.
- Results stop at the row cap, and statements stop at `statement_timeout_seconds`
  (default 30). An admin changes both through the settings described in
  [built-in MCP tools](mcp-tools.md).
- Denied fields are hidden from the compiler: a request naming one, or a metric reading one,
  is refused with `unknown_field`.

Back to the [docs index](README.md).
