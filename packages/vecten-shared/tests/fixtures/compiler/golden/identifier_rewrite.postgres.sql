-- parameters: []
SELECT UPPER(country_code) AS "country", SUM(amount - discount) AS "net_revenue" FROM marts.orders GROUP BY UPPER(country_code) LIMIT 1000
