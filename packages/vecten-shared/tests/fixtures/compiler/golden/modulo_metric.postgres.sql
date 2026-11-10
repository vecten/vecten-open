-- parameters: ["pa%"]
SELECT SUM(amount % 100) AS "cents" FROM marts.orders WHERE (status LIKE %s) LIMIT 1000
