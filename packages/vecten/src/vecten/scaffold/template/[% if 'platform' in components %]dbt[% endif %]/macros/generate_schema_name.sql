{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ vecten_convention.generate_schema_name(custom_schema_name, node) }}
{%- endmacro %}
