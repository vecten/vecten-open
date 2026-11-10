-- parameters: ["2026-01-01", "2027-01-01"]
SELECT SUM(amount) AS "total_revenue" FROM marts.orders WHERE (order_date >= %s) AND (order_date < %s) LIMIT 1000
