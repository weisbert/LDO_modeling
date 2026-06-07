"""Place the Zout resonance peak at ~1-2MHz with a visible (moderate-Q) peak.
Knobs: cout (output pole) and cg (gate/OTA-output pole). Bringing the two poles
together near UGB raises Q and creates the peak."""
import numpy as np
import ng

AC = "set wr_singlescale\nset wr_vecnames\nac dec 60 100 200meg"


def zout(cout, cg, iload="121u"):
    tb = f"""Xldo vin vout ldo_gt cout={cout} cg={cg}
Vin vin 0 DC 1.05
Iload vout 0 DC {iload} AC 0
Iac 0 vout AC 1
.control
{AC}
wrdata zout.dat vr(vout) vi(vout)
quit
.endc
.end
"""
    r = ng.run(ng.assemble(tb), ng.ROOT / "work" / "tune", outputs=["zout.dat"])
    if r["zout.dat"] is None:
        raise RuntimeError(r["_stderr"][-1500:])
    f, Z = ng.complex_col(r["zout.dat"][1])
    return f, np.abs(Z)


print(f"{'cout':>6} {'cg':>6} {'fpeak[MHz]':>10} {'Zpk':>7} {'Zlf':>6}"
      f" {'pk/lf':>6} {'Z@8M':>7} {'Z@16M':>7} {'Z@24M':>7}")
for cout in ["1n"]:
    for cg in ["0.2p", "0.3p", "0.4p", "0.5p", "0.7p", "1p", "1.5p"]:
        f, m = zout(cout, cg)
        i = np.argmax(m)
        z8, z16, z24 = (np.interp(x, f, m) for x in (8e6, 16e6, 24e6))
        print(f"{cout:>6} {cg:>6} {f[i]/1e6:10.3f} {m[i]:7.1f} {m[0]:6.2f}"
              f" {m[i]/m[0]:6.1f} {z8:7.2f} {z16:7.2f} {z24:7.2f}")
