-- parameters: []
SELECT DATE_TRUNC('week', order_date) AS "order_date", COUNT(order_id) AS "order_count" FROM marts.orders GROUP BY DATE_TRUNC('week', order_date) LIMIT 1000
