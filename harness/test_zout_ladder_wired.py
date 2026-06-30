"""STEP-2 lock: the higher-order (L||R)-ladder Zout WIRED through fit_multiport + emit.

STEP 1 (harness/test_zout_ladder.py) locked the fit_model primitives (zmodel extra=,
fit_zout_ladder). This locks the WIRING:

  (i)  the measured WuR pll rail, fitted via fit_multiport._fit_voltage_output (the in-situ
       path), ADOPTS >=1 extra ladder section, and the EMITTED PMU .va run through LOCAL
       Spectre AC reproduces the measured |Zout| as a SHAPE (G1): |Z| within 1.5 dB at
       1/10/31.6 MHz, plateau within 10% of the measured shelf top, and the rise corner
       relocated near the data (NOT the single-shelf 447 kHz artifact). DC settles to vreg
       and a load step is bounded. (Spectre parts skip when no local Spectre.)
  (ii) a SINGLE-SECTION synthetic view yields extra=[] AND the emitted rail block is
       byte-identical to the pre-ladder emit: emitting with the (empty) extra present is
       byte-for-byte the same as emitting with the "extra" key absent -- the gating
       contract (extra absent/[] -> byte-identical). The emitted .va carries NO ladder
       node/var/section tokens. A best-effort git comparison vs HEAD:emit_pmu_model.py is
       also run when a pre-change baseline can be established.
"""
import copy
import pathlib
import subprocess
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_model as FM            # noqa: E402
import fit_multiport as FMP       # noqa: E402
import emit_pmu_model as EPM      # noqa: E402

ZNPZ = ROOT / "results" / "redzone" / "wur_pmu_real_tt_55c.repro.npz"


def _spectre():
    try:
        import spectre_run as SR
        return SR if SR.available() else None
    except Exception:                              # noqa: BLE001
        return None


# ----------------------------------------------------------------- (i) pll ladder + G1
def _pll_view():
    d = np.load(ZNPZ, allow_pickle=True)
    il = "pll"
    z = np.asarray(d["z_pll_tt_55c"])
    p = np.asarray(d["p_pll_avdd1p0_tt_55c"])
    n = np.asarray(d["noise_pll_tt_55c"])
    sp = {f"z_{il}": z, f"p_{il}": p, f"noise_{il}": n, "loads": [il]}
    view = dict(npz=sp, loads=[il], primary_supply="AVDD1P0",
                supplies={"AVDD1P0": {il: p}}, tr_steps={})
    return view, il, z


def test_pll_insitu_adopts_ladder():
    """The in-situ fit of the measured pll Zout adopts >=1 extra ladder section, and the
    per-corner err (zrms/psrr/noise) is good (the ladder did not hurt PSRR/noise)."""
    if not ZNPZ.exists():
        pytest.skip("measured z_pll npz not present")
    view, il, z = _pll_view()
    vfit = FMP._fit_voltage_output("pll", view, ["AVDD1P0"], vout_dc=0.8)
    extra = vfit["P"][il].get("extra") or []
    assert len(extra) >= 1, f"expected a ladder, got extra={extra}"
    e = vfit["err"][0]
    assert e["zrms"] < 1.0, f"zrms {e['zrms']:.3f}dB"
    # PSRR/noise must not blow up (baselines ~0.45 / ~1.22 dB; the ladder should not regress)
    assert e["psrr"]["AVDD1P0"][0] < 1.0, e["psrr"]
    assert e["nrms"] < 2.0, e["nrms"]


def test_pll_emitted_va_passes_G1_spectre(tmp_path):
    """Emit the pll rail from the in-situ fit; LOCAL Spectre AC must reproduce |Zout| as a
    shape (G1) + DC settles to vreg + a load step is bounded."""
    if not ZNPZ.exists():
        pytest.skip("measured z_pll npz not present")
    SR = _spectre()
    if SR is None:
        pytest.skip("local Spectre not available")
    view, il, z = _pll_view()
    pin = "VDD0P8_PLL"
    vfit = FMP._fit_voltage_output("pll", view, ["AVDD1P0"], vout_dc=0.8)
    vfit["pin"] = pin
    res = dict(voltage={"pll": vfit}, current=[],
               meta=dict(name="wur_pll", supplies=["AVDD1P0"], supply_dc=1.0))
    va = tmp_path / "pmu_pll.va"
    EPM.emit_pmu_va(res, "PMU_pll", va, supply="AVDD1P0", ground="VSS", supply_dc=1.0)

    fmeas = z[:, 0]
    magm = np.abs(z[:, 1] + 1j * z[:, 2])

    # ---- AC |Zout|: inject 1A into the rail node (VSS tied to 0 or the model floats) ----
    scs = ("simulator lang=spectre\n"
           f'ahdl_include "{va.resolve()}"\n'
           f"Xd (AVDD1P0 {pin} VSS) PMU_pll\n"
           "Vvss (VSS 0) vsource dc=0\n"
           "Vs (AVDD1P0 0) vsource dc=1.0 mag=0\n"
           f"Iac (0 {pin}) isource mag=1 dc=0\n"
           "ac1 ac start=10 stop=5e8 dec=30\n"
           f"save {pin}\n")
    a = SR.run(scs, "wired_g1_ac")["ac1"]
    fkey = "freq" if "freq" in a else next(k for k in a if k.lower().startswith("freq"))
    fmod = np.asarray(a[fkey]).real
    moda = np.abs(np.asarray(a[pin]))

    def dberr(fq):
        im = int(np.argmin(np.abs(fmeas - fq)))
        io = int(np.argmin(np.abs(fmod - fq)))
        return 20 * np.log10(moda[io] / magm[im])
    for fq in (1e6, 1e7, 3.16e7):
        assert abs(dberr(fq)) < 1.5, f"|Z| dB err {dberr(fq):.2f} at {fq:.2g}Hz"

    # plateau = the measured shelf top (data rolls off slightly above the peak; the ladder
    # targets the shelf top 197/52 ohm), model evaluated at the measured peak frequency
    plateau_meas = float(np.max(magm))
    fpk = fmeas[int(np.argmax(magm))]
    plateau_mod = float(moda[int(np.argmin(np.abs(fmod - fpk)))])
    assert abs(plateau_mod - plateau_meas) / plateau_meas < 0.10, \
        f"plateau {plateau_mod:.1f} vs {plateau_meas:.1f}"

    # rise corner: where |Z| first crosses 10 ohm; model must be near the data, NOT 447 kHz
    def cross(fa, za, lvl):
        idx = np.where(za >= lvl)[0]
        return fa[idx[0]] if len(idx) else fa[-1]
    fc_m, fc_o = cross(fmeas, magm, 10.0), cross(fmod, moda, 10.0)
    assert 0.5 < fc_o / fc_m < 2.0, f"corner model {fc_o:.0f} vs data {fc_m:.0f}Hz"
    assert fc_o < 2e5, f"rise corner {fc_o:.0f}Hz -- the single-shelf 447kHz artifact is back"

    # ---- DC: vout settles to vreg (~0.8V) via a tiny DC sweep (op-point writes no PSF) ----
    scs = ("simulator lang=spectre\n"
           f'ahdl_include "{va.resolve()}"\n'
           f"Xd (AVDD1P0 {pin} VSS) PMU_pll\n"
           "Vvss (VSS 0) vsource dc=0\n"
           "Vs (AVDD1P0 0) vsource dc=1.0\n"
           f"Iload ({pin} 0) isource dc=1u\n"
           "swp dc dev=Iload param=dc start=1e-6 stop=5e-4 lin=40\n"
           f"save {pin}\n")
    vo = float(np.asarray(SR.run(scs, "wired_g1_dc")["swp"][pin]).real[-1])
    assert abs(vo - 0.8) < 0.05, f"DC vout {vo:.4f}V != vreg 0.8"

    # ---- tran: a 0.5->2mA load step stays finite + bounded (passive, no neg-R runaway) ----
    T0, TST = 2e-6, 4e-5
    wave = f"0 5e-4 {T0:g} 5e-4 {T0+1e-9:g} 2e-3 {TST:g} 2e-3"
    scs = ("simulator lang=spectre\n"
           f'ahdl_include "{va.resolve()}"\n'
           f"Xd (AVDD1P0 {pin} VSS) PMU_pll\n"
           "Vvss (VSS 0) vsource dc=0\n"
           "Vs (AVDD1P0 0) vsource dc=1.0\n"
           f"Ild ({pin} 0) isource type=pwl wave=[{wave}]\n"
           f"trn tran stop={TST:g} step={TST/800:g} maxstep={TST/800:g}\n"
           f"save {pin}\n")
    v = np.asarray(SR.run(scs, "wired_g1_tr")["trn"][pin]).real
    assert np.all(np.isfinite(v)), "tran produced non-finite vout"
    assert -0.05 < v.min() and v.max() < 1.05, f"tran unbounded: [{v.min():.3f},{v.max():.3f}]"


# ----------------------------------------------------- (ii) single-section -> byte-identical
def _single_section_view():
    """A synthetic single-section (R_a + sL_a||R_pl)||cap Zout view that a single shelf fits
    exactly -> the gated ladder cannot beat it by 0.3dB -> extra=[] (the byte-identical path)."""
    f = np.logspace(2, 8, 120)
    s = 2j * np.pi * f
    R_a, L_a, R_pl, C, RC = 0.5, 5e-6, 40.0, 1e-9, 0.05
    ZA = R_a + (s * L_a * R_pl) / (s * L_a + R_pl)
    ZC = RC + 1.0 / (s * C)
    Z = 1.0 / (1.0 / ZA + 1.0 / ZC)
    il = "r1"
    z = np.c_[f, Z.real, Z.imag]
    H = 2e-3 * (1.0 / (1.0 + s / (2 * np.pi * 1e5)))     # a clean min-phase PSRR shelf
    p = np.c_[f, H.real, H.imag]
    Sv = 5e-8 / np.sqrt(f) + 2e-9                          # flicker + white output noise
    n = np.c_[f, Sv]
    sp = {f"z_{il}": z, f"p_{il}": p, f"noise_{il}": n, "loads": [il]}
    view = dict(npz=sp, loads=[il], primary_supply="AVDD1P0",
                supplies={"AVDD1P0": {il: p}}, tr_steps={})
    return view, il


def test_single_section_no_ladder_byte_identical(tmp_path):
    """A clean single-section view -> extra=[] -> the emitted .va has NO ladder tokens, and
    emitting with extra=[] present is byte-for-byte the same as emitting with "extra" absent."""
    view, il = _single_section_view()
    vfit = FMP._fit_voltage_output("r1", view, ["AVDD1P0"], vout_dc=0.8)
    assert (vfit["P"][il].get("extra") or []) == [], \
        f"single-section view spuriously adopted a ladder: {vfit['P'][il].get('extra')}"

    res = dict(voltage={"r1": vfit}, current=[],
               meta=dict(name="syn", supplies=["AVDD1P0"], supply_dc=1.0))
    txt_with = EPM.emit_pmu_va(res, "PMU_syn", tmp_path / "syn_a.va",
                               supply="AVDD1P0", ground="VSS").read_text()
    # no ladder node/var/section tokens leaked into the byte-identical path
    for tok in ("_nA2", "_La2", "_Rpl2", "ladder section"):
        assert tok not in txt_with, f"ladder token {tok!r} present on the extra=[] path"

    # strip the (empty) extra key -> emit again -> must be byte-for-byte identical (extra
    # absent == extra=[] : the gating contract that makes standalone/synthetic untouched)
    res2 = copy.deepcopy(res)
    for P_il in res2["voltage"]["r1"]["P"].values():
        P_il.pop("extra", None)
    txt_without = EPM.emit_pmu_va(res2, "PMU_syn", tmp_path / "syn_b.va",
                                  supply="AVDD1P0", ground="VSS").read_text()
    assert txt_with == txt_without, "extra=[] is not byte-identical to extra absent"


def test_single_section_matches_pre_change_emit_head(tmp_path):
    """Best-effort: the extra=[] emit equals HEAD:emit_pmu_model.py's emit on the SAME fit
    (the explicit 'byte-identical to the pre-change emit' gate). Skips gracefully when a
    pre-change baseline cannot be established (e.g. HEAD already carries the ladder code, or
    git/the worktree is unavailable)."""
    import importlib.util
    try:
        head_src = subprocess.run(
            ["git", "-C", str(ROOT), "show", "HEAD:harness/emit_pmu_model.py"],
            capture_output=True, text=True, check=True).stdout
    except Exception:                              # noqa: BLE001
        pytest.skip("git HEAD:emit_pmu_model.py unavailable")
    if "_extra_body" in head_src:
        pytest.skip("HEAD already carries the ladder emit (post-commit) -- no pre-change baseline")

    head_file = tmp_path / "head_emit_pmu_model.py"
    head_file.write_text(head_src)
    spec = importlib.util.spec_from_file_location("head_emit_pmu_model", head_file)
    HEAD = importlib.util.module_from_spec(spec)
    sys.modules["head_emit_pmu_model"] = HEAD
    spec.loader.exec_module(HEAD)

    view, il = _single_section_view()
    vfit = FMP._fit_voltage_output("r1", view, ["AVDD1P0"], vout_dc=0.8)
    assert (vfit["P"][il].get("extra") or []) == []
    res = dict(voltage={"r1": vfit}, current=[],
               meta=dict(name="syn", supplies=["AVDD1P0"], supply_dc=1.0))
    txt_work = EPM.emit_pmu_va(res, "PMU_syn", tmp_path / "syn_work.va",
                               supply="AVDD1P0", ground="VSS").read_text()
    txt_head = HEAD.emit_pmu_va(res, "PMU_syn", tmp_path / "syn_head.va",
                                supply="AVDD1P0", ground="VSS").read_text()
    assert txt_work == txt_head, "wired emit drifted from the pre-change (HEAD) emit on extra=[]"


if __name__ == "__main__":
    test_pll_insitu_adopts_ladder()
    test_single_section_no_ladder_byte_identical()
    print("wired ladder lock (non-Spectre parts): PASS")
