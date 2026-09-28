-- Inventory & Supply Chain Dataset starter queries
-- Checked against the v1.0.0 SQLite release.
-- Question: How do opening balances and signed movements reconcile to closing units by warehouse?
WITH movement_totals AS (
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
ORDER BY b.warehouse_id;

-- Basic exploration:
SELECT COUNT(*) AS row_count
FROM suppliers;

SELECT *
FROM suppliers
LIMIT 20;
