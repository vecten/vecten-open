-- parameters: ["pa%"]
SELECT SUM(amount % 100) AS "cents" FROM marts.orders WHERE (status LIKE ?) LIMIT 1000
