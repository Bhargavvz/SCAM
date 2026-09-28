# Supply Chain Memory & Decision Agent - extended dataset

Additive extension of the **Inventory & Supply Chain Dataset v1.0.0** with raw-material, production, demand,
institutional-memory, narrative and evaluation data. The original tables are untouched (row counts + SHA-256 verified by `validate.py`).

## Attribution

Source data: Analytics Engineering, "Inventory & Supply Chain Dataset v1.0.0",
https://www.analyticsengineering.com/datasets/inventory-supply-chain - licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
This extension is released under CC BY 4.0. All content is synthetic and fictional; no real companies or people; no PII.

## Files

- `tables/csv/*.csv`, `tables/parquet/*.parquet` - one file per new table
- `inventory-supply-chain-v1.0.0-extended.sqlite` - copy of the v1.0.0 SQLite with the new tables appended (new tables store NULL; source tables keep their original '' blanks)
- `memory_corpus.jsonl` - narrative docs, one Hindsight retain item per line (split=memory is ingested; split=holdout = Q4-2025)
- `eval_questions.jsonl`, `holdout_scenarios.json`, `planted_patterns.json`, `validation_report.json`

## Key modelling choices

- 12 warehouses (not 6); 5 categories: components, consumables, finished_goods, packaging, spares.
- FG PO delay is ~uniform over -3..14 days with corr(delay, reliability)=-0.009; FG lateness carries no supplier/country/month signal, so causal structure and planted patterns live in the new RM-side tables; FG anchors use the most extreme clusters.
- Sales are intermittent: 2.19 sale rows per product-warehouse over the window; product_demand_weekly is therefore stored sparse (weeks with demand or a forecast).
- Seasonality is flat (month index 0.97..1.03, per-day normalised) and trend ~-0.0000/month; forecasts use these fitted values.
- FG stock touches <=0 in only 25 movement rows (14 product-warehouses): unfulfilled demand is rare and appears only there or in logged demand-shock weeks.
- Make-products: the existing FG PO for a make-product is treated as the replenishment order to our own plant; its supplier_id is kept as recorded (read-only) and interpreted as the commercial counterparty.
- RM quantities are decimal (kg, L, m2) or integer (ea); identities are checked to 1e-6 after 4-dp rounding.
- RM-side delays are causal: P(late) = 1 - reliability_score, slip ~ Gamma scaled by lead_time_days, plus country shocks and planted patterns.
- Cascade back-fill: ~12% of late/partial make-product FG lines get an upstream chain (RM ETA revision -> RM below safety -> stockout -> blocked run -> the existing late FG receipt).
- Decision cost = action cost + stockout days x (avg daily RM usage x price x `shortage_value_multiple`) + spot-buy premiums; outcome labels compare realised vs estimated.
- Extra columns beyond the brief: `catalog_line_id`, `rm_po_revision_id` (keys), decisions.`action_cost`/`context_dependent`/`context_factors`, corpus `supersedes_doc_ids`/`author_role`/`event_id`.

## Table relationships

- `products_ext.product_id` -> `products`
- `products_ext.plant_id` -> `plants`
- `bill_of_materials.product_id` -> `products`
- `bill_of_materials.rm_id` -> `raw_materials`
- `rm_supplier_catalog.rm_id` -> `raw_materials`
- `rm_supplier_catalog.supplier_id` -> `suppliers`
- `rm_supplier_catalog.contract_id` -> `contracts`
- `contracts.supplier_id` -> `suppliers`
- `rm_inventory_opening_balances.rm_id/plant_id` -> `raw_materials / plants`
- `rm_purchase_orders.supplier_id` -> `suppliers`
- `rm_purchase_orders.plant_id` -> `plants`
- `rm_purchase_order_lines.rm_po_id` -> `rm_purchase_orders`
- `rm_purchase_order_lines.rm_id` -> `raw_materials`
- `rm_po_revisions.rm_po_id` -> `rm_purchase_orders`
- `rm_inventory_movements.rm_purchase_order_line_id` -> `rm_purchase_order_lines`
- `rm_inventory_movements.production_run_id` -> `production_runs`
- `rm_inventory_snapshots_weekly.rm_id/plant_id` -> `raw_materials / plants`
- `production_runs.purchase_order_line_id` -> `purchase_order_lines`
- `production_runs.product_id` -> `products`
- `production_runs.plant_id` -> `plants`
- `production_runs.rm_shortage_rm_id` -> `raw_materials`
- `product_demand_weekly.product_id/warehouse_id` -> `products / warehouses`
- `product_demand_weekly.demand_shock_event_id` -> `disruption_events`
- `event_impacts.event_id` -> `disruption_events`
- `event_links.src_event_id/dst_event_id` -> `disruption_events`
- `decisions.event_id` -> `disruption_events`
- `commitments.decision_id` -> `decisions`
- `commitments.negotiation_id` -> `negotiations`
- `negotiations.supplier_id` -> `suppliers`
- `negotiations.contract_id` -> `contracts`
- `supplier_scorecard_monthly.supplier_id` -> `suppliers`

## Data dictionary

### `bill_of_materials`

Versioned BOM lines: RM qty per FG unit, scrap, validity window.

Rows: 10,164

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `bom_id` | bom id | str | 0 | PK |
| `product_id` | product id | str | 0 | FK |
| `rm_id` | rm id | str | 0 | FK |
| `qty_per_unit` | qty per unit | float64 | 0 |  |
| `uom` | uom | str | 0 |  |
| `scrap_pct` | scrap pct | float64 | 0 |  |
| `effective_from` | effective from | datetime64[us] | 0 |  |
| `effective_to` | effective to | datetime64[ns] | 9,696 |  |
| `bom_version` | bom version | int64 | 0 |  |
| `change_reason` | change reason | str | 0 |  |

### `commitments`

Commitments made by suppliers, plants and planning, and how they resolved.

Rows: 1,827

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `commitment_id` | commitment id | str | 0 | PK |
| `decision_id` | decision id | string | 131 | FK |
| `negotiation_id` | negotiation id | string | 1,739 | FK |
| `counterparty_type` | counterparty type | str | 0 |  |
| `counterparty_id` | counterparty id | str | 0 | FK |
| `made_by_role` | made by role | str | 0 |  |
| `made_at` | made at | datetime64[us] | 0 |  |
| `commitment_text` | commitment text | str | 0 |  |
| `quantity` | quantity | float64 | 45 |  |
| `due_date` | due date | datetime64[us] | 0 |  |
| `penalty_or_credit` | penalty or credit | str | 0 |  |
| `status` | status | str | 0 |  |
| `resolved_at` | resolved at | datetime64[us] | 36 |  |

### `contracts`

Supplier contracts (terms, min volume, penalties, force majeure).

Rows: 310

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `contract_id` | contract id | str | 0 | PK |
| `supplier_id` | supplier id | str | 0 | FK |
| `scope` | scope | str | 0 |  |
| `valid_from` | valid from | datetime64[us] | 0 |  |
| `valid_to` | valid to | datetime64[us] | 0 |  |
| `price_terms` | price terms | str | 0 |  |
| `min_volume` | min volume | float64 | 0 |  |
| `penalty_clause` | penalty clause | str | 0 |  |
| `force_majeure_flag` | force majeure flag | int64 | 0 |  |

### `decisions`

Decisions: options, rationale, expected vs actual outcome, lesson, footprint.

Rows: 837

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `decision_id` | decision id | str | 0 | PK |
| `event_id` | event id | str | 0 | FK |
| `decided_at` | decided at | datetime64[us] | 0 |  |
| `decided_by_role` | decided by role | str | 0 |  |
| `decision_type` | decision type | str | 0 |  |
| `options_considered_json` | options considered json | str | 0 |  |
| `chosen_option` | chosen option | str | 0 |  |
| `rationale_text` | rationale text | str | 0 |  |
| `expected_cost` | expected cost | float64 | 0 |  |
| `expected_stockout_days` | expected stockout days | int64 | 0 |  |
| `actual_cost` | actual cost | float64 | 0 |  |
| `actual_stockout_days` | actual stockout days | int64 | 0 |  |
| `outcome_label` | outcome label | str | 0 |  |
| `outcome_assessed_at` | outcome assessed at | datetime64[us] | 0 |  |
| `lesson_text` | lesson text | str | 0 |  |
| `footprint_ids` | footprint ids | str | 0 |  |
| `action_cost` | action cost | float64 | 0 |  |
| `context_dependent` | context dependent | int64 | 0 |  |
| `context_factors` | context factors | str | 0 |  |

### `disruption_events`

Disruptions, anchored in existing data or in the RM-side simulation.

Rows: 565

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `event_id` | event id | str | 0 | PK |
| `event_type` | event type | str | 0 |  |
| `title` | title | str | 0 |  |
| `start_date` | start date | datetime64[us] | 0 |  |
| `end_date` | end date | datetime64[us] | 0 |  |
| `detected_at` | detected at | datetime64[us] | 0 |  |
| `severity` | severity | int64 | 0 |  |
| `root_cause_code` | root cause code | str | 0 |  |
| `root_cause_text` | root cause text | str | 0 |  |
| `origin_entity_type` | origin entity type | str | 0 |  |
| `origin_entity_id` | origin entity id | str | 0 | FK |
| `anchor_type` | anchor type | str | 0 |  |
| `anchor_ids` | anchor ids | str | 0 |  |

### `event_impacts`

Quantified impacts of each event on entities.

Rows: 4,954

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `event_id` | event id | str | 0 | FK |
| `entity_type` | entity type | str | 0 |  |
| `entity_id` | entity id | str | 0 | FK |
| `impact_metric` | impact metric | str | 0 |  |
| `impact_value` | impact value | float64 | 0 |  |
| `unit` | unit | str | 0 |  |

### `event_links`

Event graph: caused, similar_to, recurrence_of, mitigated_by, superseded_by.

Rows: 367

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `src_event_id` | src event id | str | 0 | FK |
| `dst_event_id` | dst event id | str | 0 | FK |
| `link_type` | link type | str | 0 |  |

### `negotiations`

Supplier negotiations (asks, offers, concessions, final terms).

Rows: 52

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `negotiation_id` | negotiation id | str | 0 | PK |
| `supplier_id` | supplier id | str | 0 | FK |
| `rm_id` | rm id | string | 3 | FK |
| `started_at` | started at | datetime64[us] | 0 |  |
| `concluded_at` | concluded at | datetime64[us] | 1 |  |
| `topic` | topic | str | 0 |  |
| `our_ask` | our ask | str | 0 |  |
| `their_offer` | their offer | str | 0 |  |
| `concessions_json` | concessions json | str | 0 |  |
| `final_terms` | final terms | str | 0 |  |
| `outcome` | outcome | str | 0 |  |
| `contract_id` | contract id | string | 0 | FK |

### `plants`

Our manufacturing plants (fictional).

Rows: 5

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `plant_id` | plant id | str | 0 | PK |
| `plant_name` | plant name | str | 0 |  |
| `region` | region | str | 0 |  |
| `country_code` | country code | str | 0 |  |
| `capacity_units_per_week` | capacity units per week | int64 | 0 |  |

### `product_demand_weekly`

Sparse weekly demand/forecast per product x warehouse; fulfilled = existing sales exactly.

Rows: 57,295

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `product_id` | product id | str | 0 | FK |
| `warehouse_id` | warehouse id | string | 0 | FK |
| `week_start` | week start | datetime64[us] | 0 |  |
| `forecast_units` | forecast units | float64 | 0 |  |
| `forecast_made_at` | forecast made at | datetime64[us] | 0 |  |
| `actual_demand_units` | actual demand units | int64 | 0 |  |
| `fulfilled_units` | fulfilled units | int64 | 0 |  |
| `unfulfilled_units` | unfulfilled units | int64 | 0 |  |
| `promo_flag` | promo flag | int64 | 0 |  |
| `price_index` | price index | float64 | 0 |  |
| `demand_shock_event_id` | demand shock event id | string | 57,262 | FK |

### `production_runs`

One run per make-product FG PO line (received or open); delay and RM-shortage cause.

Rows: 25,552

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `production_run_id` | production run id | str | 0 | PK |
| `plant_id` | plant id | string | 0 | FK |
| `product_id` | product id | string | 0 | FK |
| `purchase_order_line_id` | purchase order line id | string | 0 | FK |
| `planned_start` | planned start | datetime64[us] | 0 |  |
| `actual_start` | actual start | datetime64[us] | 689 |  |
| `planned_qty` | planned qty | int64 | 0 |  |
| `produced_qty` | produced qty | Int64 | 689 |  |
| `delay_days` | delay days | Int64 | 689 |  |
| `delay_reason_code` | delay reason code | string | 6,171 |  |
| `rm_shortage_rm_id` | rm shortage rm id | string | 23,169 | FK |

### `products_ext`

Extension of products: make/buy, plant, brand family, ABC class, price and margin.

Rows: 2,500

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `product_id` | product id | string | 0 | PK |
| `sourcing_mode` | sourcing mode | str | 0 |  |
| `plant_id` | plant id | string | 1,284 | FK |
| `brand_family` | brand family | str | 0 |  |
| `abc_class` | abc class | str | 0 |  |
| `unit_price` | unit price | float64 | 0 |  |
| `gross_margin_pct` | gross margin pct | float64 | 0 |  |

### `raw_materials`

Raw-material master (archetypes chosen per source product category).

Rows: 134

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_id` | rm id | str | 0 | PK |
| `rm_name` | rm name | str | 0 |  |
| `rm_category` | rm category | str | 0 |  |
| `uom` | uom | str | 0 |  |
| `std_unit_cost` | std unit cost | float64 | 0 |  |
| `criticality` | criticality | str | 0 |  |
| `is_single_source` | is single source | int64 | 0 |  |
| `is_hazmat` | is hazmat | int64 | 0 |  |
| `substitute_group_id` | substitute group id | string | 81 | FK |

### `rm_inventory_movements`

Signed RM stock movements (receipt, consumption, adjustment, transfer, scrap).

Rows: 245,639

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_movement_id` | rm movement id | str | 0 | PK |
| `rm_id` | rm id | str | 0 | FK |
| `plant_id` | plant id | string | 0 | FK |
| `movement_at` | movement at | datetime64[us] | 0 |  |
| `movement_type` | movement type | str | 0 |  |
| `quantity_change` | quantity change | float64 | 0 |  |
| `rm_purchase_order_line_id` | rm purchase order line id | string | 203,995 | FK |
| `transfer_id` | transfer id | string | 245,595 | FK |
| `production_run_id` | production run id | string | 47,641 | FK |

### `rm_inventory_opening_balances`

RM opening stock per RM x plant at the simulation start.

Rows: 652

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_opening_balance_id` | rm opening balance id | str | 0 | PK |
| `rm_id` | rm id | str | 0 | FK |
| `plant_id` | plant id | str | 0 | FK |
| `balance_date` | balance date | datetime64[us] | 0 |  |
| `opening_units` | opening units | float64 | 0 |  |

### `rm_inventory_snapshots_weekly`

Sunday RM snapshots; on_hand = opening + cumulative movements.

Rows: 107,580

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_id` | rm id | str | 0 | FK |
| `plant_id` | plant id | str | 0 | FK |
| `snapshot_date` | snapshot date | datetime64[us] | 0 |  |
| `on_hand_units` | on hand units | float64 | 0 |  |
| `safety_stock_units` | safety stock units | float64 | 0 |  |
| `days_of_cover` | days of cover | float64 | 0 |  |
| `stockout_flag` | stockout flag | int64 | 0 |  |

### `rm_po_revisions`

Supplier ETA revisions and expedites on RM POs.

Rows: 11,058

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_po_revision_id` | rm po revision id | str | 0 | PK |
| `rm_po_id` | rm po id | str | 0 | FK |
| `revised_at` | revised at | datetime64[us] | 0 |  |
| `old_expected_at` | old expected at | datetime64[us] | 0 |  |
| `new_expected_at` | new expected at | datetime64[us] | 0 |  |
| `reason_code` | reason code | str | 0 |  |

### `rm_purchase_order_lines`

RM PO lines.

Rows: 40,837

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_purchase_order_line_id` | rm purchase order line id | str | 0 | PK |
| `rm_po_id` | rm po id | str | 0 | FK |
| `rm_id` | rm id | str | 0 | FK |
| `quantity_ordered` | quantity ordered | float64 | 0 |  |
| `quantity_received` | quantity received | float64 | 0 |  |
| `unit_cost` | unit cost | float64 | 0 |  |

### `rm_purchase_orders`

RM purchase orders (FG PO columns + promised_at; plant instead of warehouse).

Rows: 40,358

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_po_id` | rm po id | str | 0 | PK |
| `supplier_id` | supplier id | str | 0 | FK |
| `plant_id` | plant id | str | 0 | FK |
| `ordered_at` | ordered at | datetime64[us] | 0 |  |
| `expected_at` | expected at | datetime64[us] | 0 |  |
| `promised_at` | promised at | datetime64[us] | 0 |  |
| `received_at` | received at | datetime64[us] | 216 |  |
| `status` | status | str | 0 |  |

### `rm_supplier_catalog`

RM x supplier x price-validity segment: rank, allocation, lead time, MOQ, price, contract.

Rows: 901

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `catalog_line_id` | catalog line id | str | 0 | PK |
| `rm_id` | rm id | str | 0 | FK |
| `supplier_id` | supplier id | str | 0 | FK |
| `sourcing_rank` | sourcing rank | str | 0 |  |
| `allocation_pct` | allocation pct | float64 | 0 |  |
| `lead_time_days` | lead time days | int64 | 0 |  |
| `moq` | moq | float64 | 0 |  |
| `unit_price` | unit price | float64 | 0 |  |
| `price_valid_from` | price valid from | datetime64[us] | 0 |  |
| `price_valid_to` | price valid to | datetime64[us] | 267 |  |
| `contract_id` | contract id | str | 0 | FK |
| `qualified_flag` | qualified flag | int64 | 0 |  |

### `supplier_scorecard_monthly`

Monthly supplier scorecard computed from FG + RM PO tables and commitments.

Rows: 8,152

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `supplier_id` | supplier id | str | 0 | FK |
| `month` | month | str | 0 |  |
| `po_count` | po count | int64 | 0 |  |
| `otif_rate` | otif rate | float64 | 53 |  |
| `avg_delay_days` | avg delay days | float64 | 53 |  |
| `fill_rate` | fill rate | float64 | 53 |  |
| `quality_ppm` | quality ppm | float64 | 0 |  |
| `commitments_made` | commitments made | int64 | 0 |  |
| `commitments_kept` | commitments kept | int64 | 0 |  |
