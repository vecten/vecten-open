-- Lowercase a domain or URL and reduce it to the apex host:
-- strip protocol, leading www., and anything from the first / or ? onward.
-- Character classes stand in for backslash escapes: Postgres keeps backslashes in
-- string literals and Snowflake consumes them, so only backslash-free patterns agree.
{% macro normalize_domain(column) %}
nullif(
    regexp_replace(
        regexp_replace(
            regexp_replace(lower(trim({{ column }})), '^https?://', ''),
            '^www[.]', ''
        ),
        '[/?].*$', ''
    ),
    ''
)
{% endmacro %}
