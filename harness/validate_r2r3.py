"""Acceptance tests for R2 (explicit gnd port) + R3-L1 (settable vdd / voutdc) on the
emitted .lib model (ngspice). Run AFTER a fit+emit of the target variant.

  [1] FLOATING GROUND (R2): lift the model's gnd to -0.317V and re-measure Zout
      referenced to it; must equal the gnd=0 measurement to numerical precision
      (proves no global-node-0 reference survives inside the subckt).
  [2] VDD LINE-REG TRACKING (R3-L1): step the vdd instance param across the
      characterized span; Vout DC shift must follow the GT dc_linereg curve
      (within the emitted poly's fit residual + a small margin).
  [3] VOUTDC OVERRIDE (R3-L1): voutdc=0.85 pins Vout(iload) to 0.85V.
  [4] DEFAULTS: with no new params given, Vout DC equals the characterized value
      (byte-compat: the score path already proves the full composite).

    python harness/validate_r2r3.py [--variant base]
"""
import argparse
import sys
import pathlib

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ng          # noqa: E402
import bench       # noqa: E402

TOL_Z_DB = 1e-3      # [1] float-vs-0 Zout match (numerical noise only)
TOL_LR_V = 8e-3      # [2] line-reg tracking tolerance [V] (poly deg-4 + knee smoothing)
TOL_PIN_V = 2e-3     # [3] voutdc pin tolerance [V]


def _run(tb, lib, tag):
    r = ng.run(ng.assemble(tb, libs=[lib]), bench.WORK / tag, outputs=["out.dat"])
    if r["out.dat"] is None:
        raise RuntimeError(f"{tag}: ngspice produced no data:\n{r['_stderr'][-1500:]}")
    return r["out.dat"][1]


def _op_vout(lib, il="121u", vdd=None, voutdc=None, lift=0.0):
    xp = f"iload={il}" + (f" vdd={vdd:g}" if vdd is not None else "") \
        + (f" voutdc={voutdc:g}" if voutdc is not None else "")
    vin = vdd if vdd is not None else 1.05
    tb = f"""* op
Xdut vin vout g ldo_model {xp}
Vlift g 0 DC {lift:g}
Vin vin g DC {vin:g}
Iload vout g DC {il}
.control
op
wrdata out.dat v(vout) v(g)
quit
.endc
.end
"""
    a = _run(tb, lib, "r23op")
    return float(a[0, 1] - a[0, 3])          # Vout referenced to the model's gnd


def _zout(lib, il="121u", lift=0.0):
    tb = f"""* Zout (gnd lifted {lift:g}V)
Xdut vin vout g ldo_model iload={il}
Vlift g 0 DC {lift:g}
Vin vin g DC 1.05
Iload vout g DC {il} AC 0
Iac g vout AC 1
.control
set wr_singlescale
ac dec 20 100 100meg
wrdata out.dat vr(vout) vi(vout) vr(g) vi(g)
quit
.endc
.end
"""
    a = _run(tb, lib, "r23z")
    return a[:, 0], (a[:, 1] - a[:, 3]) + 1j * (a[:, 2] - a[:, 4])


def main(vkey="base"):
    name = "ldo_model" if vkey == "base" else f"ldo_{vkey}"
    lib = str(ng.ROOT / "model" / f"{name}.lib")
    ref = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
    fails = []

    # [1] floating ground
    f0, z0 = _zout(lib, lift=0.0)
    f1, z1 = _zout(lib, lift=-0.317)
    dz = float(np.max(np.abs(20 * np.log10(np.abs(z1) / np.abs(z0)))))
    ok = dz <= TOL_Z_DB
    print(f"[1] floating-gnd Zout: max|delta| = {dz:.2e} dB (tol {TOL_Z_DB:g}) "
          f"-> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("floating-gnd")

    # [2] vdd line-reg tracking
    if "dc_linereg" in ref.files:
        lr = ref["dc_linereg"]
        v0 = _op_vout(lib)                       # characterized nominal (vdd default)
        worst = 0.0
        for vdd in (float(lr[:, 0].min()), 0.95, 1.15, float(lr[:, 0].max())):
            vm = _op_vout(lib, vdd=vdd)
            gt_shift = (np.interp(vdd, lr[:, 0], lr[:, 1])
                        - np.interp(1.05, lr[:, 0], lr[:, 1]))
            err = abs((vm - v0) - gt_shift)
            worst = max(worst, err)
            print(f"    vdd={vdd:5.3f}V: model dVout={1e3*(vm-v0):+7.2f}mV  "
                  f"GT linereg {1e3*gt_shift:+7.2f}mV  (err {1e3*err:.2f}mV)")
        ok = worst <= TOL_LR_V
        print(f"[2] vdd line-reg tracking: worst {1e3*worst:.2f}mV (tol {1e3*TOL_LR_V:g}mV) "
              f"-> {'PASS' if ok else 'FAIL'}")
        if not ok:
            fails.append("vdd-linereg")
    else:
        print("[2] vdd line-reg tracking: SKIP (ref has no dc_linereg)")

    # [3] voutdc override
    vp = _op_vout(lib, voutdc=0.85)
    ok = abs(vp - 0.85) <= TOL_PIN_V
    print(f"[3] voutdc=0.85 pin: Vout = {vp:.4f}V (tol {1e3*TOL_PIN_V:g}mV) "
          f"-> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("voutdc-pin")

    # [4] defaults reproduce the characterized DC
    dl = ref["dc_loadreg"]
    gt = float(np.interp(ng.amps("121u"), dl[:, 0], dl[:, 1]))
    v0 = _op_vout(lib)
    ok = abs(v0 - gt) <= TOL_PIN_V
    print(f"[4] default DC: Vout = {v0:.4f}V vs GT loadreg {gt:.4f}V "
          f"-> {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("default-dc")

    print(f"\n>>> R2/R3-L1 acceptance: {'PASS' if not fails else 'FAIL ' + str(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    a = ap.parse_args()
    raise SystemExit(main(a.variant))
