# Inventory & Supply Chain Dataset v1.0.0

A deterministic synthetic operations dataset for inventory balances, supplier performance, lead-time analysis, and fulfillment modeling.

This is synthetic educational data generated to model a fictional business. It does not represent a real company or establish industry benchmarks.

## Contents

- `suppliers`: 250 rows
- `products`: 2,500 rows
- `warehouses`: 12 rows
- `inventory_opening_balances`: 30,000 rows
- `purchase_orders`: 18,000 rows
- `purchase_order_lines`: 53,967 rows
- `inventory_movements`: 138,436 rows

The source CSV ZIP preserves normalized source tables. SQLite contains the same
tables. XLSX and Parquet contain an analysis-ready denormalized view. Starter
examples demonstrate safe read-only exploration.

## Reproducibility

Built by `datasets/generate.py` with version `1.0.0` and seed
`2025090504`. See `manifest.json` for row profiles, reference outputs,
validation checks, file sizes, and SHA-256 checksums.

## License

See `LICENSE.txt`. Limitations from the catalog:
- This is synthetic educational data generated to model a fictional business. It does not represent a real company or establish industry benchmarks.
- Synthetic movements simplify transfer pairing.
- No lot, serial, or expiration tracking.
- No PII is included.
