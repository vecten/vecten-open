-- parameters: []
SELECT status AS "status", SUM(amount) AS "total_revenue", COUNT(order_id) AS "order_count" FROM marts.orders GROUP BY status ORDER BY "status" ASC LIMIT 1000
