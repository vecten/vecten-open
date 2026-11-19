{% macro convention_grant_violations() -%}
    {#- The MCP owns every read grant, and dbt's grants config revokes what it does not list. On Snowflake a rebuild without copy_grants drops the grants of the relation it replaces. Reads the graph only, so it runs before anything is built. -#}
    {%- set violations = [] -%}
    {%- if execute -%}
        {%- for node in graph.nodes.values() -%}
            {%- if node.resource_type in ['model', 'snapshot', 'seed'] -%}
                {%- set foreign = node.package_name != project_name -%}
                {%- set label = node.resource_type ~ ' ' ~ (node.package_name ~ '.' if foreign else '') ~ node.name -%}
                {%- set section = node.resource_type ~ 's' -%}
                {%- if node.config.get('grants') -%}
                    {%- set fix = '; override it with +grants: {} under ' ~ section ~ ': ' ~ node.package_name ~ ': in dbt/dbt_project.yml' if foreign else '' -%}
                    {%- do violations.append(label ~ ': sets dbt grants; read grants belong to the MCP' ~ fix) -%}
                {%- endif -%}
                {%- if target.type == 'snowflake' and node.resource_type == 'model' and node.config.get('copy_grants') is sameas false -%}
                    {%- set fix = '; override it with +copy_grants: true under models: ' ~ node.package_name ~ ': in dbt/dbt_project.yml' if foreign else '' -%}
                    {%- do violations.append(label ~ ': sets copy_grants to false' ~ fix) -%}
                {%- endif -%}
            {%- endif -%}
        {%- endfor -%}
    {%- endif -%}
    {{ return(violations) }}
{%- endmacro %}

{% macro convention_grants_gate() -%}
    {#- Runs at the start of every dbt run, so a grants config fails before dbt applies it. -#}
    {%- set violations = vecten_convention.convention_grant_violations() -%}
    {%- if violations | length > 0 -%}
        {{ exceptions.raise_compiler_error('convention_no_grants failed before the build:\n' ~ violations | join('\n')) }}
    {%- endif -%}
{%- endmacro %}
