{#- Checks the checkable part of the naming rule: prefixes, segments, lowercase, single underscores, and that an area's schemas belong to a declared area. -#}
{%- set violations = [] -%}
{%- for node in vecten_convention.convention_models() -%}
    {%- set schema = node.schema | lower -%}
    {%- set area = vecten_convention.convention_area(schema) -%}
    {%- if area is not none and area not in vecten_convention.convention_declared_areas() -%}
        {%- do violations.append('model ' ~ node.name ~ ': lands in schema ' ~ schema ~ ', an area vecten.toml does not declare; add [areas.' ~ area ~ ']') -%}
    {%- endif -%}
    {%- set layer = vecten_convention.convention_layer(schema) -%}
    {%- if area is not none and layer in ['staging', 'snapshots'] and area not in var('vecten_source_layer_areas', []) -%}
        {%- do violations.append(node.name ~ ': schema ' ~ schema ~ ' needs restricted_source_layers in [areas.' ~ area ~ ']') -%}
    {%- endif -%}
    {%- if node.resource_type == 'snapshot' and area is not none and layer == 'staging' -%}
        {%- do violations.append(node.name ~ ': use snapshots_' ~ area ~ ' for an area snapshot') -%}
    {%- endif -%}
    {%- if node.resource_type == 'snapshot' -%}
        {%- set kind = 'snapshot' -%}
    {%- elif layer == 'staging' -%}
        {%- set kind = 'staging' -%}
    {%- elif layer in ['core', 'marts'] -%}
        {%- set kind = 'core' -%}
    {%- else -%}
        {%- set kind = none -%}
        {%- do violations.append('model ' ~ node.name ~ ': lands in schema ' ~ node.schema ~ '; the convention allows staging, snapshots, core, marts and the core_<area> and marts_<area> schemas of declared areas, plus staging_<area> and snapshots_<area> when enabled') -%}
    {%- endif -%}
    {%- if kind is not none and not modules.re.match(vecten_convention.convention_regex(kind), node.name) -%}
        {%- do violations.append('model ' ~ node.name ~ ': name does not match the ' ~ kind ~ ' rule ' ~ vecten_convention.convention_regex(kind)) -%}
    {%- endif -%}
{%- endfor -%}
{{ vecten_convention.convention_result(violations) }}
