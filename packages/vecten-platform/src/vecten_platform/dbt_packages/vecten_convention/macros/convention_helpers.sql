{% macro convention_regex(kind) -%}
    {%- if kind == 'staging' -%}^staging_[a-z][a-z0-9]*_[a-z][a-z0-9]*(_[a-z0-9]+)*$
    {%- elif kind == 'snapshot' -%}^snapshot_[a-z][a-z0-9]*_[a-z][a-z0-9]*(_[a-z0-9]+)*$
    {%- else -%}^[a-z][a-z0-9]*(_[a-z0-9]+)*$
    {%- endif -%}
{%- endmacro %}

{% macro convention_area(schema) -%}
    {#- The area a `core_<area>` or `marts_<area>` schema belongs to, or none for any other schema. -#}
    {%- set found = modules.re.match('^(core|marts)_([a-z][a-z0-9]*)$', schema | lower) -%}
    {{- return(found.group(2) if found else none) -}}
{%- endmacro %}

{% macro convention_layer(schema) -%}
    {#- The layer whose rules a schema's models follow; an area's schemas follow core's and marts'. Undeclared areas count too, so their other rules still report while the naming test names the missing declaration. -#}
    {%- set name = schema | lower -%}
    {%- if name in ['staging', 'snapshots', 'core', 'marts'] -%}
        {{- return(name) -}}
    {%- elif vecten_convention.convention_area(name) is not none -%}
        {{- return(name.split('_')[0]) -}}
    {%- endif -%}
    {{- return(none) -}}
{%- endmacro %}

{% macro convention_declared_areas() -%}
    {#- vecten passes the areas `vecten.toml` declares as this var. -#}
    {{- return(var('vecten_areas', [])) -}}
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
