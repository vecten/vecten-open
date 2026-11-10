-- parameters: []
SELECT SUM("SS_EXT_SALES_PRICE") AS "total_sales", SUM("SS_NET_PROFIT") AS "total_profit", SUM("SS_NET_PAID") AS "net_paid", COUNT("SS_ITEM_SK") AS "sales_count" FROM VECTEN_CI.MARTS.STORE_SALES LIMIT 1000
