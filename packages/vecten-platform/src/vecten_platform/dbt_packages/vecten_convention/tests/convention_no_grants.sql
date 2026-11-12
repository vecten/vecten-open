{#- The MCP owns every read grant, and dbt's grants config revokes what it does not list. On Snowflake a rebuild without copy_grants drops the grants of the relation it replaces. -#}
{%- set violations = [] -%}
{%- if execute -%}
    {%- for node in graph.nodes.values() -%}
        {%- if node.resource_type in ['model', 'snapshot', 'seed'] -%}
            {%- if node.config.get('grants') -%}
                {%- do violations.append(node.resource_type ~ ' ' ~ node.name ~ ': sets dbt grants; read grants belong to the MCP') -%}
            {%- endif -%}
            {%- if target.type == 'snowflake' and node.resource_type == 'model' and node.config.get('copy_grants') is sameas false -%}
                {%- do violations.append('model ' ~ node.name ~ ': sets copy_grants to false') -%}
            {%- endif -%}
        {%- endif -%}
    {%- endfor -%}
{%- endif -%}
{{ vecten_convention.convention_result(violations) }}
