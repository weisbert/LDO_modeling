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


def _sweep_npz(tmp_path, *, with_iv=True, with_temp=False, name="sweep", temps=None, curve=0.0):
    """A multi-load (x temp) npz shaped like pmu_corner's sweep output: per-load z/p/noise
    under swept labels + a once-cell 'Lnom_T..' carrying the sink I-V, plus meta_iload_<o>
    (real per-LABEL rail current, NaN on the once-cell) and meta_temp.
    `temps` overrides the corner list; `curve` adds an A/degC^2 quadratic to the sink Idc(T)
    (0.0 = the default linear PTAT) so the 2nd-order temp fit can be exercised."""
    temps = temps if temps is not None else ([-40.0, 55.0, 125.0] if with_temp else [55.0])
    loadcur = {"L0": 50e-6, "L1": 580e-6, "L2": 2000e-6}
    rec, labels, mi_pll, mtemp = {}, [], [], []
    for T in temps:
        once = f"Lnom_T{T:g}"
        labels.append(once); mi_pll.append(float("nan")); mtemp.append(T)
        # the sink I-V sweep lives on the once-cell (one per temp); Idc has a small PTAT slope
        # (+ an optional quadratic curvature for the 2nd-order-fit tests)
        Vo = np.linspace(0.0, 0.8, 40)
        idc = 200e-6 * (1.0 + 0.001 * (T - 55.0)) + curve * (T - 55.0) ** 2
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
    assert r.get("d2", 0.0) == 0.0                     # 3 temps -> quadratic gate not met
    assert res["meta"]["tnom_c"] == 55.0               # middle of the temps


def test_current_largesignal_quadratic_temp(tmp_path):
    """The IN-SITU/on-silicon crow path must carry d2: a >=5-temp sweep with genuine curvature
    -> the row's d2 != 0 (guards the fit_multiport wiring), while a >=5-temp PURE-LINEAR sweep
    keeps d2 == 0 (adversarial keep-best reject -> no spurious curvature)."""
    temps = [-40.0, -10.0, 20.0, 55.0, 90.0, 125.0]
    npz, m = _sweep_npz(tmp_path, with_temp=True, temps=temps, curve=2e-9, name="curv")
    r = next(r for r in FMP.fit_multiport(str(npz), m)["current"] if r["sink"] == "i500n")
    assert "d2" in r and r["d2"] != 0.0                # curvature engaged AND carried in the crow
    npz2, m2 = _sweep_npz(tmp_path, with_temp=True, temps=temps, curve=0.0, name="lin5")
    r2 = next(r for r in FMP.fit_multiport(str(npz2), m2)["current"] if r["sink"] == "i500n")
    assert r2["d2"] == 0.0                             # 5+ linear temps -> keep-best rejects


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


# =============================================== 3) transient DC load-reg schedule
def _vstep(vf, vt, t0=5e-6, tstop=1e-5, n=400):
    """A clean load-step [t,V] waveform: settled at vf before t0, vt after (the shape the
    settled-DC extractor reads)."""
    t = np.linspace(0.0, tstop, n)
    return np.c_[t, np.where(t < t0, vf, vt)]


def _transient_npz(tmp_path, curve, name="tr", labels=("2m", "3m", "4m")):
    """Single-OP voltage npz (z/p/noise at 'nom') PLUS transient load steps 100u->{2m,3m,4m},
    keyed EXACTLY as the real manifest->importmp pipeline produces them: tr_<o>_<label> with the
    manifest's CUSTOM label and NO _<load> suffix (importmp writes the manifest tag verbatim --
    the box's REAL_wur_pmu_top.json uses labels "2m"/"3m"/"4m"). The (i_from,i_to) currents are
    declared ONLY in the manifest's coverage.transient -- NOT recoverable from the opaque key --
    so this fixture FAILS if the fit string-parses currents out of the key (the shipped bug).
    `curve` = {iload: settled Vout}; the steps are 100u->each. R_a from _ac() is 0.05."""
    z, p, n = _ac()
    rec = {"loads": np.array(["nom"]),
           "z_pll_nom": z, "p_pll_AVDD1P0_nom": p, "noise_pll_nom": n,
           "meta_iload_pll": np.array([1e-4])}
    i_from = 1e-4
    steps = []
    for i_to, lbl in zip((2e-3, 3e-3, 4e-3), labels):
        rec[f"tr_pll_{lbl}"] = _vstep(curve[i_from], curve[i_to])   # custom label, no _load
        steps.append({"from": i_from, "to": i_to, "label": lbl})
    npz = tmp_path / f"{name}.npz"
    np.savez(npz, **rec)
    m = {"name": name, "supplies": {"AVDD1P0": {"dc": 1.05, "net": "VDD1P0"}},
         "current_psrr_supplies": ["AVDD1P0"],
         "v_out": {"pll": {"net": "VPLL", "iload": 1e-4, "vout_dc": 0.8}}, "i_out": {},
         "coverage": {"transient": {"pll": {"steps": steps}}}}
    return npz, m


def test_vreg_schedule_from_transient(tmp_path):
    """fit_multiport derives a vreg(iload) DC load-reg schedule from the rail's transient
    load steps: settled Vout per step current -> vreg = Vout + R_a*iload, scheduled vs
    ln(iload). The 100u->{2m,3m,4m} steps yield settled V at {100u,2m,3m,4m}."""
    curve = {1e-4: 0.800, 2e-3: 0.802, 3e-3: 0.803, 4e-3: 0.804}   # rising load-reg
    npz, m = _transient_npz(tmp_path, curve)
    res = FMP.fit_multiport(str(npz), m)
    vs = res["voltage"]["pll"]["vreg_sched"]
    assert vs is not None, "transient present -> vreg_sched must be built"
    assert vs["currents"] == [1e-4, 2e-3, 3e-3, 4e-3]
    R_a = float(res["voltage"]["pll"]["P"]["nom"]["R_a"])  # the FITTED Zout floor
    exp = [curve[i] + R_a * i for i in vs["currents"]]      # vreg = Vout_settled + R_a*iload
    assert np.allclose(vs["vregs"], exp, atol=1e-9), (vs["vregs"], exp)
    assert vs["i_nom"] == 1e-4                              # the AC OP load


def test_vreg_schedule_from_opaque_custom_labels(tmp_path):
    """REGRESSION (the a8880df shipped-but-inert bug): the load currents come from the manifest
    step decl, NOT from string-parsing the key. With fully OPAQUE labels (no numeric content,
    no _<load> suffix -- e.g. 'stepA') the OLD parser returned {} -> vreg_sched=None -> the
    emit was byte-identical to pre-fix (the user's '没区别'). The schedule MUST still be built."""
    curve = {1e-4: 0.800, 2e-3: 0.802, 3e-3: 0.803, 4e-3: 0.804}
    npz, m = _transient_npz(tmp_path, curve, labels=("stepA", "stepB", "stepC"))
    res = FMP.fit_multiport(str(npz), m)
    vs = res["voltage"]["pll"]["vreg_sched"]
    assert vs is not None, "opaque-label transient -> schedule must still build (from manifest)"
    assert vs["currents"] == [1e-4, 2e-3, 3e-3, 4e-3]


def test_vreg_schedule_bridges_label_form_divergence(tmp_path):
    """REGRESSION (the box's `3 transient wfm in npz, 0 mapped`): the npz was imported with the
    AUTO `<from:g>_<to:g>` label form (`tr_pll_0.0001_0.002`) but the FIT manifest carries CUSTOM
    labels (2m/3m/4m) -- the recurring GUI-built vs hand-edited REAL divergence. split_ports must
    BRIDGE by from/to so the schedule still builds WITHOUT a re-import."""
    curve = {1e-4: 0.800, 2e-3: 0.802, 3e-3: 0.803, 4e-3: 0.804}
    z, p, n = _ac()
    rec = {"loads": np.array(["nom"]), "z_pll_nom": z, "p_pll_AVDD1P0_nom": p,
           "noise_pll_nom": n, "meta_iload_pll": np.array([1e-4])}
    for i_to in (2e-3, 3e-3, 4e-3):                          # AUTO-form npz keys (no custom label)
        rec[f"tr_pll_{1e-4:g}_{i_to:g}"] = _vstep(curve[1e-4], curve[i_to])
    npz = tmp_path / "bridge.npz"
    np.savez(npz, **rec)
    m = {"name": "bridge", "supplies": {"AVDD1P0": {"dc": 1.05, "net": "VDD1P0"}},
         "current_psrr_supplies": ["AVDD1P0"],
         "v_out": {"pll": {"net": "VPLL", "iload": 1e-4, "vout_dc": 0.8}}, "i_out": {},
         "coverage": {"transient": {"pll": {"steps": [       # CUSTOM labels -- divergent from npz
             {"from": 1e-4, "to": 2e-3, "label": "2m"},
             {"from": 1e-4, "to": 3e-3, "label": "3m"},
             {"from": 1e-4, "to": 4e-3, "label": "4m"}]}}}}
    res = FMP.fit_multiport(str(npz), m)
    vs = res["voltage"]["pll"]["vreg_sched"]
    assert vs is not None, "auto-form npz + custom-label manifest must bridge by from/to"
    assert vs["currents"] == [1e-4, 2e-3, 3e-3, 4e-3]


def test_no_transient_vreg_sched_none(tmp_path):
    """No transient arrays -> vreg_sched is None (the single-OP default; emit stays byte-
    identical via the baked-vreg parameter path)."""
    npz, m = _sweep_npz(tmp_path)                           # AC sweep, no tr_ arrays
    res = FMP.fit_multiport(str(npz), m)
    assert res["voltage"]["pll"]["vreg_sched"] is None


def test_transient_endtoend_emits_vreg_schedule(tmp_path):
    """The transient-derived schedule survives fit -> emit: the .va carries an iload_<rail>
    param + an ln(iload) vreg schedule (and compiles structurally via va_sanity)."""
    curve = {1e-4: 0.800, 2e-3: 0.802, 3e-3: 0.803, 4e-3: 0.804}
    npz, m = _transient_npz(tmp_path, curve)
    res = FMP.fit_multiport(str(npz), m)
    res["voltage"]["pll"]["pin"] = "VDD0P8_PLL"
    txt = D.emit_pmu_va(res, "PMU_tr", tmp_path / "tr.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "parameter real iload_VDD0P8_PLL = 1.000000e-04;" in txt
    assert "VDD0P8_PLL_vreg = min(max(" in txt
    assert "parameter real VDD0P8_PLL_vreg" not in txt
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VDD0P8_PLL"], [], ["VSS"])
    assert ok, problems


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))


# =============================================== current-output NOISE (req 1) wiring
def test_current_noise_fit_emit_report_when_measured(tmp_path):
    """coverage.inoise measured noise_i_<c>_<load> -> the sink fit carries REAL in_white/in_kf
    (not the hardcoded 0), emit ships a non-zero white_noise, the report shows a 'noise' panel
    with a finite nrms that is KEPT OUT of the pass/fail grade composite."""
    import report_multiport as RMP
    npz, m = _sweep_npz(tmp_path, with_temp=False, name="cn")
    ref = dict(np.load(npz, allow_pickle=True))
    f = np.logspace(1, 8, 40)
    In = np.sqrt((2e-14) ** 2 + 1e-30 / f)               # white 20 fA/rtHz + a 1/f corner
    for lbl in [str(x) for x in ref["loads"]]:
        ref[f"noise_i_i500n_{lbl}"] = np.c_[f, In]
    p2 = tmp_path / "cn2.npz"; np.savez(p2, **ref)
    res = FMP.fit_multiport(str(p2), m)
    row = next(r for r in res["current"] if r["sink"] == "i500n")
    assert row["in_white"] > 0 and abs(row["in_white"] - 2e-14) / 2e-14 < 0.05
    blk = D._current_block_largesignal("i500n", row, "AVDD1P0", "VSS")
    assert "white_noise(i500n_inw2" in blk["body"] and "i500n_inw2 = 0.000000e+00" not in blk["asg"]
    cv = next(v for v in RMP.port_views(res, str(p2), m)
              if v["kind"] == "current" and v["name"] == "i500n")
    assert "noise" in cv["present"] and np.isfinite(cv["metrics"]["nrms"])
    assert "noise" not in [n for n, *_ in cv["grade"]["metrics"]]   # NOT in the grade composite


def test_current_noise_absent_keeps_honest_zero_stub(tmp_path):
    """No noise_i -> in_white=in_kf=0 (the honest stub), no 'noise' panel, nrms NaN. Byte-compat
    with every npz that did not run coverage.inoise."""
    import report_multiport as RMP
    npz, m = _sweep_npz(tmp_path, with_temp=False, name="cn0")
    res = FMP.fit_multiport(str(npz), m)
    row = next(r for r in res["current"] if r["sink"] == "i500n")
    assert row["in_white"] == 0.0 and row["in_kf"] == 0.0
    cv = next(v for v in RMP.port_views(res, str(npz), m) if v["kind"] == "current")
    assert "noise" not in cv["present"] and not np.isfinite(cv["metrics"]["nrms"])


# =============================================== gated HYBRID voltage-noise (loop-shaped rails)
def test_voltage_noise_hybrid_engages_on_loop_shaped_rail(tmp_path):
    """A real loop-shaped rail (high-Q Zout peak + a smoothly-falling Sv) has In=Sv/|Zout| the
    Norton bank can't hold -> the gated keep-best engages 'hybrid' (series voltage-noise) and the
    nrms drops sharply. A FLAT synthetic rail stays 'norton' (no regression, byte-identical)."""
    # flat synthetic rail -> norton
    npz0, m = _sweep_npz(tmp_path, with_temp=False, name="flat")
    res0 = FMP.fit_multiport(str(npz0), m)
    assert res0["voltage"]["pll"]["nmode"] == "norton"
    # loop-shaped rail -> hybrid, much lower nrms
    ref = dict(np.load(npz0, allow_pickle=True))
    f = np.logspace(1, np.log10(5e8), 120); w = 2 * np.pi * f; w0 = 2 * np.pi * 10e6; Q = 8
    Z = (1j * w * 0.02) / (1 - (w / w0) ** 2 + 1j * w / (Q * w0)) + 0.1     # high-Q Zout
    fn = np.logspace(1, 8, 40); Sv = np.sqrt((3e-8) ** 2 + 1e-9 / fn)        # falling Sv
    for lbl in [str(x) for x in ref["loads"]]:
        if f"z_pll_{lbl}" in ref:
            ref[f"z_pll_{lbl}"] = np.c_[f, Z.real, Z.imag]
            ref[f"noise_pll_{lbl}"] = np.c_[fn, Sv]
    p2 = tmp_path / "loop.npz"; np.savez(p2, **ref)
    res = FMP.fit_multiport(str(p2), m)
    fit = res["voltage"]["pll"]
    assert fit["nmode"] == "hybrid" and fit["nfkv"]
    assert max(e["nrms"] for e in fit["err"]) < 6.0       # hybrid holds it (Norton stalled ~16dB)
    # the report path REPRODUCES the same nrms (uses the rail's nmode/nfkv, not hardcoded norton)
    import report_multiport as RMP
    cv = next(v for v in RMP.port_views(res, str(p2), m) if v["kind"] == "voltage")
    assert cv["worst"]["nrms"] < 6.0
