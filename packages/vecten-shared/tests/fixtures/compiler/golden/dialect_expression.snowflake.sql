-- parameters: []
SELECT LOWER(TRIM(channel)) AS "channel", SUM(amount) AS "total_revenue" FROM marts.orders GROUP BY LOWER(TRIM(channel)) LIMIT 1000
