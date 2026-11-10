# Demo CRM API, version 1

A small CRM API with two objects, companies and deals.

## Base URL

    https://api.demo.test/v1

## Authentication

Every request carries a bearer token in the `Authorization` header:

    Authorization: Bearer <token>

Create a token under Settings, API tokens. A token with the `read` scope can read every object.
A missing or invalid token returns `401`; a token without the `read` scope returns `403`.

## Rate limit

100 requests per minute per token. Past the limit the API returns `429` with a `Retry-After`
header in seconds.

## Pagination

List endpoints return at most `limit` records per page (default 50, maximum 200) and a cursor
for the next page:

```json
{
  "data": [{"id": 1}],
  "next_cursor": "eyJpZCI6IDUwfQ"
}
```

Pass the cursor back as the `cursor` query parameter. `next_cursor` is `null` on the last page.

## Incremental reads

Every list endpoint accepts `updated_since`, an ISO 8601 UTC timestamp, and returns only the
records whose `updated_at` is at or after it.

## Endpoints

### `GET /companies`

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Unique per company. |
| `name` | string | Registered company name. |
| `domain` | string | Web address as entered, any case, may carry a protocol or a path. |
| `email` | string | Contact email; an empty string when none was entered. |
| `country` | string | ISO 3166-1 alpha-2 code. |
| `created_at` | string | ISO 8601 UTC timestamp. |
| `updated_at` | string | ISO 8601 UTC timestamp of the last change. |

### `GET /deals`

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Unique per deal. |
| `company_id` | integer | The company the deal is with. |
| `title` | string | Deal title. |
| `stage` | string | One of `open`, `won`, `lost`. |
| `amount` | number | Deal value in the account currency. |
| `created_at` | string | ISO 8601 UTC timestamp. |
| `close_date` | string or null | ISO 8601 date; null while the deal is open. |
| `is_won` | boolean | True when the deal closed as won. |
| `updated_at` | string | ISO 8601 UTC timestamp of the last change. |
