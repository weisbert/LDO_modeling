"""Guard the LARGE-SIGNAL current VA emit (emit_pmu_model._current_block_largesignal),
fed by the offline-validated behavioral fit (fit_isrc -> current_crow_from_isrc_fit).

Offline we can't compile/sim the .va (that's Spectre on the box), so we (1) check the
emitted VA is well-formed and carries every large-signal term, (2) confirm legacy
small-signal crows still emit the old form (backward compat), and (3) numerically
evaluate the SAME math the VA emits and confirm it reproduces the MOS-GT (the on-box
Spectre run must match this reference). Needs work_isrc/*.npz (run isrc_char.py first).
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fit_isrc import fit_isrc                                          # noqa: E402
from isrc_variants import REAL_PIN_ARCHETYPE                           # noqa: E402
from emit_pmu_model import (emit_pmu_va, va_sanity,                    # noqa: E402
                            current_crow_from_isrc_fit, _current_block)

WORK = HERE.parent / "work_isrc"
VDC = 1.05


def _emit(tmp_path, arch_pins):
    crows = [current_crow_from_isrc_fit(fit_isrc(WORK / f"{a}.npz"), pin=pin)
             for pin, a in arch_pins.items()]
    out = tmp_path / "PMU_isrc.va"
    emit_pmu_va(dict(voltage={}, current=crows, meta={}), "PMU_isrc", str(out),
                supply="AVDD1P0", ground="VSS", supply_dc=VDC)   # match the char supply
    return out.read_text(), list(arch_pins.keys())


def test_largesignal_terms_and_sanity(tmp_path):
    txt, i_outs = _emit(tmp_path, REAL_PIN_ARCHETYPE)
    ok, probs = va_sanity(txt, "AVDD1P0", [], i_outs, "VSS")
    assert ok, probs
    for tok in ("tanh(pow(", "$temperature - 328.15", "white_noise(", "flicker_noise(",
                "IBP_PTAT_TUNE_1P5U_VCO_idc55", "IBP_PTAT_TUNE_1P5U_VCO_kp"):
        assert tok in txt, f"missing large-signal term: {tok}"


def test_source_drives_from_supply(tmp_path):
    # force a source archetype onto a pin and confirm the VA injects supply->pin
    p = fit_isrc(WORK / "v4_pmos_simple.npz")
    crow = current_crow_from_isrc_fit(p, pin="ISRC_SRC")
    blk = _current_block("ISRC_SRC", crow, "AVDD1P0", "VSS")
    assert "I(AVDD1P0, ISRC_SRC) <+" in blk["body"], "source must drive supply->pin"


def test_backward_compat_legacy():
    legacy = dict(sink="i1", il="nom", g0=1e-7, Cp=1e-15,
                  pi={"AVDD1P0": {"rms": 1e-12, "dc": 2e-7}})
    blk = _current_block("i1", legacy, "AVDD1P0", "VSS")
    assert "i1_pidc = 2.000000e-07;" in blk["asg"]           # legacy |PI(0)| path
    assert "i1_idc55" not in "".join(blk["rvars"])           # NOT the large-signal path


def _ls_crow(pin="IB_Q", d2=0.0):
    """A minimal large-signal crow (carries idc55 -> the large-signal VA block)."""
    return dict(sink=pin, pin=pin, pol="sink", idc55=2.0e-4, didt=1e-7, d2=d2,
                g0=1e-7, vc=0.4, gdd=2e-7, vknee=0.05, knee_p=1.0, knee_side="lo",
                vhi=1.05, Cp=1e-15, in_white=1e-12, in_kf=1e-24, tnom_c=55.0)


def test_emit_d2_absent_when_zero():
    """d2==0.0 -> NO _d2 rvar/asg/term and NO '+ 0.0*' tail: the .va is substring-identical
    to the pre-curvature emit (the linear temp law '$temperature - 328.15' survives)."""
    blk = _current_block("IB_Q", _ls_crow(d2=0.0), "AVDD1P0", "VSS")
    whole = blk["body"] + blk["asg"] + "".join(blk["rvars"])
    assert "IB_Q_d2" not in whole
    assert "+ 0.0*" not in blk["body"]
    assert "$temperature - 328.15" in blk["body"]            # linear law substring preserved


def test_emit_d2_present_when_nonzero():
    """d2!=0.0 -> the curvature rvar/asg + the squared Kelvin term are emitted, and the term
    measurably shifts the modeled current off the nominal temp (vs the d2=0 twin)."""
    d2 = 3e-9
    crow = _ls_crow(d2=d2)
    blk = _current_block("IB_Q", crow, "AVDD1P0", "VSS")
    assert f"IB_Q_d2 = {d2:.6e};" in blk["asg"]
    assert "IB_Q_d2" in "".join(blk["rvars"])
    assert "IB_Q_d2*($temperature - 328.15)*($temperature - 328.15)" in blk["body"]
    # curvature changes the modeled current away from TNOM (mirror agrees with predict_idcT)
    curved = _va_eval_Ipin(crow, crow["vc"], VDC, 125.0)
    linear = _va_eval_Ipin(_ls_crow(d2=0.0), crow["vc"], VDC, 125.0)
    assert curved != linear
    from fit_isrc import predict_idcT
    dT = (125.0 + 273.15) - 328.15
    assert abs((crow["idc55"] + crow["didt"]*dT + crow["d2"]*dT*dT)
               - predict_idcT(crow, 125.0)) < 1e-18


def _va_eval_Ipin(crow, Vo, Vsup, Tc):
    """Mirror of the emitted VA current math (Kelvin temp, sqrt-floored gate, gdd sign)."""
    pol = crow["pol"]
    dT = (Tc + 273.15) - 328.15
    idcT = crow["idc55"] + crow["didt"] * dT + crow.get("d2", 0.0) * dT * dT
    gdd_eff = -crow["gdd"] if pol == "sink" else crow["gdd"]
    core = idcT + crow["g0"] * (Vo - crow["vc"]) + gdd_eff * (Vsup - VDC)
    karg = np.sqrt(Vo * Vo + 1e-12) if pol == "sink" else np.sqrt((VDC - Vo) ** 2 + 1e-12)
    gate = np.tanh((karg / crow["vknee"]) ** crow["knee_p"])
    return core * gate


def test_va_math_reproduces_gt():
    """The math the VA emits must reproduce each MOS-GT (on-box Spectre must match this)."""
    bad = []
    for name in ("v1_nmos_simple", "v4_pmos_simple", "v6_ptat", "v8_wilson"):
        d = np.load(WORK / f"{name}.npz", allow_pickle=True)
        crow = current_crow_from_isrc_fit(fit_isrc(WORK / f"{name}.npz"))
        Vo = np.asarray(d["iv_v"]); gt = np.asarray(d["iv_i"])
        m = np.abs(_va_eval_Ipin(crow, Vo, VDC, 55.0))
        on = gt > 0.5 * gt.max()
        rms = float(np.sqrt(np.mean(((m[on] - gt[on]) / gt[on]) ** 2)))
        idc_err = abs(np.interp(crow["vc"], Vo, m) - float(d["idc"])) / float(d["idc"])
        if rms > 0.05 or idc_err > 0.02:
            bad.append((name, rms, idc_err))
    assert not bad, f"VA math off vs GT: {bad}"


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_largesignal_terms_and_sanity(pathlib.Path(td))
        test_source_drives_from_supply(pathlib.Path(td))
    test_backward_compat_legacy()
    test_va_math_reproduces_gt()
    print("large-signal current VA emit: terms + sanity + source-drive + legacy + "
          "GT-math all OK")
