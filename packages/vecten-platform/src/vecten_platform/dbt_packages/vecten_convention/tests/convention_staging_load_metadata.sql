{#- Reads the built relations, so it runs after the build. get_columns_in_relation returns no columns for a relation it cannot read, and a built table always has at least one. -#}
{%- set violations = [] -%}
{%- for node in vecten_convention.convention_models() -%}
    {%- if node.resource_type == 'model' and (node.schema | lower) == 'staging' -%}
        {%- set relation = api.Relation.create(database=node.database, schema=node.schema, identifier=node.alias) -%}
        {%- set columns = adapter.get_columns_in_relation(relation) | map(attribute='name') | map('lower') | list -%}
        {%- if columns | length == 0 -%}
            {%- do violations.append('model ' ~ node.name ~ ': has no readable columns (not built, ephemeral or not visible)') -%}
        {%- else -%}
            {%- for required in ['_loaded_at', '_source'] -%}
                {%- if required not in columns -%}
                    {%- do violations.append('model ' ~ node.name ~ ': lacks column ' ~ required) -%}
                {%- endif -%}
            {%- endfor -%}
        {%- endif -%}
    {%- endif -%}
{%- endfor -%}
{{ vecten_convention.convention_result(violations) }}
