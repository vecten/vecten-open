-- parameters: []
SELECT LOWER(channel) AS "channel", SUM(amount) AS "total_revenue" FROM marts.orders GROUP BY LOWER(channel) LIMIT 1000
