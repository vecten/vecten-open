select
    cast(id as integer) as company_id,
    name,
    {{ vecten_convention.normalize_domain('domain') }} as domain,
    {{ vecten_convention.normalize_email('email') }} as email,
    country,
    cast(created_at as timestamp) as created_at,
    current_timestamp as _loaded_at,
    'demo' as _source
from {{ source('demo', 'company') }}
