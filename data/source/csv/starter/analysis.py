"""Starter profile for Inventory & Supply Chain Dataset."""
import sqlite3
from pathlib import Path

database = next(Path(".").glob("*.sqlite"))
with sqlite3.connect(database) as connection:
    query = """WITH movement_totals AS (
  SELECT product_id, warehouse_id, SUM(quantity_change) AS movement_units
  FROM inventory_movements
  GROUP BY product_id, warehouse_id
)
SELECT b.warehouse_id,
       SUM(b.opening_units) AS opening_units,
       SUM(COALESCE(m.movement_units, 0)) AS movement_units,
       SUM(b.opening_units + COALESCE(m.movement_units, 0)) AS closing_units
FROM inventory_opening_balances b
LEFT JOIN movement_totals m USING (product_id, warehouse_id)
GROUP BY b.warehouse_id
ORDER BY b.warehouse_id;"""
    rows = connection.execute(query).fetchall()
    for row in rows:
        print(row)
