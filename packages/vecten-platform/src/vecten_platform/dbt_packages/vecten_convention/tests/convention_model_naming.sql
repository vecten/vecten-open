{#- Checks the checkable part of the naming rule: prefixes, segments, lowercase, single underscores, and that an area's schemas belong to a declared area. -#}
{%- set violations = [] -%}
{%- for node in vecten_convention.convention_models() -%}
    {%- set schema = node.schema | lower -%}
    {%- if node.resource_type == 'snapshot' -%}
        {%- set kind = 'snapshot' -%}
    {%- elif schema == 'staging' -%}
        {%- set kind = 'staging' -%}
    {%- elif vecten_convention.convention_layer(schema) in ['core', 'marts'] -%}
        {%- set kind = 'core' -%}
        {%- set area = vecten_convention.convention_area(schema) -%}
        {%- if area is not none and area not in vecten_convention.convention_declared_areas() -%}
            {%- do violations.append('model ' ~ node.name ~ ': lands in schema ' ~ schema ~ ', an area vecten.toml does not declare; add [areas.' ~ area ~ ']') -%}
        {%- endif -%}
    {%- else -%}
        {%- set kind = none -%}
        {%- do violations.append('model ' ~ node.name ~ ': lands in schema ' ~ node.schema ~ '; the convention allows staging, snapshots, core, marts and the core_<area> and marts_<area> schemas of declared areas') -%}
    {%- endif -%}
    {%- if kind is not none and not modules.re.match(vecten_convention.convention_regex(kind), node.name) -%}
        {%- do violations.append('model ' ~ node.name ~ ': name does not match the ' ~ kind ~ ' rule ' ~ vecten_convention.convention_regex(kind)) -%}
    {%- endif -%}
{%- endfor -%}
{{ vecten_convention.convention_result(violations) }}
