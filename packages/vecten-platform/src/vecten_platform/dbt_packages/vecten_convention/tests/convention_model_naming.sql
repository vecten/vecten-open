{#- Checks the checkable part of the naming rule: prefixes, segments, lowercase, single underscores. -#}
{%- set violations = [] -%}
{%- for node in vecten_convention.convention_models() -%}
    {%- set schema = node.schema | lower -%}
    {%- if node.resource_type == 'snapshot' -%}
        {%- set kind = 'snapshot' -%}
    {%- elif schema == 'staging' -%}
        {%- set kind = 'staging' -%}
    {%- elif schema in ['core', 'marts'] -%}
        {%- set kind = 'core' -%}
    {%- else -%}
        {%- set kind = none -%}
        {%- do violations.append('model ' ~ node.name ~ ': lands in schema ' ~ node.schema ~ '; the convention allows staging, snapshots, core and marts') -%}
    {%- endif -%}
    {%- if kind is not none and not modules.re.match(vecten_convention.convention_regex(kind), node.name) -%}
        {%- do violations.append('model ' ~ node.name ~ ': name does not match the ' ~ kind ~ ' rule ' ~ vecten_convention.convention_regex(kind)) -%}
    {%- endif -%}
{%- endfor -%}
{{ vecten_convention.convention_result(violations) }}
