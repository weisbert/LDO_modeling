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
}


def get(key):
    if key not in VARIANTS:
        raise KeyError(f"unknown variant '{key}'. known: {list(VARIANTS)}")
    return VARIANTS[key]
