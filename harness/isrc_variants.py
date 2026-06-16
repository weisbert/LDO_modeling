"""Registry of MOS current-source GROUND-TRUTH variants (the OBJECT we model).

Mirrors harness/variants.py for the LDO: each entry is a distinct transistor-
level current source the behavioral current-model method is tested against. The
set is DELIBERATELY diverse (polarity / output-Z / compliance / noise / temp
law / PSRR sign) so a behavioral fit cannot overfit one topology or operating
point -- the user's ">= 6, prevent overfitting" requirement.

Fields:
  subckt   : .subckt name in ground_truth/isrc_gt.lib
  pol      : 'sink' (NMOS, +I drawn from `out`) | 'source' (PMOS, I pushed into `out`)
  idc      : approximate target DC current [A] at the nominal compliance vc
  vc       : nominal compliance voltage forced on `out` [V]
  note     : which assumption / archetype this stresses
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
ISRC_LIB = ROOT / "ground_truth" / "isrc_gt.lib"

VDD = 1.05   # supply for the whole library (cards tuned for ~1.05 V)

VARIANTS = {
    "v1_nmos_simple":  dict(subckt="isrc_v1_nmos_simple",  pol="sink",   idc=1.8e-6, vc=0.5,
                            note="simple NMOS mirror: low-ish rout, early knee, ideal PSRR (IBP_POLY_1P8U archetype)"),
    "v2_nmos_cascode": dict(subckt="isrc_v2_nmos_cascode", pol="sink",   idc=1.0e-6, vc=0.5,
                            note="cascode NMOS: very high rout, higher compliance, ideal PSRR"),
    "v3_nmos_long":    dict(subckt="isrc_v3_nmos_long",    pol="sink",   idc=0.8e-6, vc=0.5,
                            note="long-channel (L=4u) simple NMOS: high rout + low knee + low noise"),
    "v4_pmos_simple":  dict(subckt="isrc_v4_pmos_simple",  pol="source", idc=1.0e-6, vc=0.5,
                            note="simple PMOS source: opposite polarity, knee near out->vdd"),
    "v5_pmos_cascode": dict(subckt="isrc_v5_pmos_cascode", pol="source", idc=2.0e-6, vc=0.5,
                            note="cascode PMOS source: high-rout sourcing branch"),
    "v6_ptat":         dict(subckt="isrc_v6_ptat",         pol="sink",   idc=1.5e-6, vc=0.5,
                            note="constant-gm beta-multiplier: ~PTAT (linear-in-T) (IBP_PTAT_TUNE archetype)"),
    "v7_nmos_rbias":   dict(subckt="isrc_v7_nmos_rbias",   pol="sink",   idc=1.2e-6, vc=0.5,
                            note="resistor-biased NMOS: strong dIout/dVdd (poor +sign PSRR), CTAT-leaning"),
    "v8_wilson":       dict(subckt="isrc_v8_wilson",       pol="sink",   idc=0.6e-6, vc=0.5,
                            note="Wilson mirror: feedback-boosted rout, distinct PSRR/noise"),
}

# The 3 real PMU contract pins map onto archetypes (for the eventual real fit):
REAL_PIN_ARCHETYPE = {
    "IBP_POLY_1P8U_VCO":      "v1_nmos_simple",
    "IBP_POLY_500N_VCO_Fit":  "v3_nmos_long",
    "IBP_PTAT_TUNE_1P5U_VCO": "v6_ptat",
}
