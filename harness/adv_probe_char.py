"""Feature extractor for the ADVERSARIAL overfit-probe GT DUTs (HANDOFF_ADVERSARIAL_
OVERFIT_PROBE.md). NOT part of the scored pipeline -- a fast, standalone vetting tool
that pulls the ONE adversarial feature each probe DUT must exhibit, so a draft netlist
can be (a) confirmed CONVERGENT and (b) confirmed to actually hit its falsifiable GT
target BEFORE it enters the experiment. (A non-converging or off-target GT is a build
bug, not a finding.)

Reuses the proven decks: current-path mirrors harness/isrc_char.py (DC sweep / AC
dId/dVdd / per-T Idc); voltage-path mirrors harness/bench.py (Zout / PSRR / load-step).

  python adv_probe_char.py isrc <lib> <subckt> <pol> [vc]
  python adv_probe_char.py ldo  <lib> <subckt>
"""
import sys
import pathlib
import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ng                                                              # noqa: E402

VDD = 1.05
ISRC_TEMPS_FIT = (-40.0, 55.0, 125.0)      # the 3 default fit temps
ISRC_TEMPS_HELD = (25.0, 85.0)             # interior held-out temps (B1)
ISRC_TEMPS_ALL = (-40.0, 25.0, 55.0, 85.0, 125.0)


# ----------------------------------------------------------------- current path
def _isrc_run(lib, name, tag, body, outfile):
    r = ng.run(ng.assemble(body, libs=[lib]), ng.ROOT / "work_adv" / name / tag,
               outputs=(outfile,))
    data = r.get(outfile)
    if data is None or r["_rc"] != 0:
        raise RuntimeError(f"{name}/{tag} ngspice rc={r['_rc']}:\n{r['_stderr'][-1500:]}")
    return data[1]


def _isrc_head(sub, temp):
    return f".options temp={temp:g}\nVdd vdd 0 DC {VDD}\nXd vdd out {sub}\n"


def isrc_iv(lib, sub, vc, temp):
    body = _isrc_head(sub, temp) + (f"Vout out 0 DC {vc:g}\n.control\n"
                                    f"dc Vout 0 {VDD} 0.005\nwrdata iv.data i(vout)\n.endc\n.end\n")
    a = _isrc_run(lib, sub, f"iv{int(temp)}", body, "iv.data")
    return a[:, 0], np.abs(a[:, 1])


def isrc_didvdd(lib, sub, vo, temp=55.0):
    head = _isrc_head(sub, temp).replace(f"Vdd vdd 0 DC {VDD}\n", f"Vdd vdd 0 DC {VDD} AC 1\n")
    body = head + (f"Vout out 0 DC {vo:g} AC 0\n.control\n"
                   f"ac dec 20 10 500meg\nwrdata p.data i(vout)\n.endc\n.end\n")
    a = _isrc_run(lib, sub, f"p{vo:.2f}", body, "p.data")
    g = a[0, 1] + 1j * a[0, 2]
    return float(g.real)                               # LF dIout/dVdd [S], signed (probe convention)


def isrc_idc_at(lib, sub, vc, temp):
    vo, I = isrc_iv(lib, sub, vc, temp)
    return float(np.interp(vc, vo, I))


def isrc_features(lib, sub, pol, vc=0.5):
    """Print Idc/knee + the three adversarial-probe features and return a dict."""
    vo, I = isrc_iv(lib, sub, vc, 55.0)
    Iplat = float(np.median(np.sort(I)[-8:]))
    on = I >= 0.9 * Iplat
    klo, khi = (float(vo[on].min()), float(vo[on].max())) if on.any() else (np.nan, np.nan)
    idc = float(np.interp(vc, vo, I))

    # B1: Idc(T) at the 3 fit temps + the 2 held-out interior temps -> straight-line miss
    idcT = {T: isrc_idc_at(lib, sub, vc, T) for T in ISRC_TEMPS_ALL}
    Tk = np.array(ISRC_TEMPS_FIT) + 273.15
    yfit = np.array([idcT[T] for T in ISRC_TEMPS_FIT])
    b, a = np.polyfit(Tk, yfit, 1)                     # 3-temp line
    held_miss = {}
    for T in ISRC_TEMPS_HELD:
        lin = a + b * (T + 273.15)
        held_miss[T] = float((idcT[T] - lin) / (abs(idcT[T]) + 1e-30) * 100)

    # B3: dIout/dVdd sign at vc-0.2 / vc / vc+0.2 (off-compliance PSRR sign)
    gd = {}
    for v in (round(vc - 0.2, 3), vc, round(vc + 0.2, 3)):
        if 0.0 <= v <= VDD:
            gd[v] = isrc_didvdd(lib, sub, v)

    # B4: IV plateau (median over compliance window) at cold/nom/hot -> knee moves with T
    plat = {}
    for T in ISRC_TEMPS_FIT:
        v2, I2 = isrc_iv(lib, sub, vc, T)
        sel = (v2 >= klo) & (v2 <= khi) if on.any() else np.ones_like(v2, bool)
        plat[T] = float(np.median(I2[sel])) if sel.any() else float("nan")

    print(f"=== isrc adv-features: {sub}  pol={pol} vc={vc} ===")
    print(f"Idc(55C)={idc*1e6:.4f}uA  knee=[{klo:.3f},{khi:.3f}]V  Iplat={Iplat*1e6:.4f}uA")
    print("Idc(T):  " + "  ".join(f"{int(T):+4d}C={idcT[T]*1e6:.4f}u" for T in ISRC_TEMPS_ALL))
    print(f"  B1 held-out straight-line miss: 25C={held_miss[25.0]:+.2f}%  85C={held_miss[85.0]:+.2f}%"
          f"   (expose if |miss|>5%)")
    print("  B3 dIout/dVdd [nS]:  " + "  ".join(f"Vo={v}:{gd[v]*1e9:+.3f}" for v in sorted(gd)))
    flip = (min(gd.values()) < 0) and (max(gd.values()) > 0) if len(gd) >= 2 else False
    print(f"     sign-flip across compliance: {flip}   (B3 expose target)")
    print("  B4 IV plateau [uA]:  " + "  ".join(f"{int(T):+4d}C={plat[T]*1e6:.4f}" for T in ISRC_TEMPS_FIT))
    sp = [plat[T] for T in ISRC_TEMPS_FIT]
    print(f"     plateau spread cold->hot: {(max(sp)-min(sp))/(np.mean(sp)+1e-30)*100:+.1f}%")
    return dict(idc=idc, knee=(klo, khi), idcT=idcT, held_miss=held_miss, gd=gd, plat=plat, flip=flip)


# ----------------------------------------------------------------- voltage path
def ldo_features(lib, sub):
    """Per-corner Zout LF/peak/Q/fpk + PSRR LF/-3dB-corner + load-step ring/asymmetry.
    qbow target: Q non-monotonic across 20u/121u/250u. pzmig target: PSRR -3dB corner
    CONVEX in ln(iload). classab target: big/slew step droop!=recovery (asymmetry)."""
    import bench
    print(f"=== ldo adv-features: {sub} ===")
    print(f"{'load':>5} | {'Zlf':>7} {'Zpk':>8} {'fpk[MHz]':>8} {'Q~':>6} | "
          f"{'Plf[dB]':>7} {'Pf3dB[MHz]':>10} | {'ringdec':>7}")
    out = {}
    for il in bench.LOADS:
        fz, Z = bench.measure_zout(lib, sub, il)
        fp, H = bench.measure_psrr(lib, sub, il)
        zmag = np.abs(Z); ipk = int(np.argmax(zmag * (fz < 2e7)))
        Q = zmag[ipk] / zmag[0]
        hmag = np.abs(H)
        # PSRR roll-off corner: lowest f where |PSRR| has dropped 3 dB below its LF value
        lf = hmag[0]
        below = np.where(hmag <= lf / np.sqrt(2))[0]
        fc = float(fp[below[0]]) if below.size else float("nan")
        base = ng.amps(il)
        t, v = bench.measure_loadstep(lib, sub, bench.LIN_FRAC * base, iload=base)
        # ring decay
        m = (t >= bench.STEP_T0 + 2e-9) & (t < bench.STEP_T0 + 8e-6)
        vv = v[m] - v[m].mean()
        pk = [i for i in range(1, len(vv) - 1) if vv[i] > vv[i-1] and vv[i] > vv[i+1] and vv[i] > 0]
        rd = float(vv[pk[1]] / (vv[pk[0]] + 1e-15)) if len(pk) >= 2 else 0.0
        out[il] = dict(zlf=float(zmag[0]), zpk=float(zmag[ipk]), fpk=float(fz[ipk]), Q=float(Q),
                       plf=float(-20*np.log10(lf)), pf3=fc, rd=rd)
        print(f"{il:>5} | {zmag[0]:7.3f} {zmag[ipk]:8.2f} {fz[ipk]/1e6:8.3f} {Q:6.2f} | "
              f"{-20*np.log10(lf):7.1f} {fc/1e6:10.3f} | {rd:7.2f}")
    Qs = [out[il]["Q"] for il in bench.LOADS]
    nonmono = (Qs[1] > Qs[0] * 1.3) and (Qs[1] > Qs[2] * 1.3)
    print(f"  qbow: Q = {Qs[0]:.2f}/{Qs[1]:.2f}/{Qs[2]:.2f} -> non-monotonic(mid-peak): {nonmono}")
    # pzmig: convexity of ln(fc) vs ln(iload)
    fcs = np.array([out[il]["pf3"] for il in bench.LOADS])
    if np.all(np.isfinite(fcs)) and np.all(fcs > 0):
        u = np.log([ng.amps(il) for il in bench.LOADS]); lf = np.log(fcs)
        # straight line through endpoints, midpoint deviation (convex if mid sits ABOVE? we want
        # the 3-pt quad to overshoot -> mid far from the endpoint line)
        linmid = lf[0] + (lf[2] - lf[0]) * (u[1] - u[0]) / (u[2] - u[0])
        dev = (lf[1] - linmid)
        print(f"  pzmig: PSRR corner [MHz]={fcs/1e6}  ln-mid deviation from endpoint line={dev:+.3f}"
              f"  (|dev|>0.5 => strongly non-log-linear)")
    return out


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "isrc":
        lib, sub, pol = sys.argv[2], sys.argv[3], sys.argv[4]
        vc = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
        isrc_features(pathlib.Path(lib), sub, pol, vc)
    else:
        ldo_features([pathlib.Path(sys.argv[2])], sys.argv[3])
