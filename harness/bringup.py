"""Bring-up / sanity diagnostic for a candidate ground-truth LDO subckt.

Reuses the DUT-generic bench. Reports the things that decide whether a new GT
architecture is a VALID, STABLE, DISTINCT LDO before it enters the experiment:

  DC      : Vout at the load corners (regulation sane, not collapsed/railed)
  Zout    : LF floor, resonance peak |Z|/freq/Q  (Q>~30 => marginally stable)
  PSRR    : LF rejection, worst-case, AND a non-minimum-phase score (measured
            phase vs the minimum-phase reconstruction of |PSRR| via Hilbert) --
            a large score flags a feedforward / RHP-zero PSRR (stresses shelf*Zout)
  Stable  : load-step ring decay ratio (2nd peak / 1st peak < 1 => damped/stable)

    python bringup.py --lib ground_truth/ldo_v1_nmos.lib --subckt ldo_v1_nmos
    python bringup.py --variant v1_nmos
"""
import argparse
import numpy as np
import bench
import ng


def _minphase_score(f, H):
    """How non-minimum-phase is H(f)? Reconstruct the minimum-phase phase from
    log|H| via the Hilbert relation on a log-uniform grid, compare to the actual
    unwrapped phase (after removing the best-fit linear delay). RMS deg of the
    residual. ~0 => minimum-phase; large => RHP zero / feedforward."""
    lf = np.log(f)
    g = np.linspace(lf[0], lf[-1], 1024)
    logmag = np.interp(g, lf, np.log(np.abs(H) + 1e-30))
    ph = np.interp(g, lf, np.unwrap(np.angle(H)))
    # minimum-phase phase = Hilbert transform of log-magnitude
    from scipy.signal import hilbert
    mp = -np.imag(hilbert(logmag))
    # remove constant + linear (delay) terms that Hilbert can't pin
    A = np.c_[np.ones_like(g), g]
    res = (ph - mp) - A @ np.linalg.lstsq(A, ph - mp, rcond=None)[0]
    return float(np.degrees(np.sqrt(np.mean(res ** 2))))


def _ring_decay(t, v, t0=bench.STEP_T0):
    """2nd-overshoot / 1st-overshoot amplitude after the step edge. <1 => damped."""
    m = (t >= t0 + 2e-9) & (t < t0 + 8e-6)
    tt, vv = t[m], v[m] - v[m].mean()
    # find local maxima
    pk = [i for i in range(1, len(vv) - 1) if vv[i] > vv[i-1] and vv[i] > vv[i+1] and vv[i] > 0]
    if len(pk) < 2:
        return 0.0
    return float(vv[pk[1]] / (vv[pk[0]] + 1e-15))


def bringup(libs, subckt, xparams=""):
    print(f"=== bring-up: {subckt}  xparams='{xparams}' ===")
    # DC
    il_s, v_s = bench.measure_dc_loadreg(libs, subckt, xparams=xparams)
    vout121 = np.interp(121e-6, il_s, v_s)
    print(f"DC  : Vout(1u)={v_s[0]*1e3:.1f}mV  Vout(121u)={vout121*1e3:.1f}mV  "
          f"Vout(500u)={v_s[-1]*1e3:.1f}mV  loadreg={(v_s[0]-v_s[-1])*1e3/0.499:.1f}mV/mA")
    # AC per corner
    print(f"{'load':>5} | {'Zlf':>7} {'Zpk':>7} {'fpk[MHz]':>8} {'Q~':>5} | "
          f"{'Plf[dB]':>7} {'Pworst':>6} {'NMPdeg':>6} | {'ringdec':>7}")
    ok = True
    for il in bench.LOADS:
        fz, Z = bench.measure_zout(libs, subckt, il, xparams=xparams)
        fp, H = bench.measure_psrr(libs, subckt, il, xparams=xparams)
        zmag = np.abs(Z); ipk = int(np.argmax(zmag * (fz < 2e7)))
        Q = zmag[ipk] / zmag[0]
        plf = -20*np.log10(np.abs(H[0]))
        pworst = (-20*np.log10(np.abs(H))).min()
        nmp = _minphase_score(fp, H)
        base = ng.amps(il)
        t, v = bench.measure_loadstep(libs, subckt, bench.LIN_FRAC*base, iload=base, xparams=xparams)
        rd = _ring_decay(t, v)
        flag = ""
        if Q > 30: flag += " HIQ"
        if rd > 1.0: flag += " UNSTABLE"
        if not (0.05 < vout121 < 1.04): flag += " VOUT?"
        ok = ok and not flag
        print(f"{il:>5} | {zmag[0]:7.2f} {zmag[ipk]:7.1f} {fz[ipk]/1e6:8.3f} {Q:5.1f} | "
              f"{plf:7.1f} {pworst:6.1f} {nmp:6.1f} | {rd:7.2f}{flag}")
    print("VERDICT:", "PASS (stable, sane)" if ok else "*** CHECK FLAGS ***")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default=None)
    ap.add_argument("--lib", default=None)
    ap.add_argument("--subckt", default=None)
    ap.add_argument("--xparams", default="")
    a = ap.parse_args()
    if a.variant:
        import variants
        v = variants.get(a.variant)
        bringup(v["libs"], v["subckt"], v["xparams"])
    else:
        bringup([a.lib], a.subckt, a.xparams)
