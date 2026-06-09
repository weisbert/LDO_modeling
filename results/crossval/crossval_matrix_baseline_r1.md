# Cross-validation matrix (out-of-sample guardrails)

| variant | loco | ident | offgrid | Zout_oos_max | PSRR_oos_max | Zout_cond_worst | switches | offgrid_Zout_max |
|---|---|---|---|---|---|---|---|---|
| base | FAIL | PASS | FAIL | 5.82 | 13.79 | 2.74e+12 | - | 0.73 |
| cout10n | FAIL | FAIL | FAIL | 4.00 | 15.13 | 2.65e+12 | noise:gn5 | 0.29 |
| cout4n7 | FAIL | FAIL | FAIL | 4.30 | 14.90 | 2.38e+12 | zout:R_pl;noise:gn2 | 0.31 |
| esr_hi | FAIL | PASS | FAIL | 5.63 | 13.85 | 2.92e+12 | - | 0.58 |
| iq_lo | FAIL | FAIL | FAIL | 5.15 | 12.47 | 2.24e+12 | zout:R_pl;noise:gn5 | 0.45 |
| iq_hi | FAIL | FAIL | FAIL | 5.69 | 16.38 | 2.45e+12 | noise:gnw,gn5 | 0.78 |
| wp_big | FAIL | FAIL | FAIL | 3.18 | 14.60 | 3.80e+12 | noise:gnw | 0.63 |
| cg_hi | FAIL | FAIL | FAIL | 4.98 | 12.31 | 2.21e+12 | zout:R_pl | 0.36 |
| v1_nmos | FAIL | FAIL | FAIL | 4.56 | 19.10 | 3.25e+12 | zout:R_pl;noise:gn5 | 2.11 |
| v2_capless | FAIL | FAIL | PASS | 6.53 | 19.86 | 2.57e+12 | psrr:G2,G3,pcb0,pcb1 | 1.16 |
| v3_miller | FAIL | FAIL | FAIL | 4.23 | 39.26 | 2.33e+08 | psrr:pcb1,pcw0;noise:gn5 | 0.64 |
| v4_ffpsrr | FAIL | FAIL | FAIL | 4.74 | 63.89 | 55.3 | psrr:pcb1;noise:gn6 | 0.31 |
| v5_spur | FAIL | PASS | FAIL | 5.82 | 13.79 | 2.74e+12 | - | 0.73 |
| v6_spur2 | FAIL | PASS | FAIL | 5.82 | 13.79 | 2.74e+12 | - | 0.73 |
