{% snapshot snapshot_demo_deal %}
{{ config(
    unique_key='deal_id',
    strategy='check',
    check_cols=['company_id', 'title', 'stage', 'amount', 'created_at', 'close_date', 'is_won'],
    schema='snapshots',
) }}
select * from {{ ref('staging_demo_deal') }}
{% endsnapshot %}
