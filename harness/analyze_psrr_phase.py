"""ANALYSIS-ONLY (no model emit): de-risk the non-min-phase PSRR / migrating-Zout
PHASE task. For a variant, per load corner:
  * fit Zout exactly as fit_model does (R_a,L_a,R_pl,R_b,L_b) -> Zmodel
  * form the PSRR coupling-current target  i_c = H_gt / Zmodel
  * AAA-fit i_c (with conjugate samples so poles are conjugate-symmetric / real-coeff)
  * report poles -> # real vs # complex-conjugate pairs, freq/Q, residues
  * decompose each conj pair to the realizable section (b0,b1,a1,a2) and the
    physical R/L/C+VCCS element values (Csec=1p) -> sanity-check signs/magnitudes
  * report the current min-phase SHELF residual (e_shelf) and the SK fallback path
  * also AAA-fit Zout_gt -> true resonance vs the current fit's resonance (pkf bug)
Prints a compact per-corner report + a cross-corner structural-stability verdict.

    python analyze_psrr_phase.py --variant v4_ffpsrr
"""
import argparse
import numpy as np
from scipy.interpolate import AAA
import fit_model as fm

TWO_PI = 2 * np.pi


def _aaa_conj(f, y, max_terms=12):
    """AAA on the imaginary axis with conjugate samples so the interpolant has
    real coefficients (conjugate-symmetric poles). Returns (poles, residues)."""
    s = 1j * TWO_PI * f
    x = np.concatenate([s, np.conj(s)])
    yy = np.concatenate([y, np.conj(y)])
    r = AAA(x, yy, max_terms=max_terms)
    poles = r.poles()
    # residues: scipy AAA exposes .residues() aligned with .poles()
    try:
        res = r.residues()
    except Exception:
        res = np.full(len(poles), np.nan, complex)
    return poles, res


def _group_poles(poles, res, tol=1e-3):
    """Split into real poles and conjugate pairs (keep one of each pair)."""
    reals, pairs = [], []
    used = np.zeros(len(poles), bool)
    for i, p in enumerate(poles):
        if used[i]:
            continue
        if abs(p.imag) <= tol * abs(p.real) + 1.0:
            reals.append((p.real, res[i]))
            used[i] = True
        else:
            # find conjugate partner
            j = None
            for k in range(len(poles)):
                if not used[k] and k != i and abs(poles[k] - np.conj(p)) < tol * abs(p) + 1.0:
                    j = k
                    break
            pairs.append((p, res[i], (poles[j] if j is not None else np.conj(p)),
                          (res[j] if j is not None else np.conj(res[i]))))
            used[i] = True
            if j is not None:
                used[j] = True
    return reals, pairs


def _pair_section(p, r):
    """Conjugate pair -> realizable 2nd-order section (b0+b1 s)/(1+a1 s+a2 s^2)
    plus physical elements with Csec=1p:  Rsec=a1/C, Lsec=a2/C, Gb0=b0, Gb1=b1/a1."""
    B1 = 2 * r.real
    B0 = -2 * (r * np.conj(p)).real
    A1 = -2 * p.real
    A0 = (abs(p)) ** 2
    a2, a1, b0, b1 = 1 / A0, A1 / A0, B0 / A0, B1 / A0
    C = 1e-12
    R, L = a1 / C, a2 / C
    w0 = abs(p)
    Q = w0 / A1 if A1 > 0 else np.inf
    return dict(b0=b0, b1=b1, a1=a1, a2=a2, R=R, L=L, C=C, Gb0=b0, Gb1=b1 / a1 if a1 else 0,
                f0=w0 / TWO_PI, Q=Q, stable=(p.real < 0))


def analyze(vkey):
    fm.load(vkey)
    print(f"\n{'='*78}\nVARIANT {vkey}\n{'='*78}")
    struct = {}
    for il in fm.LOADS:
        gz = fm.ref[f"z_{il}"]; fz = gz[:, 0]; Z = gz[:, 1] + 1j * gz[:, 2]
        gp = fm.ref[f"p_{il}"]; fp = gp[:, 0]; H = gp[:, 1] + 1j * gp[:, 2]
        R_a, L_a, R_pl, R_b, L_b = fm.fit_zout(fz, Z)
        Zmod = fm.zmodel(fp, R_a, L_a, R_pl, R_b, L_b)
        ic = H / Zmod                                    # PSRR coupling-current target

        # current shelf residual + SK fallback verdict
        G_shelf, e_shelf = fm._shelf(fp, H, R_a, L_a, R_pl, R_b, L_b)
        sk = fm._sk_fit(1j * TWO_PI * fp, ic, fm.NPS)

        # AAA on i_c (real-coeff via conjugate samples)
        pol, res = _aaa_conj(fp, ic)
        # keep stable poles in/near band only
        keep = [(p, r) for p, r in zip(pol, res)
                if p.real < 0 and fp[0] * 0.1 < abs(p) / TWO_PI < fp[-1] * 10]
        reals, pairs = _group_poles(np.array([p for p, _ in keep]),
                                    np.array([r for _, r in keep]))
        struct[il] = (len(reals), len(pairs))

        # phase error of the current shelf vs GT (the thing we must reduce)
        Hsh = fm.psrr_model(fp, R_a, L_a, R_pl, R_b, L_b, G_shelf)
        sel = fp >= 1e3
        ph_shelf = np.degrees(np.sqrt(np.mean((np.angle(Hsh / H)[sel]) ** 2)))

        print(f"\n--- load {il} ---  Zfit: R_a={R_a:.3g} L_a={L_a*1e6:.3g}uH "
              f"R_pl={R_pl:.3g} R_b={R_b:.3g}")
        print(f"  shelf e_shelf={e_shelf:.3f}  (>0.05 -> non-min-phase path) "
              f"shelf PSRR phase RMS={ph_shelf:.1f} deg   SK fallback={'COMPLEX(None)' if sk is None else 'real-ok'}")
        print(f"  AAA i_c structure: {len(reals)} real pole(s), {len(pairs)} conj pair(s)")
        for p, r, pc, rc in pairs:
            sec = _pair_section(p, r)
            print(f"    pair f0={sec['f0']/1e6:.3f}MHz Q={sec['Q']:.2f} stable={sec['stable']} "
                  f"| b0={sec['b0']:+.3e} b1={sec['b1']:+.3e} a1={sec['a1']:.3e} a2={sec['a2']:.3e}"
                  f" | R={sec['R']:.3g} L={sec['L']:.3g} Gb0={sec['Gb0']:+.3e} Gb1={sec['Gb1']:+.3e}")
        for pr, rr in reals:
            print(f"    real w/2pi={(-pr)/TWO_PI/1e6:+.3f}MHz  G={(-rr/pr).real:+.3e}")

        # Zout true resonance via AAA (the pkf=5.01 migrating-resonance check)
        zp, zr = _aaa_conj(fz, Z, max_terms=10)
        zpairs = [p for p in zp if abs(p.imag) > 1e-3 * abs(p.real) and p.real < 0]
        ztrue = (min(abs(p) for p in zpairs) / TWO_PI) if zpairs else np.nan
        ipk = int(np.argmax(np.abs(Z) * (fz < 1e7)))
        zfitpk = fz[int(np.argmax(np.abs(Zmod) * (fz < 1e7)))]
        print(f"  Zout resonance: GT peak={fz[ipk]/1e6:.3f}MHz  AAA-pole={ztrue/1e6 if ztrue==ztrue else float('nan'):.3f}MHz"
              f"  current-fit peak={zfitpk/1e6:.3f}MHz  (ratio {zfitpk/fz[ipk]:.2f})")

    print(f"\n>>> {vkey} cross-corner i_c structure (real,pairs) per corner: "
          + "  ".join(f"{il}:{struct[il]}" for il in fm.LOADS))
    counts = set(struct.values())
    print(f">>> structural stability: {'STABLE (same across corners)' if len(counts)==1 else 'VARIES -> pick max envelope'}; "
          f"recommend N1_real={max(c[0] for c in struct.values())} N2_pairs={max(c[1] for c in struct.values())}")


def _ic_model(p, f, N1, N2):
    """i_c = G0 + sum_i g_i/(1+s/w_i) + sum_j (b0+b1 s)/(1+s/(Q w0)+(s/w0)^2).
    p layout: [G0] + N1*[g_i, logw_i] + N2*[b0, b1, logw0, logQ]."""
    s = 1j * TWO_PI * f
    ic = np.full_like(s, p[0])
    k = 1
    for _ in range(N1):
        g, lw = p[k], p[k + 1]; k += 2
        ic = ic + g / (1 + s / np.exp(lw))
    for _ in range(N2):
        b0, b1, lw0, lQ = p[k], p[k + 1], p[k + 2], p[k + 3]; k += 4
        w0 = np.exp(lw0); Q = np.exp(lQ)
        ic = ic + (b0 + b1 * s) / (1 + s / (Q * w0) + (s / w0) ** 2)
    return ic


def _init_pq(fp, ic, N1, N2):
    """Initialize the (N1 real + N2 complex) bank from a pruned AAA fit of ic."""
    pol, res = _aaa_conj(fp, ic)
    band = (abs(pol) / TWO_PI > fp[0] * 0.3) & (abs(pol) / TWO_PI < fp[-1] * 3) & (pol.real < 0)
    pol, res = pol[band], res[band]
    reals, pairs = _group_poles(pol, res)
    # rank reals by |residue/pole| (DC contribution); pairs by |b0|
    reals = sorted(reals, key=lambda pr: -abs((pr[1] / pr[0])))
    pairsd = sorted([_pair_section(p, r) for p, r, *_ in pairs], key=lambda d: -abs(d["b0"]))
    p = [float(ic[-1].real)]
    for i in range(N1):
        if i < len(reals):
            pr, rr = reals[i]; p += [float((-rr / pr).real), float(np.log(max(-pr, 1e3)))]
        else:
            p += [0.0, float(np.log(TWO_PI * fp[len(fp) // 2]))]
    for j in range(N2):
        if j < len(pairsd):
            d = pairsd[j]
            p += [float(d["b0"]), float(d["b1"]), float(np.log(max(d["f0"] * TWO_PI, 1e4))),
                  float(np.log(min(max(d["Q"], 0.5), 40)))]
        else:
            p += [0.0, 0.0, float(np.log(TWO_PI * fp[len(fp) // 2])), float(np.log(2.0))]
    return np.array(p)


def polish(vkey, N1, N2):
    fm.load(vkey)
    wlo, whi = None, None
    print(f"\n### POLISH {vkey}  N1_real={N1} N2_complex={N2} ###")
    for il in fm.LOADS:
        gz = fm.ref[f"z_{il}"]; fz = gz[:, 0]; Z = gz[:, 1] + 1j * gz[:, 2]
        gp = fm.ref[f"p_{il}"]; fp = gp[:, 0]; H = gp[:, 1] + 1j * gp[:, 2]
        zf = fm.fit_zout(fz, Z)
        Zmod = fm.zmodel(fp, *zf)
        ic = H / Zmod
        p0 = _init_pq(fp, ic, N1, N2)
        lo = np.full_like(p0, -np.inf); hi = np.full_like(p0, np.inf)
        # bound the log-frequency / log-Q params
        k = 1
        flo, fhi = np.log(TWO_PI * fp[0] / 10), np.log(TWO_PI * fp[-1] * 10)
        for _ in range(N1):
            lo[k + 1], hi[k + 1] = flo, fhi; k += 2
        for _ in range(N2):
            lo[k + 2], hi[k + 2] = flo, fhi
            lo[k + 3], hi[k + 3] = np.log(0.5), np.log(40.0); k += 4

        def resid(p):
            r = np.log(_ic_model(p, fp, N1, N2)) - np.log(ic)
            wgt = 1.0 / (np.abs(np.log(np.abs(ic)) ) + 1.0)
            return np.concatenate([r.real, r.imag])
        from scipy.optimize import least_squares
        s = least_squares(resid, p0, bounds=(lo, hi), method="trf", max_nfev=20000)
        icm = _ic_model(s.x, fp, N1, N2)
        sel = fp >= 1e3
        magdb = 20 * np.log10(np.abs(icm[sel]) / np.abs(ic[sel]))
        phd = np.degrees(np.angle(icm[sel] / ic[sel]))
        # also the PSRR-as-seen (icm*Zmod vs H) which is what score grades
        Hm = icm * Zmod
        pph = np.degrees(np.sqrt(np.mean((np.angle(Hm[sel] / H[sel])) ** 2)))
        pmag = np.sqrt(np.mean((20 * np.log10(np.abs(Hm[sel]) / np.abs(H[sel]))) ** 2))
        print(f"  {il}: PSRR mag-RMS={pmag:5.2f}dB  PSRR phase-RMS={pph:5.1f}deg   "
              f"(ic mag {np.sqrt(np.mean(magdb**2)):.2f}dB / ic phase {np.sqrt(np.mean(phd**2)):.1f}deg)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="v4_ffpsrr")
    ap.add_argument("--polish", default="", help="N1,N2 e.g. 3,1 (sweep with ';': 3,1;3,2)")
    a = ap.parse_args()
    if a.polish:
        for spec in a.polish.split(";"):
            n1, n2 = (int(x) for x in spec.split(","))
            polish(a.variant, n1, n2)
    else:
        analyze(a.variant)
