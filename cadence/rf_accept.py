"""SpectreRF PSS acceptance at the RF carrier (the 304 MHz use case).

Drives a periodic supply tone at `fcar` into the DUT's supply pin and reads the
steady-state vout harmonic spectrum (PSS). Compares the behavioral model to the
transistor GT on:
  * PSRR @ carrier (vout fundamental / supply tone)      -> fidelity at the RF freq
  * harmonic levels (2f, 3f) = nonlinear upconversion     -> sideband-asymmetry proxy
  * PSS wall-clock                                         -> the model's whole point (speedup)

The model is all-linear/passive by construction, so it converges in PSS trivially
(a key design goal) and predicts ~zero harmonics; the transistor GT shows the real
(small) nonlinearity. Reuses the validated DutSpec bench (cadence/spectre_bench).
"""
import time
import numpy as np
import spectre_bench as sb
import spectre_run as sr
import psf

ROOT = sb._HARNESS.parent


def pss_spectrum(dut, fcar=304e6, ampl=20e-3, iload="121u", harms=10,
                 tstab=300e-9, tag="pss"):
    il = float(iload.replace("u", "e-6")) if isinstance(iload, str) else float(iload)
    scs = f"""// PSS: {fcar:g} Hz supply tone -> vout harmonic spectrum
simulator lang=spectre
{dut.block(il)}
Vsup ({dut.supply} 0) vsource dc={sb.VIN_DC} mag=0 type=sine ampl={ampl:g} freq={fcar:g}
Ild  ({dut.out} 0)    isource dc={il:g}
pssAna pss fund={fcar:g} harms={harms} tstab={tstab:g} errpreset=conservative
"""
    t0 = time.perf_counter()
    d = sr.run(scs, tag, aux=dut.aux, timeout=600)
    dt = time.perf_counter() - t0
    fd = None
    import pathlib
    for f in (sr.WORK / tag / "raw").iterdir():
        if f.name.endswith(".fd.pss"):
            fd = psf.read_psf(f); break
    if fd is None:
        raise RuntimeError(f"[{tag}] no fd.pss produced")
    fh = np.asarray(fd[fd["_sweep"]]).real
    Vo = np.asarray(fd[dut.out])
    return fh, Vo, dt


def _harm(fh, Vo, k, fcar):
    i = int(np.argmin(np.abs(fh - k * fcar)))
    return abs(Vo[i])


def compare(fcar=304e6, ampl=20e-3, iload="121u"):
    va = str(ROOT / "model" / "ldo_model.va")
    tbl = str(ROOT / "model" / "ldo_model_dropout.tbl")
    model = sb.va_dut(va, tbl=tbl)
    models = [ROOT / "models" / "nmos_lv.mod", ROOT / "models" / "pmos_lv.mod"]
    gt = sb.spice_dut([str(m) for m in models], [str(ROOT / "ground_truth" / "ldo_gt.lib")], "ldo_gt")

    print(f"=== SpectreRF PSS acceptance @ {fcar/1e6:.0f} MHz supply tone "
          f"({ampl*1e3:.0f} mV), load {iload} ===")
    out = {}
    for name, dut in (("GT (transistor)", gt), ("model (behavioral)", model)):
        fh, Vo, dt = pss_spectrum(dut, fcar, ampl, iload, tag=f"pss_{name.split()[0]}")
        f1 = _harm(fh, Vo, 1, fcar)
        psrr = -20 * np.log10(f1 / ampl)
        h2 = _harm(fh, Vo, 2, fcar); h3 = _harm(fh, Vo, 3, fcar)
        out[name] = dict(psrr=psrr, f1=f1, h2dbc=20*np.log10(h2/f1+1e-30),
                         h3dbc=20*np.log10(h3/f1+1e-30), dt=dt)
        print(f"  {name:<18}: PSRR@{fcar/1e6:.0f}M = {psrr:5.1f} dB | "
              f"2f = {out[name]['h2dbc']:6.1f} dBc  3f = {out[name]['h3dbc']:6.1f} dBc | "
              f"PSS {dt:5.2f}s")
    g, m = out["GT (transistor)"], out["model (behavioral)"]
    print(f"\n  PSRR@{fcar/1e6:.0f}M  model-vs-GT delta = {m['psrr']-g['psrr']:+.2f} dB "
          f"(model {'reproduces' if abs(m['psrr']-g['psrr'])<2 else 'MISSES'} the carrier PSRR)")
    print(f"  PSS speedup model/GT = {g['dt']/m['dt']:.1f}x   "
          f"(GT {g['dt']:.2f}s -> model {m['dt']:.2f}s)")
    print(f"  GT nonlinear sidebands: 2f={g['h2dbc']:.0f} dBc, 3f={g['h3dbc']:.0f} dBc "
          f"(model is linear -> ~floor; intrinsic-spur/HB asymmetry is a follow-up)")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fcar", type=float, default=304e6)
    ap.add_argument("--ampl", type=float, default=20e-3)
    ap.add_argument("--iload", default="121u")
    a = ap.parse_args()
    compare(a.fcar, a.ampl, a.iload)
