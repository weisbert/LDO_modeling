"""Characterize the ground-truth LDO: Zout(f) and PSRR(f) at a given load.

Zout : inject 1 A AC into vout, measure v(vout) -> transfer impedance.
PSRR : 1 V AC on vin, measure v(vout)/v(vin) -> supply-to-output gain.
"""
import argparse
import pathlib
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import ng

ROOT = ng.ROOT
RESULTS = ROOT / "results"
WORK = ROOT / "work"

AC_CMD = "set wr_singlescale\nset wr_vecnames\nac dec 40 10 100meg"


def tb_zout(iload):
    return f"""* Zout testbench
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC {iload} AC 0
Iac 0 vout AC 1
.control
{AC_CMD}
wrdata zout.dat vr(vout) vi(vout)
quit
.endc
.end
"""


def tb_psrr(iload):
    return f"""* PSRR testbench
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05 AC 1
Iload vout 0 DC {iload}
.control
{AC_CMD}
wrdata psrr.dat vr(vout) vi(vout)
quit
.endc
.end
"""


def characterize(iload):
    z = ng.run(ng.assemble(tb_zout(iload)), WORK / "zout", outputs=["zout.dat"])
    p = ng.run(ng.assemble(tb_psrr(iload)), WORK / "psrr", outputs=["psrr.dat"])
    if z["zout.dat"] is None:
        raise RuntimeError("Zout run produced no data:\n" + z["_stderr"][-2000:])
    if p["psrr.dat"] is None:
        raise RuntimeError("PSRR run produced no data:\n" + p["_stderr"][-2000:])
    fz, Z = ng.complex_col(z["zout.dat"][1])
    fp, H = ng.complex_col(p["psrr.dat"][1])
    return dict(fz=fz, Z=Z, fp=fp, H=H)


def report(d, iload):
    fz, Z = d["fz"], np.abs(d["Z"])
    fp, psrr_db = d["fp"], -20 * np.log10(np.abs(d["H"]))   # attenuation (higher=better)
    ipk = np.argmax(Z)
    print(f"--- Iload = {iload} ---")
    print(f"Zout: LF(10Hz)={Z[0]:.3g} ohm | peak={Z[ipk]:.3g} ohm @ {fz[ipk]:.4g} Hz"
          f" | HF(100MHz)={Z[-1]:.3g} ohm")
    print(f"PSRR: LF(10Hz)={psrr_db[0]:.1f} dB | worst={psrr_db.min():.1f} dB"
          f" @ {fp[np.argmin(psrr_db)]:.4g} Hz")
    return ipk


def plot(d, iload, ipk):
    RESULTS.mkdir(exist_ok=True)
    fz, Z = d["fz"], np.abs(d["Z"])
    fp, psrr_db = d["fp"], -20 * np.log10(np.abs(d["H"]))
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    ax[0].loglog(fz, Z)
    ax[0].plot(fz[ipk], Z[ipk], "ro")
    ax[0].set(title=f"Zout(f)  Iload={iload}", xlabel="Hz", ylabel="|Zout| (ohm)")
    ax[0].grid(True, which="both", alpha=.3)
    ax[1].semilogx(fp, psrr_db)
    ax[1].set(title=f"PSRR(f)  Iload={iload}", xlabel="Hz", ylabel="PSRR (dB, atten)")
    ax[1].grid(True, which="both", alpha=.3)
    fig.tight_layout()
    out = RESULTS / f"gt_iload_{iload}.png"
    fig.savefig(out, dpi=110)
    print(f"saved {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--iload", default="121u")
    a = ap.parse_args()
    d = characterize(a.iload)
    ipk = report(d, a.iload)
    plot(d, a.iload, ipk)
