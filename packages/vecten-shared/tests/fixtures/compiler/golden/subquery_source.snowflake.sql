-- parameters: []
SELECT region AS "region", DATE_TRUNC('month', balance_month) AS "balance_month", SUM(balance) AS "account_balance" FROM (SELECT account_id, region, balance_month, balance FROM marts.balances WHERE NOT region LIKE 'test%') AS "balances" GROUP BY region, DATE_TRUNC('month', balance_month) LIMIT 1000
