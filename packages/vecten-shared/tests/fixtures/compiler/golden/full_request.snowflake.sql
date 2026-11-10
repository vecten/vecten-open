-- parameters: ["2026-01-01", "2027-01-01", "paid", "shipped"]
SELECT UPPER(country_code) AS "country", DATE_TRUNC('month', order_date) AS "order_date", SUM(amount) AS "total_revenue" FROM marts.orders WHERE (order_date >= ?) AND (order_date < ?) AND (status IN (?, ?)) GROUP BY UPPER(country_code), DATE_TRUNC('month', order_date) ORDER BY "total_revenue" DESC LIMIT 500
