-- parameters: []
SELECT DATE_TRUNC('month', order_date) AS "order_date", SUM(discount) / SUM(amount) AS "discount_share" FROM marts.orders GROUP BY DATE_TRUNC('month', order_date) LIMIT 1000
