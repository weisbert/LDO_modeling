"""ANALYSIS-ONLY: diagnose the Zout fidelity gap (Task 3). For a variant, per corner:
  * GT Zout: LF floor, in-band resonance peak (freq/mag)
  * CURRENT fit_zout: zrms(dB), peak freq, pkf=peakfit/peakGT (v3 pkf=5.01 bug)
  * AAA fit of Zout (conjugate samples -> real-coeff poles): zrms + dominant pole freqs
  * AAA-INITIALIZED re-fit of the SAME zmodel topology (seed L_a/L_b from the AAA
    resonances) -> does better init alone fix pkf, or is a richer realization needed?
  * passivity: min Re(Z) over band for GT and fitted (Re<0 => non-passive)

    python analyze_zout.py --variant v3_miller
"""
import argparse
import numpy as np
from scipy.optimize import least_squares
import fit_model as fm

TWO_PI = 2 * np.pi


def _peak(f, Z):
    lo = f < 1e7
    i = int(np.argmax(np.abs(Z) * lo))
    return f[i], np.abs(Z[i])


def _zrms(f, Zmod, Z):
    return float(np.sqrt(np.mean((20 * np.log10(np.abs(Zmod) / np.abs(Z))) ** 2)))


def _aaa_poles(f, Z, max_terms=12):
    from scipy.interpolate import AAA
    s = 1j * TWO_PI * f
    r = AAA(np.concatenate([s, np.conj(s)]), np.concatenate([Z, np.conj(Z)]), max_terms=max_terms)
    return r


def _aaa_res_freqs(r, f):
    """In-band complex-pole resonance frequencies from an AAA fit, by |residue| weight."""
    pol = r.poles()
    try:
        res = r.residues()
    except Exception:
        res = np.ones(len(pol), complex)
    out = []
    for p, rr in zip(pol, res):
        fo = abs(p) / TWO_PI
        if p.real < 0 and abs(p.imag) > 1e-3 * abs(p.real) and f[0] * 0.3 < fo < f[-1] * 3:
            out.append((fo, abs(rr)))
    out.sort(key=lambda t: -t[1])
    return [fo for fo, _ in out]


def _refit_seeded(f, Z, C, RC, fres):
    """Re-fit the zmodel (R_a,L_a,R_pl,R_b,L_b) seeded from AAA resonance freqs."""
    R0 = float(np.abs(Z[0]))
    fr1 = fres[0] if len(fres) >= 1 else _peak(f, Z)[0]
    fr2 = fres[1] if len(fres) >= 2 else fr1 * 4
    La0 = 1.0 / ((TWO_PI * fr1) ** 2 * C)
    Lb0 = 1.0 / ((TWO_PI * fr2) ** 2 * C)

    def zmod(p):
        Ra, La, Rpl, Rb, Lb = np.exp(p)
        return fm.zmodel(f, Ra, La, Rpl, Rb, Lb)

    def resid(p):
        r = np.log(zmod(p)) - np.log(Z)
        return np.concatenate([r.real, r.imag])
    p0 = np.log([max(R0, 1e-2), La0, R0 * 30, R0 * 3, Lb0])
    lo = np.log([R0 / 10, La0 / 50, R0 / 3, R0 / 10, Lb0 / 50])
    hi = np.log([R0 * 10, La0 * 50, 1e9, 1e9, Lb0 * 50])
    s = least_squares(resid, p0, bounds=(lo, hi), method="trf", max_nfev=8000)
    Zm = zmod(s.x)
    return _zrms(f, Zm, Z), _peak(f, Zm)[0], np.exp(s.x)


def analyze(vkey):
    fm.load(vkey)
    C, RC = fm.C, fm.RC
    print(f"\n{'='*86}\nVARIANT {vkey}   (Cout={C*1e12:.0f}pF ESR={RC:.2f})\n{'='*86}")
    print(f"{'load':>5} | {'Zlf':>7} {'pkGT[MHz]':>9} | {'cur zrms':>8} {'pkf':>5} | "
          f"{'AAA zrms':>8} {'AAA fres[MHz]':>22} | {'seed zrms':>9} {'seed pkf':>8} | {'minRe GT/fit':>14}")
    for il in fm.LOADS:
        gz = fm.ref[f"z_{il}"]; f = gz[:, 0]; Z = gz[:, 1] + 1j * gz[:, 2]
        fpkGT, _ = _peak(f, Z)
        zf = fm.fit_zout(f, Z); Zcur = fm.zmodel(f, *zf)
        cur_zrms = _zrms(f, Zcur, Z); fpkcur, _ = _peak(f, Zcur)
        r = _aaa_poles(f, Z)
        Zaaa = r(1j * TWO_PI * f)
        aaa_zrms = _zrms(f, Zaaa, Z)
        fres = _aaa_res_freqs(r, f)
        seed_zrms, fpkseed, _ = _refit_seeded(f, Z, C, RC, fres)
        minRe_gt = float(np.min(Z.real)); minRe_fit = float(np.min(Zcur.real))
        fres_s = " ".join(f"{x:.3f}" for x in fres[:3]) if fres else "(none)"
        print(f"{il:>5} | {abs(Z[0]):7.2f} {fpkGT/1e6:9.3f} | {cur_zrms:8.2f} {fpkcur/fpkGT:5.2f} | "
              f"{aaa_zrms:8.2f} {fres_s:>22} | {seed_zrms:9.2f} {fpkseed/fpkGT:8.2f} | "
              f"{minRe_gt:6.2f}/{minRe_fit:6.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="v3_miller")
    analyze(ap.parse_args().variant)
