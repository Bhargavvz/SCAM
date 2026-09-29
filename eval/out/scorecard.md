# Scorecard

## Pattern detection (0/12 with Mental Models, 3/12 without)

| pattern | with Mental Models | without |
|---|---|---|
| P01 | None | True |
| P02 | None | False |
| P03 | None | True |
| P04 | None | False |
| P05 | None | False |
| P06 | None | False |
| P07 | None | False |
| P08 | None | False |
| P09 | None | True |
| P10 | None | False |
| P11 | None | False |
| P12 | None | False |

## Holdout scenarios (n=14, accuracy 1.0, trap accuracy 1.0 on 4, simulator parity 14/14, errors 0)

| scenario | gold | chosen | correct | trap | trap pass | precedent recall | commitments recall | latency s |
|---|---|---|---|---|---|---|---|---|
| HS01 | switch_supplier | switch_supplier | True | False | None | 1.0 | 1.0 | 20.19 |
| HS02 | reallocate_stock | reallocate_stock | True | True | True | 0.333 | 1.0 | 86.07 |
| HS03 | reallocate_stock | reallocate_stock | True | False | None | 1.0 | 1.0 | 59.96 |
| HS04 | switch_supplier | switch_supplier | True | False | None | 0.333 | None | 26.72 |
| HS05 | switch_supplier | switch_supplier | True | True | True | 1.0 | 1.0 | 86.16 |
| HS06 | reallocate_stock | reallocate_stock | True | False | None | 1.0 | 1.0 | 63.94 |
| HS07 | reallocate_stock | reallocate_stock | True | False | None | 1.0 | 1.0 | 99.42 |
| HS08 | switch_supplier | switch_supplier | True | True | True | 1.0 | 1.0 | 67.04 |
| HS09 | reallocate_stock | reallocate_stock | True | False | None | 0.0 | 1.0 | 25.23 |
| HS10 | switch_supplier | switch_supplier | True | True | True | 0.667 | 1.0 | 88.08 |
| HS11 | reallocate_stock | reallocate_stock | True | False | None | 1.0 | 1.0 | 23.02 |
| HS12 | reallocate_stock | reallocate_stock | True | False | None | 0.333 | 1.0 | 80.49 |
| HS13 | switch_supplier | switch_supplier | True | False | None | 0.333 | 1.0 | 20.98 |
| HS14 | reallocate_stock | reallocate_stock | True | False | None | 0.667 | 1.0 | 86.47 |

Latency {'mean_s': 59.555, 'p50_s': 65.49, 'max_s': 99.42} | tokens {'mean_input': 5739.214, 'mean_output': 1307.357} | cost {'mean_usd': None, 'total_usd': None}
