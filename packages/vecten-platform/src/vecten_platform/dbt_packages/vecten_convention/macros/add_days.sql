{% macro add_days(date_expression, days) -%}
    {%- if target.type == 'snowflake' -%}
        dateadd(day, {{ days }}, {{ date_expression }})
    {%- elif target.type == 'postgres' -%}
        ({{ date_expression }} + ({{ days }}) * interval '1 day')
    {%- else -%}
        {{ exceptions.raise_compiler_error("vecten_convention.add_days supports postgres and snowflake; got " ~ target.type) }}
    {%- endif -%}
{%- endmacro %}
