select
    c.company_id,
    c.name,
    c.domain,
    c.email,
    c.country,
    count(d.deal_id) as deal_count,
    sum(d.amount) as total_amount,
    cast(min(d.created_at) as date) as first_deal_date
from {{ ref('company') }} as c
left join {{ ref('deal') }} as d on d.company_id = c.company_id
group by c.company_id, c.name, c.domain, c.email, c.country
