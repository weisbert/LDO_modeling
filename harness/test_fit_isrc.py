"""Regression guard for the BEHAVIORAL current-source fit+emit: the single model
template (fit_isrc -> emit_isrc) must reproduce ALL >=8 diverse MOS-transistor
archetypes in-simulator (anti-overfit). Re-runs the model-vs-GT cross-validation
and asserts every archetype passes + the I-V/noise fits are good. Needs ngspice +
work_isrc/*.npz (run harness/isrc_char.py first). `python -m pytest test_fit_isrc.py -q`.
"""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from isrc_variants import VARIANTS, BASELINE_VARIANTS                  # noqa: E402
from fit_isrc import fit_isrc                                          # noqa: E402
import crossval_isrc as xv                                            # noqa: E402

WORK = HERE.parent / "work_isrc"


def test_iv_and_noise_fit_quality():
    # the anti-overfit guard covers the 8 BASELINE archetypes; the round-3 adversarial probes are
    # DESIGNED to fit poorly (the finding) -> excluded here (see test_adv_probe_gates.py for them).
    for name in BASELINE_VARIANTS:
        p = fit_isrc(WORK / f"{name}.npz")
        assert p["iv_r2"] > 0.90, f"{name}: I-V fit R2={p['iv_r2']:.3f} too low"
        assert p["in_r2"] > 0.80, f"{name}: noise fit R2={p['in_r2']:.3f} too low"


def test_template_reproduces_all_archetypes():
    rows = [xv.crossval(n) for n in BASELINE_VARIANTS]      # the 8 anti-overfit archetypes only
    bad = [r["name"] for r in rows if not r["ok"]]
    assert not bad, f"behavioral template failed to reproduce: {bad}"
    assert len(rows) >= 6


# --------------------------------------------------------------------------------------------
# 2nd-order Idc(T) temperature fit (pure numpy -- no ngspice / no work_isrc needed)
# --------------------------------------------------------------------------------------------
import numpy as np                                                    # noqa: E402
import fit_isrc as FI                                                 # noqa: E402


def _lin_idcT(temps, idc55=200e-6, didt=3e-7):
    return np.array([idc55 + didt * (T - FI.TNOM) for T in temps], float)


def test_temp_backward_compat_3pt_linear():
    """<=3 temps -> d2==0.0 AND idc55/didt byte-equal a standalone deg-1 raw-Tk polyfit
    (the model is identical to the pre-2nd-order code)."""
    temps = np.array([-40.0, 55.0, 125.0])
    idcT = _lin_idcT(temps)
    tp = FI._fit_temp(temps, idcT)
    assert tp["d2"] == 0.0
    Tk = temps + 273.15
    b, a = np.polyfit(Tk, idcT, 1)
    assert tp["idc55"] == a + b * (FI.TNOM + 273.15)   # byte-identical computation
    assert tp["didt"] == b


def test_temp_4pt_still_linear():
    """The gate is >=5 UNIQUE pts: 4 points NEVER engage the quadratic (thin DOF)."""
    temps = np.array([-40.0, 0.0, 55.0, 125.0])
    idcT = _lin_idcT(temps) + 5e-6 * (temps - 55.0) ** 2   # real curvature, but only 4 pts
    assert FI._fit_temp(temps, idcT)["d2"] == 0.0


def test_temp_quadratic_recovers_curvature():
    """>=5 temps from a genuine quadratic -> d2 ~= the true c2 and predict_idcT recovers
    the curve to <1% RMS."""
    temps = np.array([-40.0, -10.0, 20.0, 55.0, 90.0, 125.0])
    c2_true = 4e-9
    idcT = _lin_idcT(temps) + c2_true * (temps - FI.TNOM) ** 2
    tp = FI._fit_temp(temps, idcT)
    assert tp["d2"] != 0.0
    assert abs(tp["d2"] - c2_true) < 0.05 * c2_true
    pred = FI.predict_idcT(tp, temps)
    rms = float(np.sqrt(np.mean(((pred - idcT) / idcT) ** 2)))
    assert rms < 0.01, f"quadratic recovery RMS {rms:.4f}"


def test_temp_keepbest_rejects_linear_data():
    """>=5 temps from a PURELY LINEAR law (tiny noise) -> the quadratic loses the 10% SSE
    margin -> d2==0.0 (no spurious curvature term on real PTAT/CTAT)."""
    temps = np.array([-40.0, -10.0, 20.0, 55.0, 90.0, 125.0])
    rng = np.random.default_rng(0)
    idcT = _lin_idcT(temps) + 1e-12 * rng.standard_normal(temps.size)
    assert FI._fit_temp(temps, idcT)["d2"] == 0.0


def test_temp_duplicate_temps_do_not_satisfy_gate():
    """5 rows but only 4 UNIQUE temps -> gate uses np.unique -> stays linear (d2==0.0)."""
    temps = np.array([-40.0, 20.0, 55.0, 125.0, 125.0])
    idcT = _lin_idcT(temps) + 5e-6 * (temps - 55.0) ** 2
    assert FI._fit_temp(temps, idcT)["d2"] == 0.0


def test_temp_constant_idc_is_safe():
    """All-equal Idc (sse_lin==0) must not divide-by-zero or engage a quadratic."""
    temps = np.array([-40.0, -10.0, 20.0, 55.0, 90.0, 125.0])
    tp = FI._fit_temp(temps, np.full(temps.size, 200e-6))
    assert tp["d2"] == 0.0 and abs(tp["idc55"] - 200e-6) < 1e-12


def test_predict_idcT_legacy_dict_without_d2():
    """A legacy param dict (no 'd2' key) predicts the exact linear law -- p.get default."""
    p = dict(idc55=200e-6, didt=3e-7)
    assert FI.predict_idcT(p, 125.0) == 200e-6 + 3e-7 * (125.0 - FI.TNOM)


if __name__ == "__main__":
    test_iv_and_noise_fit_quality()
    test_template_reproduces_all_archetypes()
    print(f"behavioral fit: I-V + noise quality OK and {len(VARIANTS)}/{len(VARIANTS)} "
          "archetypes reproduced in-simulator")
