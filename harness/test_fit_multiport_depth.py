"""STAGE 2b -- modeling-depth tests for harness/fit_multiport.py.

Two new behaviors, both GATED so a single-OP / legacy npz stays byte-identical:

  1) REAL per-load iload mapping: P[il]['iv'] is the rail's real current read from
     meta_iload_<o> (non-numeric labels like 'L0' now carry a real abscissa), and
     fit['schedule_loads'] + meta['schedule_loads'] expose the emit-side ln(iload) set.
  2) BIAS-CURRENT LARGE-SIGNAL CORE: a sink that carries a real I-V sweep (iv_<c>_<label>)
     gets ONE large-signal row (idc55/didt/g0/vc/gdd/vknee/knee_p/Cp/pol/tnom_c) so the
     emit dispatches to _current_block_largesignal; the SIGN of current-PSRR is preserved
     (gdd = dI/dVsup = -pi). A sink WITHOUT an I-V sweep keeps the legacy AC-only row.

Backward-compat: a coverage-free npz (no meta_iload_<o>, no iv_<c>_<label>) reproduces the
pre-stage-2b shape exactly (iv falls back to numeric-label parse then 0.0; legacy rows).

No simulator: pure-Python producer + structural assertions.
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_multiport as FMP   # noqa: E402
import emit_pmu_model as D    # noqa: E402


# ---------------------------------------------------------------- producers
def _ac(R=0.05, g=1e-3):
    f = np.logspace(1, 8, 40)
    return (np.c_[f, R + 0 * f, 1e-9 * f],          # z
            np.c_[f, g + 0 * f, 0 * f],             # p
            np.c_[f, 1e-9 + 0 * f])                 # noise


def _sweep_npz(tmp_path, *, with_iv=True, with_temp=False, name="sweep"):
    """A multi-load (x temp) npz shaped like pmu_corner's sweep output: per-load z/p/noise
    under swept labels + a once-cell 'Lnom_T..' carrying the sink I-V, plus meta_iload_<o>
    (real per-LABEL rail current, NaN on the once-cell) and meta_temp."""
    temps = [-40.0, 55.0, 125.0] if with_temp else [55.0]
    loadcur = {"L0": 50e-6, "L1": 580e-6, "L2": 2000e-6}
    rec, labels, mi_pll, mtemp = {}, [], [], []
    for T in temps:
        once = f"Lnom_T{T:g}"
        labels.append(once); mi_pll.append(float("nan")); mtemp.append(T)
        # the sink I-V sweep lives on the once-cell (one per temp); Idc has a small PTAT slope
        Vo = np.linspace(0.0, 0.8, 40)
        idc = 200e-6 * (1.0 + 0.001 * (T - 55.0))
        I = (idc + 1e-7 * (Vo - 0.4)) * np.tanh((Vo / 0.05) ** 1.0)
        rec[f"iv_i500n_{once}"] = np.c_[Vo, I]
        for lbl, cur in loadcur.items():
            full = f"{lbl}_T{T:g}"
            labels.append(full); mi_pll.append(cur); mtemp.append(T)
            z, p, n = _ac()
            rec[f"z_pll_{full}"] = z
            rec[f"p_pll_AVDD1P0_{full}"] = p
            rec[f"noise_pll_{full}"] = n
            rec[f"y_i500n_{full}"] = np.c_[z[:, 0], 1e-7 + 0 * z[:, 0],
                                           1e-15 * 2 * np.pi * z[:, 0]]
            # pi = -I/Vsup (SIGN carried by importmp). A negative pi => dI/dVsup positive.
            rec[f"pi_i500n_AVDD1P0_{full}"] = np.c_[z[:, 0], -2e-7 + 0 * z[:, 0], 0 * z[:, 0]]
    if not with_iv:
        for k in list(rec):
            if k.startswith("iv_i500n_"):
                del rec[k]
    rec["loads"] = np.array(labels)
    rec["meta_iload_pll"] = np.array(mi_pll, dtype=float)
    rec["meta_temp"] = np.array(mtemp, dtype=float)
    p = tmp_path / f"{name}.npz"
    np.savez(p, **rec)
    m = {"name": name, "supplies": {"AVDD1P0": {"dc": 1.05, "net": "VDD1P0"}},
         "current_psrr_supplies": ["AVDD1P0"],
         "v_out": {"pll": {"net": "VPLL", "iload": 500e-6, "vout_dc": 0.8}},
         "i_out": {"i500n": {"net": "IB", "dc": 0.4, "probe_src": "Vp"}},
         "coverage": {"temps": temps if with_temp else []}}
    return p, m


# =============================================== 1) real per-load iload mapping
def test_iload_map_real_currents(tmp_path):
    """P[il]['iv'] is the REAL rail current from meta_iload_<o>, not a numeric-label parse."""
    npz, m = _sweep_npz(tmp_path)
    res = FMP.fit_multiport(str(npz), m)
    P = res["voltage"]["pll"]["P"]
    # the once-cell (no z_pll) is NOT a voltage-fit corner; the 3 swept labels are
    assert set(P) == {"L0_T55", "L1_T55", "L2_T55"}
    assert P["L0_T55"]["iv"] == 50e-6
    assert P["L1_T55"]["iv"] == 580e-6
    assert P["L2_T55"]["iv"] == 2000e-6
    # vreg uses the REAL iv (vout_dc + R_a*iv), not 0
    assert P["L2_T55"]["vreg"] != P["L0_T55"]["vreg"]


def test_schedule_loads_exposed(tmp_path):
    """fit['schedule_loads'] + meta['schedule_loads'] expose the emit-side abscissa set:
    only labels with a real, finite, nonzero iv."""
    npz, m = _sweep_npz(tmp_path)
    res = FMP.fit_multiport(str(npz), m)
    sl = res["voltage"]["pll"]["schedule_loads"]
    assert sl == ["L0_T55", "L1_T55", "L2_T55"]
    sm = res["meta"]["schedule_loads"]["pll"]
    assert sm["labels"] == sl
    assert sm["currents"] == [50e-6, 580e-6, 2000e-6]


def test_legacy_npz_iload_unchanged(tmp_path):
    """A legacy npz with NO meta_iload_<o> and NON-numeric labels -> iv falls back to 0.0
    exactly as before (single-OP baked-literal path)."""
    f = np.logspace(1, 8, 40)
    z, p, n = _ac()
    rec = {"loads": np.array(["nom"]),
           "z_pll_nom": z, "p_pll_AVDD1P0_nom": p, "noise_pll_nom": n}
    npz = tmp_path / "legacy.npz"
    np.savez(npz, **rec)
    m = {"name": "legacy", "supplies": {"AVDD1P0": {"dc": 1.0, "net": "V"}},
         "current_psrr_supplies": ["AVDD1P0"],
         "v_out": {"pll": {"net": "VP", "iload": 500e-6, "vout_dc": 0.8}}, "i_out": {}}
    res = FMP.fit_multiport(str(npz), m)
    P = res["voltage"]["pll"]["P"]
    assert set(P) == {"nom"}
    assert P["nom"]["iv"] == 0.0                       # non-numeric label, no meta -> 0.0
    assert res["voltage"]["pll"]["schedule_loads"] == []   # nothing schedulable


def test_numeric_legacy_label_still_parses(tmp_path):
    """A legacy NUMERIC-label npz (no meta_iload) still parses iv via ng.amps (the older
    contract path) -> backward-compat for numeric corner keys."""
    z, p, n = _ac()
    rec = {"loads": np.array(["500u"]),
           "z_pll_500u": z, "p_pll_AVDD1P0_500u": p, "noise_pll_500u": n}
    npz = tmp_path / "num.npz"
    np.savez(npz, **rec)
    m = {"name": "num", "supplies": {"AVDD1P0": {"dc": 1.0, "net": "V"}},
         "current_psrr_supplies": ["AVDD1P0"],
         "v_out": {"pll": {"net": "VP", "iload": 500e-6, "vout_dc": 0.8}}, "i_out": {}}
    res = FMP.fit_multiport(str(npz), m)
    assert res["voltage"]["pll"]["P"]["500u"]["iv"] == 500e-6


# =============================================== 2) bias-current large-signal core
def test_current_largesignal_row(tmp_path):
    """A sink with a real I-V sweep -> ONE large-signal row carrying the full core; the
    emit dispatches to _current_block_largesignal."""
    npz, m = _sweep_npz(tmp_path)
    res = FMP.fit_multiport(str(npz), m)
    rows = [r for r in res["current"] if r["sink"] == "i500n"]
    assert len(rows) == 1                              # one large-signal row per sink
    r = rows[0]
    for k in ("idc55", "didt", "g0", "vc", "gdd", "vknee", "knee_p",
              "Cp", "in_white", "in_kf", "pol", "tnom_c"):
        assert k in r, f"large-signal row missing {k}"
    assert abs(r["idc55"] - 200e-6) < 5e-6             # the I-V OP current
    assert r["vc"] == 0.4 and r["pol"] == "sink" and r["tnom_c"] == 55.0
    assert r["in_white"] == 0.0 and r["in_kf"] == 0.0  # sink noise not in the matrix


def test_current_psrr_sign_preserved(tmp_path):
    """gdd = dI/dVsup = -pi (pi carries the importmp sign). pi=-2e-7 -> gdd=+2e-7."""
    npz, m = _sweep_npz(tmp_path)
    res = FMP.fit_multiport(str(npz), m)
    r = next(r for r in res["current"] if r["sink"] == "i500n")
    assert abs(r["gdd"] - 2e-7) < 1e-9                 # SIGN kept (+, not |.|)


def test_current_largesignal_temp_didt(tmp_path):
    """Multi-temp I-V sweeps -> _fit_temp gives a nonzero didt + idc55 at the nominal temp."""
    npz, m = _sweep_npz(tmp_path, with_temp=True)
    res = FMP.fit_multiport(str(npz), m)
    r = next(r for r in res["current"] if r["sink"] == "i500n")
    assert r["didt"] != 0.0                            # PTAT slope across -40/55/125
    assert abs(r["idc55"] - 200e-6) < 5e-6             # referenced to 55 C
    assert res["meta"]["tnom_c"] == 55.0               # middle of the temps


def test_no_iv_keeps_legacy_current(tmp_path):
    """A sink WITHOUT an I-V sweep keeps the legacy AC-only row (no idc55) so the legacy
    emit block fires -- byte-identical to the pre-stage-2b path."""
    npz, m = _sweep_npz(tmp_path, with_iv=False)
    res = FMP.fit_multiport(str(npz), m)
    rows = [r for r in res["current"] if r["sink"] == "i500n"]
    assert all("idc55" not in r for r in rows)         # legacy rows (no large-signal core)
    # the swept-load rows (those carrying a y_<c> array) still fit the AC admittance g0
    swept = [r for r in rows if r["il"].startswith("L0") or r["il"].startswith("L1")
             or r["il"].startswith("L2")]
    assert swept and all("g0" in r for r in swept)     # admittance still fit per swept load


def test_emit_dispatches_largesignal(tmp_path):
    """End-to-end: the large-signal row reaches emit_pmu_va and triggers the validated
    large-signal VA block with the folded (sink -> -gdd) sign."""
    npz, m = _sweep_npz(tmp_path)
    res = FMP.fit_multiport(str(npz), m)
    va = D.emit_pmu_va(res, "PMU_ls", tmp_path / "ls.va", supply="AVDD1P0", ground="VSS")
    txt = va.read_text()
    assert "Idc(T)+I-V knee+g0+Cp+signed PSRR+noise" in txt
    assert "i500n_idc55 = 2.0" in txt
    assert "i500n_gdd = -2.000000e-07" in txt          # sink fold of +2e-7
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["pll"], ["i500n"], "VSS")
    assert ok, problems


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
