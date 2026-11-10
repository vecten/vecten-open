select
    cast(id as integer) as deal_id,
    cast(company_id as integer) as company_id,
    title,
    stage,
    cast(amount as decimal(18, 2)) as amount,
    cast(created_at as timestamp) as created_at,
    cast(close_date as date) as close_date,
    cast(is_won as boolean) as is_won,
    current_timestamp as _loaded_at,
    'demo' as _source
from {{ source('demo', 'deal') }}
