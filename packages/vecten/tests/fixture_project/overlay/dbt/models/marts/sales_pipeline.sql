select
    d.deal_id,
    c.name as company_name,
    c.country,
    d.stage,
    d.amount,
    d.created_at,
    d.close_date,
    d.is_won
from {{ ref('deal') }} as d
inner join {{ ref('company') }} as c on c.company_id = d.company_id
