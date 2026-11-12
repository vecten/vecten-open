{#- A core model's primary key is a column carrying both the unique and the not_null test. -#}
{%- set violations = [] -%}
{%- for node in vecten_convention.convention_models() -%}
    {%- if node.resource_type == 'model' and vecten_convention.convention_layer(node.schema) == 'core' -%}
        {%- set tests_by_column = {} -%}
        {%- for test in graph.nodes.values() -%}
            {%- if test.resource_type == 'test' and test.attached_node == node.unique_id and test.column_name
                and test.test_metadata and test.test_metadata.name in ['unique', 'not_null'] -%}
                {%- do tests_by_column.setdefault(test.column_name | lower, []).append(test.test_metadata.name) -%}
            {%- endif -%}
        {%- endfor -%}
        {%- set keys = [] -%}
        {%- for column, names in tests_by_column.items() -%}
            {%- if 'unique' in names and 'not_null' in names -%}
                {%- do keys.append(column) -%}
            {%- endif -%}
        {%- endfor -%}
        {%- if keys | length == 0 -%}
            {%- do violations.append('model ' ~ node.name ~ ': declares no primary key column with unique and not_null tests') -%}
        {%- endif -%}
    {%- endif -%}
{%- endfor -%}
{{ vecten_convention.convention_result(violations) }}
