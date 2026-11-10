-- parameters: ["2026-01-01", "2027-01-01"]
SELECT SUM(amount) AS "total_revenue" FROM marts.orders WHERE (order_date >= ?) AND (order_date < ?) LIMIT 1000
