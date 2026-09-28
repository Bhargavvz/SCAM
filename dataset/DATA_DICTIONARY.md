# Data dictionary - Inventory & Supply Chain Dataset v1.0.0 + extension v1.1.0-ext

Coverage: 2023-01-01 through 2025-12-31 (RM ledger starts earlier to feed early production runs).

Original v1.0.0 tables are unchanged; see the source DATA_DICTIONARY.md for them. New tables follow.

## `plants`

Grain: One row per manufacturing plant.  
Rows: 5

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `plant_id` | Stable plant key (P01...). | text | none | PK |
| `plant_name` | Fictional site name. | text | none |  |
| `region` | Operating region; reuses warehouse region names. | text | none |  |
| `country_code` | Plant country. | text | none |  |
| `capacity_units_per_week` | Nominal FG output capacity per week (p95 of weekly output x headroom). | integer | none |  |

## `products_ext`

Grain: One row per product (1:1 extension of products).  
Rows: 2,500

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `product_id` | Product key. | text | none | PK, FK -> products |
| `sourcing_mode` | make (produced at plant_id under a tolling arrangement) or buy. | text | none |  |
| `plant_id` | Producing plant for make products. | text | 1,394 (56%) | FK -> plants |
| `brand_family` | Fictional brand family grouping similar SKUs. | text | none |  |
| `abc_class` | A/B/C by cumulative 2023-2025 sales value. | text | none |  |
| `unit_price` | List selling price = unit_cost / (1 - gross_margin_pct/100). | decimal | none |  |
| `gross_margin_pct` | Gross margin in percent. | decimal | none |  |

## `raw_materials`

Grain: One row per raw material.  
Rows: 104

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_id` | Stable raw-material key (RM0001...). | text | none | PK |
| `rm_name` | Descriptive fictional name. | text | none |  |
| `rm_category` | Material family (surfactant, pp_resin, corrugate, ...). | text | none |  |
| `uom` | Unit of measure for quantities (kg, m2, m, ea). | text | none |  |
| `std_unit_cost` | Standard cost per uom (USD). | decimal | none |  |
| `criticality` | A (stops lines, hard to replace) / B / C. | text | none |  |
| `is_single_source` | 1 if only one qualified supplier in the catalog at any time. | integer | none |  |
| `is_hazmat` | 1 if dangerous goods handling applies. | integer | none |  |
| `substitute_group_id` | RMs sharing a group can replace each other after QA approval. | text | 44 (42%) |  |

## `bill_of_materials`

Grain: One row per product, raw material and BOM version.  
Rows: 8,949

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `bom_id` | Stable BOM line key. | text | none | PK |
| `product_id` | Make product. | text | none | FK -> products |
| `rm_id` | Component raw material. | text | none | FK -> raw_materials |
| `qty_per_unit` | RM quantity (uom) per FG unit, before scrap. | decimal | none |  |
| `uom` | Unit of measure of qty_per_unit. | text | none |  |
| `scrap_pct` | Planned scrap allowance in percent; consumption = produced * qty * (1 + scrap_pct/100). | decimal | none |  |
| `effective_from` | First date the line is valid. | date | none |  |
| `effective_to` | Last date the line is valid; empty = open-ended. | date | 8,518 (95%) |  |
| `bom_version` | Version number per product (1 = initial). | integer | none |  |
| `change_reason` | Why this version exists. | text | none |  |

## `contracts`

Grain: One row per supplier contract term.  
Rows: 269

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `contract_id` | Stable contract key (CTR0001...). | text | none | PK |
| `supplier_id` | Contracted supplier. | text | none | FK -> suppliers |
| `scope` | Materials covered. | text | none |  |
| `valid_from` | Start of term. | date | none |  |
| `valid_to` | End of term. | date | none |  |
| `price_terms` | fixed | index_linked | cost_plus with detail. | text | none |  |
| `min_volume` | Minimum committed purchase value over the term (USD). | decimal | none |  |
| `penalty_clause` | Late-delivery / volume shortfall penalty wording. | text | none |  |
| `force_majeure_flag` | 1 if a force-majeure clause applies. | integer | none |  |

## `rm_supplier_catalog`

Grain: One row per raw material, supplier and price-validity period.  
Rows: 805

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `catalog_id` | Surrogate key (CAT00001...). | text | none | PK |
| `rm_id` | Raw material. | text | none | FK -> raw_materials |
| `supplier_id` | Existing supplier re-used as RM source. | text | none | FK -> suppliers |
| `sourcing_rank` | primary | secondary | tertiary during the period. | text | none |  |
| `allocation_pct` | Share of RM orders placed with this supplier during the period. | decimal | none |  |
| `lead_time_days` | Quoted lead time = supplier lead_time_days (+ hazmat handling). | integer | none |  |
| `moq` | Minimum order quantity (uom). | decimal | none |  |
| `unit_price` | Price per uom (USD) during the period. | decimal | none |  |
| `price_valid_from` | Period start. | date | none |  |
| `price_valid_to` | Period end. | date | none |  |
| `contract_id` | Governing contract. | text | none | FK -> contracts |
| `qualified_flag` | 1 if QA-qualified; unqualified sources get 0% allocation. | integer | none |  |

## `rm_inventory_opening_balances`

Grain: One row per raw material and plant.  
Rows: 478

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_opening_balance_id` | Key rm_id-plant_id. | text | none | PK |
| `rm_id` | Raw material. | text | none | FK -> raw_materials |
| `plant_id` | Plant. | text | none | FK -> plants |
| `balance_date` | RM ledger start (Monday before the first production run feeding a 2023 receipt). | date | none |  |
| `opening_units` | On hand (uom) before movements on balance_date. | decimal | none |  |

## `rm_purchase_orders`

Grain: One row per raw-material purchase order.  
Rows: 24,959

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_purchase_order_id` | Stable RM PO key (RPO...). | text | none | PK |
| `supplier_id` | Supplier. | text | none | FK -> suppliers |
| `plant_id` | Receiving plant. | text | none | FK -> plants |
| `order_type` | regular | spot_buy (switch/emergency) | replacement. | text | none |  |
| `ordered_at` | Order date. | date | none |  |
| `promised_at` | Supplier's original promised receipt date. | date | none |  |
| `expected_at` | Latest expected receipt date after revisions. | date | none |  |
| `received_at` | Actual receipt date. | date | 222 (1%) |  |
| `status` | received, partial, open, or cancelled. | text | none |  |

## `rm_purchase_order_lines`

Grain: One row per RM purchase-order line.  
Rows: 30,562

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_purchase_order_line_id` | Stable line key (RPOL...). | text | none | PK |
| `rm_purchase_order_id` | Parent RM PO. | text | none | FK -> rm_purchase_orders |
| `rm_id` | Ordered raw material. | text | none | FK -> raw_materials |
| `quantity_ordered` | Quantity ordered (uom). | decimal | none |  |
| `quantity_received` | Quantity received (uom) before quality rejects. | decimal | none |  |
| `unit_cost` | Price paid per uom (USD). | decimal | none |  |

## `rm_po_revisions`

Grain: One row per change of an RM PO's expected date.  
Rows: 7,484

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `revision_id` | Surrogate key (REV000001...). | text | none | PK |
| `rm_po_id` | Revised RM PO. | text | none | FK -> rm_purchase_orders |
| `revised_at` | Date the new date was communicated. | date | none |  |
| `old_expected_at` | Expected date before the revision. | date | none |  |
| `new_expected_at` | Expected date after the revision. | date | none |  |
| `reason_code` | Why the date moved (supplier_capacity, port_congestion, expedite_air_freight, ...). | text | none |  |

## `rm_inventory_movements`

Grain: One row per raw material, plant and inventory event.  
Rows: 210,766

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_movement_id` | Stable movement key (RMM...). | text | none | PK |
| `rm_id` | Moved raw material. | text | none | FK -> raw_materials |
| `plant_id` | Affected plant. | text | none | FK -> plants |
| `movement_at` | Movement date. | date | none |  |
| `movement_type` | receipt, consumption, adjustment, transfer, or scrap. | text | none |  |
| `quantity_change` | Signed stock delta (uom). | decimal | none |  |
| `rm_purchase_order_line_id` | Receipt / receipt-reject source line. | text | 179,782 (85%) | FK -> rm_purchase_order_lines |
| `transfer_id` | Shared id of the equal outbound and inbound rows of an inter-plant transfer. | text | 210,664 (100%) |  |
| `production_run_id` | Consuming production run. | text | 35,164 (17%) | FK -> production_runs |

## `rm_inventory_snapshots_weekly`

Grain: One row per raw material, plant and week (Sunday close).  
Rows: 75,524

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `rm_id` | Raw material. | text | none | PK, FK -> raw_materials |
| `plant_id` | Plant. | text | none | PK, FK -> plants |
| `snapshot_date` | Sunday; on hand includes all movements on or before this date. | date | none | PK |
| `on_hand_units` | opening_units + cumulative quantity_change. | decimal | none |  |
| `safety_stock_units` | Safety-stock target in force. | decimal | none |  |
| `days_of_cover` | on_hand / average planned daily requirement of the next 28 days; empty if no requirement. | decimal | 9,437 (12%) |  |
| `stockout_flag` | 1 if during the week a scheduled run was blocked by insufficient stock or on hand <= 0. | integer | none |  |

## `production_runs`

Grain: One row per production run (one per make-product PO line with output or still open).  
Rows: 23,417

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `production_run_id` | Stable run key (PR...). | text | none | PK |
| `plant_id` | Producing plant. | text | none | FK -> plants |
| `product_id` | Product made. | text | none | FK -> products |
| `purchase_order_line_id` | Existing FG PO line this run replenishes. | text | none | FK -> purchase_order_lines |
| `planned_start` | Scheduled start = expected_at - planned production+transit lead. | date | none |  |
| `actual_start` | Actual start; empty if not started by 2025-12-31. | date | 629 (3%) |  |
| `planned_qty` | = quantity_ordered of the FG line. | integer | none |  |
| `produced_qty` | = quantity_received of the FG line. | integer | none |  |
| `delay_days` | actual_start - planned_start (or days waiting at window end). | integer | none |  |
| `delay_reason_code` | Reason for delay > 0 (rm_shortage, changeover_overrun, ...). | text | 5,480 (23%) |  |
| `rm_shortage_rm_id` | RM that blocked the run when reason = rm_shortage. | text | 21,030 (90%) | FK -> raw_materials |

## `product_demand_weekly`

Grain: One row per product, warehouse and ISO week with demand or a non-zero forecast.  
Rows: 71,540

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `product_id` | Product. | text | none | PK, FK -> products |
| `warehouse_id` | Warehouse. | text | none | PK, FK -> warehouses |
| `week_start` | Monday of the week. | date | none | PK |
| `forecast_units` | Forecast for the week. | integer | none |  |
| `forecast_made_at` | Date the forecast was frozen. | date | none |  |
| `actual_demand_units` | fulfilled_units + unfulfilled_units. | integer | none |  |
| `fulfilled_units` | = -SUM(sale quantity_change) in inventory_movements for the product, warehouse and week. | integer | none |  |
| `unfulfilled_units` | Lost / cut demand; > 0 only where stock hit <= 0 or a shock event explains it. | integer | none |  |
| `promo_flag` | 1 if a planned promotion ran that week. | integer | none |  |
| `price_index` | Net price index (1.0 = Jan-2023 list price). | decimal | none |  |
| `demand_shock_event_id` | Disruption event explaining unusual demand or cuts. | text | 71,275 (100%) | FK -> disruption_events |

## `disruption_events`

Grain: One row per disruption or notable supply-chain event.  
Rows: 2,696

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `event_id` | Stable event key (EVT00001...), chronological. | text | none | PK |
| `event_type` | rm_supply_delay, rm_shortage, supplier_delivery_delay, port_congestion, ... | text | none |  |
| `title` | Short human title. | text | none |  |
| `start_date` | First day of impact. | date | none |  |
| `end_date` | Last day of impact. | date | none |  |
| `detected_at` | Date the organisation became aware. | date | none |  |
| `severity` | 1 (minor) to 5 (critical), from quantified impact. | integer | none |  |
| `root_cause_code` | Coded root cause. | text | none |  |
| `root_cause_text` | Root cause in words. | text | none |  |
| `origin_entity_type` | supplier, rm, plant, warehouse, product, country, category. | text | none |  |
| `origin_entity_id` | Id of the origin entity. | text | none |  |
| `anchor_type` | existing_data (mined from v1.0.0 rows) or rm_side (new RM rows). | text | none |  |
| `anchor_ids` | JSON array of record ids that evidence the event. | text (JSON) | none |  |

## `event_impacts`

Grain: One row per event, impacted entity and metric.  
Rows: 16,267

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `event_id` | Event. | text | none | PK, FK -> disruption_events |
| `entity_type` | Impacted entity type. | text | none | PK |
| `entity_id` | Impacted entity id. | text | none | PK |
| `impact_metric` | What was measured. | text | none | PK |
| `impact_value` | Computed value. | decimal | none |  |
| `unit` | Unit of impact_value. | text | none |  |

## `event_links`

Grain: One row per directed relationship between two events.  
Rows: 3,791

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `src_event_id` | Source event. | text | none | PK, FK -> disruption_events |
| `dst_event_id` | Target event. | text | none | PK, FK -> disruption_events |
| `link_type` | caused | similar_to | recurrence_of | mitigated_by | superseded_by. | text | none | PK |

## `negotiations`

Grain: One row per negotiation with a supplier.  
Rows: 140

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `negotiation_id` | Stable key (NEG00001...). | text | none | PK |
| `supplier_id` | Counterparty. | text | none | FK -> suppliers |
| `rm_id` | Raw material in scope (empty for supplier-wide topics). | text | 7 (5%) | FK -> raw_materials |
| `started_at` | First meeting. | date | none |  |
| `concluded_at` | Conclusion date; empty if still open. | date | 3 (2%) |  |
| `topic` | price_increase, annual_renewal, delivery_performance, capacity_reservation, allocation_change. | text | none |  |
| `our_ask` | Our opening position. | text | none |  |
| `their_offer` | Supplier's opening position. | text | none |  |
| `concessions_json` | Concessions exchanged (party, item, value). | text (JSON) | none |  |
| `final_terms` | Agreed terms (matches catalog / contract rows). | text | 3 (2%) |  |
| `outcome` | agreed | partial | failed | open. | text | none |  |
| `contract_id` | Resulting / governing contract. | text | 15 (11%) | FK -> contracts |

## `decisions`

Grain: One row per decision taken in response to an event.  
Rows: 402

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `decision_id` | Stable key (DEC00001...), chronological. | text | none | PK |
| `event_id` | Triggering event. | text | none | FK -> disruption_events |
| `decided_at` | Decision date. | date | none |  |
| `decided_by_role` | Role of the decision owner. | text | none |  |
| `decision_type` | expedite, switch_supplier, substitute_rm, reallocate_stock, cancel_po, build_safety_stock, renegotiate, accept_delay, reduce_allocation. | text | none |  |
| `options_considered_json` | Options with est_cost, est_stockout_days, risk as estimated at decided_at. | text (JSON) | none |  |
| `chosen_option` | Name of the chosen option. | text | none |  |
| `rationale_text` | Why it was chosen. | text | none |  |
| `expected_cost` | Estimated cost of the chosen option (USD). | decimal | none |  |
| `expected_stockout_days` | Estimated stockout days of the chosen option. | decimal | none |  |
| `actual_cost` | Cost computed from the executed rows (USD). | decimal | none |  |
| `actual_stockout_days` | Stockout days computed from the simulated / existing inventory trajectory. | decimal | none |  |
| `outcome_label` | success | partial | failed (computed from expected vs actual). | text | none |  |
| `outcome_assessed_at` | When the outcome was assessed. | date | none |  |
| `lesson_text` | Lesson recorded for future decisions. | text | none |  |
| `footprint_ids` | JSON array of rows that prove the decision was executed. | text (JSON) | none |  |
| `outcome_attribution` | as_expected | context_shift | bad_luck | decision_quality (extension column). | text | none |  |

## `commitments`

Grain: One row per commitment made by or to us.  
Rows: 924

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `commitment_id` | Stable key (CMT00001...). | text | none | PK |
| `decision_id` | Decision that produced the commitment. | text | 378 (41%) | FK -> decisions |
| `negotiation_id` | Negotiation that produced the commitment. | text | 897 (97%) | FK -> negotiations |
| `counterparty_type` | supplier | plant | warehouse | internal. | text | none |  |
| `counterparty_id` | Counterparty id. | text | none |  |
| `made_by_role` | Who made it (supplier or our role). | text | none |  |
| `made_at` | Date made. | date | none |  |
| `commitment_text` | What was promised. | text | none |  |
| `quantity` | Committed quantity (uom / units / USD per text). | decimal | 391 (42%) |  |
| `due_date` | When it is due. | date | none |  |
| `penalty_or_credit` | Agreed consequence if breached. | text | 296 (32%) |  |
| `status` | fulfilled | breached | renegotiated | open (computed from data as of 2025-12-31). | text | none |  |
| `resolved_at` | Date status became final. | date | 24 (3%) |  |
| `evidence_ids` | Rows used to compute status (extension column). | text (JSON) | none |  |

## `supplier_scorecard_monthly`

Grain: One row per supplier and month with POs due or commitments made.  
Rows: 7,974

| Column | What it means | Data type | Missing values | Key |
|---|---|---|---|---|
| `supplier_id` | Supplier. | text | none | PK, FK -> suppliers |
| `month` | First day of month (month of FG expected_at / RM promised_at). | date | none | PK |
| `po_count` | Non-cancelled FG + RM POs due in the month. | integer | none |  |
| `otif_rate` | Share of those POs received in full on or before the due date. | decimal | 3 (0%) |  |
| `avg_delay_days` | Mean max(0, received_at - due date) over received POs. | decimal | 65 (1%) |  |
| `fill_rate` | Received value / ordered value over their lines. | decimal | 3 (0%) |  |
| `quality_ppm` | RM receipt rejects per million received units; empty without RM receipts. | decimal | 5,277 (66%) |  |
| `commitments_made` | Supplier commitments made in the month. | integer | none |  |
| `commitments_kept` | Of those, status = fulfilled. | integer | none |  |

## Relationships

- `products_ext.product_id` -> `products.product_id` (many-to-one)
- `products_ext.plant_id` -> `plants.plant_id` (many-to-one)
- `bill_of_materials.product_id` -> `products.product_id` (many-to-one)
- `bill_of_materials.rm_id` -> `raw_materials.rm_id` (many-to-one)
- `contracts.supplier_id` -> `suppliers.supplier_id` (many-to-one)
- `rm_supplier_catalog.rm_id` -> `raw_materials.rm_id` (many-to-one)
- `rm_supplier_catalog.supplier_id` -> `suppliers.supplier_id` (many-to-one)
- `rm_supplier_catalog.contract_id` -> `contracts.contract_id` (many-to-one)
- `rm_inventory_opening_balances.rm_id` -> `raw_materials.rm_id` (many-to-one)
- `rm_inventory_opening_balances.plant_id` -> `plants.plant_id` (many-to-one)
- `rm_purchase_orders.supplier_id` -> `suppliers.supplier_id` (many-to-one)
- `rm_purchase_orders.plant_id` -> `plants.plant_id` (many-to-one)
- `rm_purchase_order_lines.rm_purchase_order_id` -> `rm_purchase_orders.rm_purchase_order_id` (many-to-one)
- `rm_purchase_order_lines.rm_id` -> `raw_materials.rm_id` (many-to-one)
- `rm_po_revisions.rm_po_id` -> `rm_purchase_orders.rm_purchase_order_id` (many-to-one)
- `rm_inventory_movements.rm_id` -> `raw_materials.rm_id` (many-to-one)
- `rm_inventory_movements.plant_id` -> `plants.plant_id` (many-to-one)
- `rm_inventory_movements.rm_purchase_order_line_id` -> `rm_purchase_order_lines.rm_purchase_order_line_id` (many-to-one)
- `rm_inventory_movements.production_run_id` -> `production_runs.production_run_id` (many-to-one)
- `rm_inventory_snapshots_weekly.rm_id` -> `raw_materials.rm_id` (many-to-one)
- `rm_inventory_snapshots_weekly.plant_id` -> `plants.plant_id` (many-to-one)
- `production_runs.plant_id` -> `plants.plant_id` (many-to-one)
- `production_runs.product_id` -> `products.product_id` (many-to-one)
- `production_runs.purchase_order_line_id` -> `purchase_order_lines.purchase_order_line_id` (many-to-one)
- `production_runs.rm_shortage_rm_id` -> `raw_materials.rm_id` (many-to-one)
- `product_demand_weekly.product_id` -> `products.product_id` (many-to-one)
- `product_demand_weekly.warehouse_id` -> `warehouses.warehouse_id` (many-to-one)
- `product_demand_weekly.demand_shock_event_id` -> `disruption_events.event_id` (many-to-one)
- `event_impacts.event_id` -> `disruption_events.event_id` (many-to-one)
- `event_links.src_event_id` -> `disruption_events.event_id` (many-to-one)
- `event_links.dst_event_id` -> `disruption_events.event_id` (many-to-one)
- `negotiations.supplier_id` -> `suppliers.supplier_id` (many-to-one)
- `negotiations.rm_id` -> `raw_materials.rm_id` (many-to-one)
- `negotiations.contract_id` -> `contracts.contract_id` (many-to-one)
- `decisions.event_id` -> `disruption_events.event_id` (many-to-one)
- `commitments.decision_id` -> `decisions.decision_id` (many-to-one)
- `commitments.negotiation_id` -> `negotiations.negotiation_id` (many-to-one)
- `supplier_scorecard_monthly.supplier_id` -> `suppliers.supplier_id` (many-to-one)
- `decisions.footprint_ids`, `disruption_events.anchor_ids`, `commitments.evidence_ids` -> JSON arrays of ids from any table (snapshot rows are referenced as `rm_id|plant_id|snapshot_date`).
- `memory_corpus.jsonl.source_record_ids` / `entity_ids` -> ids in any table; `eval_questions.jsonl.gold_doc_ids` -> `memory_corpus.doc_id`.
