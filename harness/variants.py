"""Registry of LDO variants for the GENERALIZATION experiment.

Each variant is a distinct DUT the modeling method is tested against. Two layers:

  A-layer (same ldo_gt topology, swept operating regime) -- tests the
           OP-parameterization + the Cout/ESR auto-extraction. Just subckt params.
  B-layer (new transistor-level architectures) -- the real generalization test,
           each stressing one structural assumption of the fit topology.

A variant carries:
  libs     : list of .lib files to inline (subckt definitions)
  subckt   : subckt name to instantiate as the DUT
  xparams  : extra subckt params passed at instantiation (A-layer sweeps)
  biasnode : hierarchical bias-mirror node for IBP->Vout injection (None = skip)
  cout,esr : the TRUE physical cap (for the self-consistency check vs auto-extract)
  note     : which assumption this variant stresses

`base` reproduces Target A exactly.
"""
import ng

GT = ng.LDO_LIB
GROUND = ng.ROOT / "ground_truth"

VARIANTS = {
    # ---- reference (Target A) -------------------------------------------------
    "base": dict(libs=[GT], subckt="ldo_gt", xparams="", biasnode="nb",
                 cout=1e-9, esr=0.5, note="Target-A PMOS-pass / 5T-OTA reference"),

    # GHz-carrier coverage demo (B-cover): same ideal-cap ldo_gt, but characterized to a 10 GHz
    # *_hf ceiling (vs the 500 MHz default) so the system test's validity envelope opens up to a
    # ~6 GHz carrier (brackets the real Target-B ~5.8 GHz). Proves the recipe is GENERAL -- one
    # profile number (hf_stop) carries the whole characterize->fit->emit->envelope->coherent-FFT
    # pipeline to GHz with NO code edits. ldo_gt's Zout rolls off smoothly to its ESR floor through
    # 10 GHz (exploratory sweep: no inductive/ESL rise), so the lumped model extrapolates correctly.
    # (This validates the PLUMBING at GHz; real-silicon GHz physics -- ESL/distributed -- is Target B,
    # guarded by the same exploratory sweep.)
    "base_ghz": dict(libs=[GT], subckt="ldo_gt", xparams="", biasnode="nb",
                     cout=1e-9, esr=0.5, hf_stop=10e9,
                     note="GHz-carrier demo: ldo_gt characterized to 10GHz (B-cover validity envelope at GHz)"),

    # ---- A-layer: same topology, swept operating regime ----------------------
    "cout10n": dict(libs=[GT], subckt="ldo_gt", xparams="cout=10n resr=1", biasnode="nb",
                    cout=10e-9, esr=1.0, note="10x output cap + higher ESR (Cout autoextract)"),
    "cout4n7": dict(libs=[GT], subckt="ldo_gt", xparams="cout=4.7n resr=0.8", biasnode="nb",
                    cout=4.7e-9, esr=0.8, note="mid output cap"),
    "esr_hi": dict(libs=[GT], subckt="ldo_gt", xparams="resr=3", biasnode="nb",
                   cout=1e-9, esr=3.0, note="high ESR -> ESR zero moves into band"),
    "iq_lo": dict(libs=[GT], subckt="ldo_gt", xparams="ib=4u", biasnode="nb",
                  cout=1e-9, esr=0.5, note="low quiescent current (lower UGB/gm)"),
    "iq_hi": dict(libs=[GT], subckt="ldo_gt", xparams="ib=20u", biasnode="nb",
                  cout=1e-9, esr=0.5, note="high quiescent current (higher UGB/gm)"),
    "wp_big": dict(libs=[GT], subckt="ldo_gt", xparams="wp=60u", biasnode="nb",
                   cout=1e-9, esr=0.5, note="2x pass-device width (gm, Zout LF)"),
    "cg_hi": dict(libs=[GT], subckt="ldo_gt", xparams="cg=4p", biasnode="nb",
                  cout=1e-9, esr=0.5, note="higher gate cap -> lower-Q / damped resonance"),

    # ---- B-layer: new architectures (added as each is brought up) -------------
    "v1_nmos": dict(libs=[GROUND / "ldo_v1_nmos.lib"], subckt="ldo_v1_nmos", xparams="",
                    biasnode="nb", cout=1e-9, esr=30.0,
                    note="NMOS-pass source-follower: low flat Zout, high PSRR, ~no peak (ESR-damped)"),
    "v2_capless": dict(libs=[GROUND / "ldo_v2_capless.lib"], subckt="ldo_v2_capless", xparams="",
                       biasnode="nb", cout=100e-12, esr=120.0,
                       note="cap-less small-Cout: resonance pushed to ~4-8MHz spur band (ESR-zero damped)"),
    "v3_miller": dict(libs=[GROUND / "ldo_v3_miller.lib"], subckt="ldo_v3_miller", xparams="",
                      biasnode="nb", cout=1e-9, esr=0.5,
                      note="two-stage Miller + nulling-R: multi-pole Zout (resonance migrates w/ load)"),
    "v4_ffpsrr": dict(libs=[GROUND / "ldo_v4_ffpsrr.lib"], subckt="ldo_v4_ffpsrr", xparams="",
                      biasnode="nb", cout=1e-9, esr=5.0,
                      note="feedforward onto fb node: non-minimum-phase PSRR (notch + RHP zero)"),

    # ---- spur-block validation DUTs (Part B): GT + deterministic on-chip spurs ----
    "v5_spur": dict(libs=[GROUND / "ldo_v5_spur.lib"], subckt="ldo_v5_spur", xparams="",
                    biasnode="nb", cout=1e-9, esr=0.5,
                    note="intrinsic spurs via 3 paths: ref 1.0MHz / charge-pump 2.5MHz / clock 4.0MHz"),
    "v6_spur2": dict(libs=[GROUND / "ldo_v6_spur2.lib"], subckt="ldo_v6_spur2", xparams="",
                     biasnode="nb", cout=1e-9, esr=0.5,
                     note="two INCOMMENSURATE intrinsic tones (1.0MHz + 3.7MHz) -> multi-tone HB stress"),

    # ---- B-layer round 2: 4 new architectures stressing untested assumptions ----
    # (additive generalization probes; a poor fit IS the finding -- do not touch shared fit/score code)
    "v7_esl": dict(libs=[GROUND / "ldo_v7_esl.lib"], subckt="ldo_v7_esl", xparams="",
                   biasnode="nb", cout=1e-9, esr=0.5, hf_stop=2e9,
                   note="output-cap series ESL: Zout dips at SRF then RISES inductively (no series-L term in model)"),
    "v8_dlc": dict(libs=[GROUND / "ldo_v8_dlc.lib"], subckt="ldo_v8_dlc", xparams="",
                   biasnode="nb", cout=4.7e-9, esr=0.5, hf_stop=1e9,
                   note="double-LC pi output net: 2 resonances + an anti-resonance NOTCH (parallel-RLC can't dip)"),
    "v9_vldo": dict(libs=[GROUND / "ldo_v9_vldo.lib"], subckt="ldo_v9_vldo", xparams="",
                    biasnode="nb", cout=1e-9, esr=0.5,
                    note="very-low-dropout (~50mV): small-signal fits, large steps hit dropout -> validity envelope narrows"),
    "v10_3lc": dict(libs=[GROUND / "ldo_v10_3lc.lib"], subckt="ldo_v10_3lc", xparams="",
                    biasnode="nb", cout=200e-12, esr=0.1, hf_stop=1e9,
                    note="multi-stage PDN (3-cap ladder): >2 Zout resonances -> exceeds the 2-branch RLC order"),

    # ---- ADVERSARIAL OVERFIT PROBES (round 3): engineered to drive the load-interp overfit locus +
    # the LTI/composite blind spots to FAILURE (HANDOFF_ADVERSARIAL_OVERFIT_PROBE.md §A). A poor fit
    # (or held-out crossval miss) IS the finding; registered additively (the 14 baselines untouched).
    "qbow": dict(libs=[GROUND / "ldo_qbow.lib"], subckt="ldo_qbow", xparams="",
                 biasnode="nb", cout=1e-9, esr=0.5,
                 note="A1: NON-monotonic Zout resonance-Q vs load (Q 1.5/16.5/11.1) -> the 3-pt quad-in-ln(iload) "
                      "Zout interp can't track the mid-corner bow (LOCO/offgrid Zout)"),
    "pzmig": dict(libs=[GROUND / "ldo_pzmig.lib"], subckt="ldo_pzmig", xparams="",
                  biasnode="nb", cout=12e-9, esr=0.01,
                  note="A2: PSRR corner migrates NON-log-linearly with load (convex, ln-mid dev +0.535) -> "
                       "the load-interp PSRR fitter (the proven weak axis) overshoots the interior"),
    "swbleed": dict(libs=[GROUND / "ldo_swbleed.lib"], subckt="ldo_swbleed", xparams="",
                    biasnode="nb", cout=1e-9, esr=0.5,
                    note="A3: load-threshold MODE SWITCH -- a DC-blocked LC damper notch (150kHz) appears ONLY "
                         "at 250u -> the Zout fitter selects branch-B only at one corner (structloco flip)"),
    "classab": dict(libs=[GROUND / "ldo_classab.lib"], subckt="ldo_classab", xparams="",
                    biasnode="nb", cout=2e-9, esr=15.0,
                    note="A4: class-AB push-pull, swing-dependent gm, NO dropout -> small-signal fits base-like "
                         "(fools composite) but large steps are asymmetric (LTI-foundation breaker)"),
}


def get(key):
    if key not in VARIANTS:
        raise KeyError(f"unknown variant '{key}'. known: {list(VARIANTS)}")
    return VARIANTS[key]
