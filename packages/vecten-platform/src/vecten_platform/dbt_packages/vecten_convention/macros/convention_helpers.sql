{% macro convention_regex(kind) -%}
    {%- if kind == 'staging' -%}^staging_[a-z][a-z0-9]*_[a-z][a-z0-9]*(_[a-z0-9]+)*$
    {%- elif kind == 'snapshot' -%}^snapshot_[a-z][a-z0-9]*_[a-z][a-z0-9]*(_[a-z0-9]+)*$
    {%- else -%}^[a-z][a-z0-9]*(_[a-z0-9]+)*$
    {%- endif -%}
{%- endmacro %}

{% macro convention_models() -%}
    {#- Every model and snapshot in the graph except seeds and the convention package's own models. -#}
    {%- set out = [] -%}
    {%- if execute -%}
        {%- for node in graph.nodes.values() -%}
            {%- if node.resource_type in ['model', 'snapshot'] and node.package_name != 'vecten_convention' -%}
                {%- do out.append(node) -%}
            {%- endif -%}
        {%- endfor -%}
    {%- endif -%}
    {{ return(out) }}
{%- endmacro %}

{% macro convention_result(violations) -%}
    {%- if violations | length > 0 -%}
        {{ exceptions.raise_compiler_error('convention violations:\n' ~ violations | join('\n')) }}
    {%- endif -%}
    select 1 as violation where 1 = 0
{%- endmacro %}
