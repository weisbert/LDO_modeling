"""SPECTRE-GATED end-to-end validation (vs an INDEPENDENT GT, not vs self): the PMU fit
that CONSUMES transient load steps reproduces the ground-truth DC load regulation, where the
no-transient baseline is ~20 mV off.

Mirrors the user's real symptom: the manually-imported single-port model reproduced the
startup drop + the steady-state 20 mV perfectly, but the automated PMU model (single OP, vreg
baked from the 0.8 TARGET) sat ~20 mV high and load-dependent. The fix (fit_multiport reads the
rail's transient steps -> vreg(iload) DC load-reg schedule) is validated here against an
INDEPENDENT behavioral GT (a different Verilog-A LDO, regulated to 0.78 with a nonlinear
loop-limited output resistance -- NOT our emit, NOT our fit):

  1. drive the GT with one-way load steps 100u->{2m,3m,4m} (real Spectre transient physics);
  2. fit_multiport from {single AC OP + those steps} -> emit MODEL_tr.va (WITH the schedule);
  3. fit_multiport from {single AC OP only}        -> emit MODEL_base.va (no schedule, baseline);
  4. DC-sweep the load on the GT and on each model (re-instanced at each load = the schedule
     abscissa) and compare Vout(iload).

Asserts: MODEL_tr tracks the GT load-reg within ~2 mV at every load, AND the baseline is
>10 mV off -- i.e. the transient consumption is demonstrably WHAT fixes the 20 mV (this is the
'validate vs independent GT, not self' methodology, harness memory).

SKIP cleanly when Spectre is absent (honours SPECTRE_HOME override, like the sibling
cadence/test_va_compile_spectre.py).

Run:  python3 -m pytest cadence/test_pmu_loadreg_gt_spectre.py -q
"""
import os
import pathlib
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent          # .../cadence
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "harness")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spectre_run as sr                                                    # noqa: E402
import spectre_bench as sb                                                  # noqa: E402
import fit_multiport as FMP                                                 # noqa: E402
import emit_pmu_model as D                                                  # noqa: E402

IOP = 1e-4                                  # AC operating-point load
LOADS = [1e-4, 2e-3, 3e-3, 4e-3]            # the step-derived load-reg abscissa
GT_VA = '''`include "constants.vams"
`include "disciplines.vams"
module gtldo(vin, vout, gnd);
  inout vin, vout, gnd;  electrical vin, vout, gnd, nint;
  parameter real vset=0.78;     // REAL regulated level (20mV below the 0.8 fit "target")
  parameter real r0=3.0;        // base output resistance
  parameter real kq=400.0;      // load-dependent (loop-gain droop) term
  parameter real cout=2e-8;
  parameter real psrr_dc=0.02;
  real il;
  analog begin
    il = I(nint, vout);
    V(nint, gnd) <+ vset + psrr_dc*(V(vin,gnd) - 0.98);
    V(nint, vout) <+ (r0 + kq*il)*il;
    I(vout, gnd) <+ cout*ddt(V(vout,gnd));
    I(vout, gnd) <+ white_noise(4e-18, "out");
  end
endmodule
'''


def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


def _gt_block(va):
    return f'ahdl_include "{va.resolve()}"\nXdut (vin vout 0) gtldo\n'


def _one_way_step(gt_block, i_from, i_to, tag):
    """A one-way load step i_from -> i_to that STAYS at i_to (manifest from/to semantics).

    The step lands at mid-capture (T0 = TST/2), NOT at the very start: the production
    settled-DC extractor (fit_multiport._settled_step) skips the first 15% of the capture
    span as a TURN-ON guard (real box transients open with a t~0 startup drop whose |dV|
    dwarfs the load step -- shipped in 8e2588f). A real load-step capture therefore puts
    the edge well inside the settled window; this synthetic GT must do the same so it
    exercises the same path. (A step at 5% would fall inside the turn-on guard and the
    extractor would correctly find no settled pre-window -> vreg stays baked.)"""
    T0, edge, TST = 1e-5, 1e-9, 2e-5            # step at 50% of the capture (clear of the 15% guard)
    wave = f"0 {i_from:g} {T0:g} {i_from:g} {T0+edge:g} {i_to:g} {TST:g} {i_to:g}"
    scs = ("simulator lang=spectre\n" + gt_block +
           "Vsup (vin 0) vsource dc=0.98\n"
           f"Ild (vout 0) isource type=pwl wave=[{wave}]\n"
           f"trn tran stop={TST:g} step={TST/400:g} maxstep={TST/400:g}\nsave vout\n")
    d = sr.run(scs, tag)
    return np.c_[np.asarray(d["trn"]["time"]).real, np.asarray(d["trn"]["vout"]).real]


def _dc_vout(block, load, tag):
    """Vout at a given load: re-instance `block` and DC-sweep Ild to `load`."""
    scs = ("simulator lang=spectre\n" + block +
           "Vsup (vin 0) vsource dc=0.98\n"
           "Ild (vout 0) isource dc=1u\n"
           f"swp dc dev=Ild param=dc start=1e-6 stop={load:.6e} lin=60\nsave vout\n")
    d = sr.run(scs, tag)
    il = np.asarray(d["swp"]["dc"]).real
    vo = np.asarray(d["swp"]["vout"]).real
    return float(np.interp(load, il, vo))


def _model_block(va, load):
    return (f'ahdl_include "{va.resolve()}"\n'
            f"Xdut (vin vout 0) {va.stem} iload_VDD0P8_PLL={load:.6e}\n")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pmu_loadreg_gt")
    gt = tmp / "gtldo.va"
    gt.write_text(GT_VA)
    gt_block = _gt_block(gt)
    gtdut = sb.DutSpec(block=lambda il: gt_block, supply="vin", out="vout")

    fz, Z = sb.measure_zout(gtdut, IOP, tag="lrg_z")
    fp, H = sb.measure_psrr(gtdut, IOP, tag="lrg_p")
    fn, Sv = sb.measure_noise(gtdut, IOP, tag="lrg_n")

    rec = {"loads": np.array(["nom"]),
           "z_pll_nom": np.c_[fz, Z.real, Z.imag],
           "p_pll_AVDD1P0_nom": np.c_[fp, H.real, H.imag],
           "noise_pll_nom": np.c_[fn, np.abs(Sv)],
           "meta_iload_pll": np.array([IOP])}
    steps = []
    for i_to, lbl in zip((2e-3, 3e-3, 4e-3), ("2m", "3m", "4m")):
        rec[f"tr_pll_{lbl}"] = _one_way_step(gt_block, IOP, i_to, f"lrg_tr_{i_to:g}")
        steps.append({"from": IOP, "to": i_to, "label": lbl})
    # vout_dc stays at the 0.8 default (the buggy assumption) -- the transient must override it.
    # The (from,to) currents are declared in coverage.transient (the SOURCE OF TRUTH); the npz
    # key tr_pll_<label> uses the box's REAL custom labels "2m"/"3m"/"4m" (NOT a numeric
    # <from>_<to> key) -- so this GT validation now exercises the exact shipped-and-inert path.
    m = {"name": "lrg", "supplies": {"AVDD1P0": {"dc": 0.98, "net": "vin"}},
         "current_psrr_supplies": ["AVDD1P0"],
         "v_out": {"pll": {"net": "vout", "iload": IOP, "vout_dc": 0.8}}, "i_out": {},
         "coverage": {"transient": {"pll": {"steps": steps}}}}

    def _fit_emit(npz_rec, cell):
        npz = tmp / f"{cell}.npz"
        np.savez(npz, **npz_rec)
        res = FMP.fit_multiport(str(npz), m)
        res["voltage"]["pll"]["pin"] = "VDD0P8_PLL"
        va = tmp / f"{cell}.va"
        D.emit_pmu_va(res, cell, va, supply="AVDD1P0", ground="VSS")
        return va, res["voltage"]["pll"].get("vreg_sched")

    va_tr, vs = _fit_emit(rec, "MODEL_tr")
    rec_base = {k: v for k, v in rec.items() if not k.startswith("tr_")}
    va_base, vs0 = _fit_emit(rec_base, "MODEL_base")
    return dict(gt_block=gt_block, va_tr=va_tr, va_base=va_base, vs=vs, vs0=vs0)


def test_fit_consumes_transient_only_when_present(built):
    """The transient run builds a vreg(iload) schedule; the AC-only baseline does not."""
    assert built["vs"] is not None and len(built["vs"]["currents"]) >= 2
    assert built["vs0"] is None


def test_transient_model_tracks_gt_loadreg(built):
    """End-to-end vs the INDEPENDENT GT: the transient-fed model reproduces the GT DC load
    regulation within ~2 mV at every load, while the no-transient baseline is >10 mV off
    (it sits ~20 mV high -- the 0.8-target artifact the user saw)."""
    gt_block, va_tr, va_base = built["gt_block"], built["va_tr"], built["va_base"]
    tr_err = base_err = 0.0
    for L in LOADS:
        vg = _dc_vout(gt_block, L, f"lrg_dc_gt_{L:g}")
        vt = _dc_vout(_model_block(va_tr, L), L, f"lrg_dc_tr_{L:g}")
        vb = _dc_vout(_model_block(va_base, L), L, f"lrg_dc_bs_{L:g}")
        tr_err = max(tr_err, abs(vt - vg))
        base_err = max(base_err, abs(vb - vg))
    assert tr_err < 2e-3, f"transient model off the GT load-reg by {tr_err*1e3:.2f} mV"
    assert base_err > 1e-2, (f"baseline only {base_err*1e3:.2f} mV off -- the GT is too easy "
                             f"to make this test meaningful")


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q", "-s"]))
