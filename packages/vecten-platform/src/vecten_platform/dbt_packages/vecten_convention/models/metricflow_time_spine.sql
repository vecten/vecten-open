{{ config(materialized='table', schema='core') }}
with digits as (
    select 0 as d union all select 1 union all select 2 union all select 3 union all select 4
    union all select 5 union all select 6 union all select 7 union all select 8 union all select 9
),
numbers as (
    select d1.d + 10 * d2.d + 100 * d3.d + 1000 * d4.d + 10000 * d5.d as n
    from digits d1 cross join digits d2 cross join digits d3 cross join digits d4 cross join digits d5
)
select cast({{ vecten_convention.add_days("cast('2000-01-01' as date)", "n") }} as date) as date_day
from numbers
where n < 36525
