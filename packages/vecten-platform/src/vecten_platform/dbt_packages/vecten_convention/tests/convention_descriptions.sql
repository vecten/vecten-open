{#- Core and marts are read by people and by the semantic model, so every built column there is declared with a description. Reads the built relations, so it runs after the build. -#}
{%- set violations = [] -%}
{%- for node in vecten_convention.convention_models() -%}
    {%- if node.resource_type == 'model' and vecten_convention.convention_layer(node.schema) in ['core', 'marts'] -%}
        {%- if not (node.description | trim) -%}
            {%- do violations.append('model ' ~ node.name ~ ': has no description') -%}
        {%- endif -%}
        {%- if node.columns | length == 0 -%}
            {%- do violations.append('model ' ~ node.name ~ ': declares no columns in schema.yml') -%}
        {%- endif -%}
        {%- for column in node.columns.values() -%}
            {%- if not (column.description | trim) -%}
                {%- do violations.append('model ' ~ node.name ~ ': column ' ~ column.name ~ ' has no description') -%}
            {%- endif -%}
        {%- endfor -%}
        {%- set declared = node.columns.values() | map(attribute='name') | map('lower') | list -%}
        {%- set relation = api.Relation.create(database=node.database, schema=node.schema, identifier=node.alias) -%}
        {%- set built = adapter.get_columns_in_relation(relation) | map(attribute='name') | map('lower') | list -%}
        {%- if built | length == 0 -%}
            {%- do violations.append('model ' ~ node.name ~ ': has no readable columns (not built, ephemeral or not visible)') -%}
        {%- endif -%}
        {%- for column in built if column not in declared -%}
            {%- do violations.append('model ' ~ node.name ~ ': column ' ~ column ~ ' is not declared in schema.yml') -%}
        {%- endfor -%}
    {%- endif -%}
{%- endfor -%}
{{ vecten_convention.convention_result(violations) }}
