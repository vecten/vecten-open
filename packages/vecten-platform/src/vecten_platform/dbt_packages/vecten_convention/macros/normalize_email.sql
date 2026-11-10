-- Lowercase + trim an email; empty becomes null.
{% macro normalize_email(column) %}
nullif(lower(trim({{ column }})), '')
{% endmacro %}
