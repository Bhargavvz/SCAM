# Inventory & Supply Chain Dataset data dictionary

Version: 1.0.0  
Coverage: 2023-01-01 through 2025-12-31  
Source type: synthetic

## `suppliers`

Grain: One row per supplier.
| Column | Type | Nullable | Description |
|---|---|---:|---|
| `supplier_id` | text | no | Stable supplier key. |
| `supplier_name` | text | no | Display name. |
| `country_code` | text | no | Supplier country. |
| `lead_time_days` | integer | no | Expected procurement lead time. |
| `reliability_score` | decimal | no | Expected on-time fraction. |

## `products`

Grain: One row per product.
| Column | Type | Nullable | Description |
|---|---|---:|---|
| `product_id` | text | no | Stable product key. |
| `supplier_id` | text | no | Primary supplier. |
| `sku` | text | no | Unique stock keeping unit. |
| `category` | text | no | Inventory category. |
| `unit_cost` | decimal | no | Procurement unit cost. |
| `reorder_point` | integer | no | Target replenishment threshold. |

## `warehouses`

Grain: One row per warehouse.
| Column | Type | Nullable | Description |
|---|---|---:|---|
| `warehouse_id` | text | no | Stable warehouse key. |
| `warehouse_name` | text | no | Display name. |
| `region` | text | no | Operating region. |
| `capacity_units` | integer | no | Nominal unit capacity. |

## `inventory_opening_balances`

Grain: One row per product and warehouse.
| Column | Type | Nullable | Description |
|---|---|---:|---|
| `opening_balance_id` | text | no | Stable key for the product and warehouse opening balance. |
| `product_id` | text | no | Stocked product. |
| `warehouse_id` | text | no | Stocking warehouse. |
| `balance_date` | date | no | Opening balance date; 2023-01-01 in v1.0.0. |
| `opening_units` | integer | no | Units on hand before movements on the balance date. |

## `purchase_orders`

Grain: One row per purchase order.
| Column | Type | Nullable | Description |
|---|---|---:|---|
| `purchase_order_id` | text | no | Stable PO key. |
| `supplier_id` | text | no | Fulfilling supplier. |
| `warehouse_id` | text | no | Receiving warehouse. |
| `ordered_at` | date | no | Order date. |
| `expected_at` | date | no | Expected receipt date. |
| `received_at` | date | yes | Actual receipt date. |
| `status` | text | no | received, partial, open, or cancelled. |

## `purchase_order_lines`

Grain: One row per purchase-order line.
| Column | Type | Nullable | Description |
|---|---|---:|---|
| `purchase_order_line_id` | text | no | Stable line key. |
| `purchase_order_id` | text | no | Parent PO. |
| `product_id` | text | no | Ordered product. |
| `quantity_ordered` | integer | no | Units ordered. |
| `quantity_received` | integer | no | Units received. |
| `unit_cost` | decimal | no | PO unit cost. |

## `inventory_movements`

Grain: One row per product, warehouse, and inventory event.
| Column | Type | Nullable | Description |
|---|---|---:|---|
| `movement_id` | text | no | Stable movement key. |
| `product_id` | text | no | Moved product. |
| `warehouse_id` | text | no | Affected warehouse. |
| `movement_at` | date | no | Movement date. |
| `movement_type` | text | no | receipt, sale, adjustment, or transfer. |
| `quantity_change` | integer | no | Signed stock delta. |
| `purchase_order_line_id` | text | yes | Receipt source line when applicable. |
| `transfer_id` | text | yes | Shared identifier for the equal outbound and inbound rows of a warehouse transfer. |

## Relationships

- `products.supplier_id` → `suppliers.supplier_id` (many-to-one)
- `inventory_opening_balances.product_id` → `products.product_id` (many-to-one)
- `inventory_opening_balances.warehouse_id` → `warehouses.warehouse_id` (many-to-one)
- `purchase_orders.supplier_id` → `suppliers.supplier_id` (many-to-one)
- `purchase_orders.warehouse_id` → `warehouses.warehouse_id` (many-to-one)
- `purchase_order_lines.purchase_order_id` → `purchase_orders.purchase_order_id` (many-to-one)
- `purchase_order_lines.product_id` → `products.product_id` (many-to-one)
- `inventory_movements.product_id` → `products.product_id` (many-to-one)
- `inventory_movements.warehouse_id` → `warehouses.warehouse_id` (many-to-one)
