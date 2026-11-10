{% snapshot snapshot_demo_company %}
{{ config(
    unique_key='company_id',
    strategy='check',
    check_cols=['name', 'domain', 'email', 'country', 'created_at'],
    schema='snapshots',
) }}
select * from {{ ref('staging_demo_company') }}
{% endsnapshot %}
