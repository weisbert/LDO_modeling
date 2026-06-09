# Cross-validation matrix (out-of-sample guardrails)

| variant | loco | ident | offgrid | Z_interp | P_interp | offgrid_Z | offgrid_P | Zout_cond_worst | switches(contained) |
|---|---|---|---|---|---|---|---|---|---|
| base | FAIL | PASS | FAIL | 1.44 | 5.60 | 0.73 | 4.07 | 2.74e+12 | - |
| base_ghz | FAIL | PASS | FAIL | 1.44 | 5.60 | 0.73 | 4.07 | 2.51e+12 | - |
| cout10n | FAIL | PASS | FAIL | 1.25 | 5.46 | 0.29 | 2.76 | 2.65e+12 | noise:gn5 |
| cout4n7 | FAIL | PASS | FAIL | 1.34 | 5.50 | 0.31 | 2.78 | 2.38e+12 | zout:R_pl;noise:gn2 |
| esr_hi | FAIL | PASS | FAIL | 1.54 | 5.59 | 0.58 | 3.99 | 2.92e+12 | - |
| iq_lo | FAIL | PASS | FAIL | 1.54 | 4.97 | 0.45 | 3.94 | 2.24e+12 | zout:R_pl;noise:gn5 |
| iq_hi | FAIL | PASS | FAIL | 1.27 | 6.21 | 0.78 | 1.83 | 2.45e+12 | noise:gnw,gn5 |
| wp_big | FAIL | PASS | FAIL | 0.68 | 4.25 | 0.63 | 8.97 | 3.80e+12 | noise:gnw |
| cg_hi | FAIL | PASS | FAIL | 1.61 | 4.76 | 0.36 | 2.03 | 2.21e+12 | zout:R_pl |
| v1_nmos | FAIL | PASS | FAIL | 2.14 | 4.52 | 2.11 | 4.44 | 3.25e+12 | zout:R_pl;noise:gn5 |
| v2_capless | FAIL | PASS | PASS | 1.41 | 5.82 | 1.16 | 0.70 | 2.57e+12 | psrr:G2,G3,pcb0,pcb1 |
| v3_miller | FAIL | PASS | FAIL | 1.31 | 11.11 | 0.64 | 11.14 | 2.33e+08 | psrr:pcb1,pcw0;noise:gn5 |
| v4_ffpsrr | FAIL | PASS | FAIL | 1.49 | 4.87 | 0.31 | 9.88 | 55.3 | psrr:pcb1;noise:gn6 |
| v5_spur | FAIL | PASS | FAIL | 1.44 | 5.60 | 0.73 | 4.07 | 2.74e+12 | - |
| v6_spur2 | FAIL | PASS | FAIL | 1.44 | 5.60 | 0.73 | 4.07 | 2.74e+12 | - |
| v7_esl | FAIL | PASS | FAIL | 1.48 | 5.59 | 0.80 | 4.09 | 2.50e+12 | - |
| v8_dlc | FAIL | PASS | FAIL | 1.64 | 5.56 | 1.00 | 2.88 | 2.58e+12 | noise:gn5 |
| v9_vldo | FAIL | PASS | FAIL | 1.49 | 4.07 | 0.10 | 1.91 | 2.03e+12 | noise:gn5 |
| v10_3lc | FAIL | PASS | PASS | 6.82 | 14.44 | 6.73 | 13.58 | 2.44e+12 | psrr:G2,G3,pcb0,pcb1,pcw0 |
