select
    deal_id,
    company_id,
    title,
    stage,
    amount,
    created_at,
    close_date,
    is_won
from {{ ref('snapshot_demo_deal') }}
where dbt_valid_to is null
