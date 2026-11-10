select
    company_id,
    name,
    domain,
    email,
    country,
    created_at
from {{ ref('snapshot_demo_company') }}
where dbt_valid_to is null
