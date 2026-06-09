"""Fit the behavioral model element values to the GT reference and EMIT
model/ldo_model.lib. Topology (all linear/passive -> HB/PSS-robust, no laplace_nd):

  Zout(s) = (R_a + sL_a)  ||  (R_c + 1/sC)      C=1n, R_c=0.5 FIXED (physical cap)
            LF floor = R_a ; resonance @ 1/2pi.sqrt(L_a.C) ; HF -> cap rolloff
  PSRR : i_couple(s) = G0 + sum_i Gi/(1+s/wi) [real bank] + (b0+b1 s)/(1+s/(Q w0)+(s/w0)^2)
            [one signed COMPLEX-conjugate 2nd-order section], injected into vout, x Zout.
            Real bank = min-phase shelf (1 sec) / non-min-phase real fit (3 sec); the
            complex section carries genuine non-min-phase / notch PHASE (V4/V3). LP filter
            resistors are NOISELESS VCCS-conductances (signal path -> no thermal noise).
  Noise: series voltage-noise Vn in branch A -> vout via Z_C/(Z_A+Z_C) divider
            => flat (+1/f) at LF, peak at resonance, Cout rolloff  (matches GT shape)
  DC   : Vreg behavioral source w/ load-reg + line-reg

Per-load corner {R_a, L_a, Rpass, g_lf, wz, Vn_white} fitted; model selects by `iload`.
"""
import argparse
from dataclasses import dataclass
import numpy as np
from scipy.optimize import least_squares
import ng

TWO_PI = 2 * np.pi
ref = None
LOADS = []
NOMINAL = "121u"   # nominal load corner KEY; set per-variant in load() = middle corner
                   # (contract: 3 corners low/nom/high). Replaces the previously hardcoded
                   # "121u" so the harness/GUI work for any corner naming / OP.
VREF = 1.05        # nominal vin the PSRR path is DC-referenced to. EMIT-TIME ONLY -- the
                   # analytic fit is OP-agnostic -- so the GUI profile can override it
                   # (CADENCE_EXTRACTION.md "3 things to report" #1) with no fit change.
C, RC = 1e-9, 0.5          # output cap / ESR -- AUTO-EXTRACTED per variant in load()


_amps = ng.amps   # canonical corner-key->amps (see ng.amps); module alias for the emit f-strings


def fit_cout_esr():
    """Auto-extract the physical Cout/ESR (load-independent) from the CAPACITIVE
    HF TAIL of the wideband nominal Zout. Above all resonances the L-branch is
    high-Z and Z -> ESR + 1/(jwC), so ESR = Re(Z) and C = -1/(w*Im(Z)). Reading
    the tail directly (not a full fit) is robust to multi-pole mid-band shapes
    (a full fit gets pulled off, e.g. V3 -> 1606pF). Replaces the old hardcoded
    1n/0.5 so the fitter works on LDOs with any output cap."""
    g = ref[f"z_{NOMINAL}_hf"] if f"z_{NOMINAL}_hf" in ref.files else ref[f"z_{NOMINAL}"]
    f = g[:, 0]; Z = g[:, 1] + 1j * g[:, 2]
    # Cout from the CAPACITIVE band (phase < -45deg, post-resonance): there
    # Z ~ 1/(jwC) so C = -1/(w*Im Z). Using phase selection (not just the HF tail)
    # keeps C right even when a large ESR floors the tail (e.g. V1 ESR=30 -> Im tiny).
    # Cout from the CAPACITIVE band (phase < -45deg, post-resonance): there
    # Z ~ 1/(jwC) so C = -1/(w*Im Z). Using phase selection (not just the HF tail)
    # keeps C right even when a large ESR floors the tail (e.g. V1 ESR=30 -> Im tiny).
    # KNOWN LIMITATION: when ESR >> output-R the output cap is electrically near-invisible
    # (cap branch always higher-Z than the output R) so Cout/ESR are weakly identifiable;
    # V1 then reads ~381pF for a true 1nF. A joint Rp||(ESR+1/jwC) LS was tried and
    # rejected -- it is underdetermined here (sent V2's invisible cap to 1pF/1e269F). Since
    # an invisible cap barely affects Zout, the residual is small; left as future work.
    cap = np.angle(Z) < -np.pi / 4
    sel = cap if cap.sum() >= 3 else (f > 0.3 * f[-1])
    Cc = float(np.median(-1.0 / (TWO_PI * f[sel] * Z[sel].imag)))
    # ESR from the HF-tail real part (cap impedance smallest there -> Re Z -> ESR)
    tail = f > 0.3 * f[-1]
    Rc = float(max(np.median(Z[tail].real), 1e-3))
    return Cc, Rc


def load(vkey="base", nominal=None, vref=None):
    """Load a variant reference and auto-extract its physical Cout/ESR.
    nominal: nominal corner KEY (default = middle of `loads`, the contract's nom corner).
    vref:    nominal vin reference for the emitted model (default = keep module VREF=1.05).
    Both default to the legacy behavior, so existing callers are unchanged."""
    global ref, LOADS, NOMINAL, VREF, C, RC
    ref = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
    LOADS = [str(x) for x in ref["loads"]]
    NOMINAL = nominal if nominal is not None else LOADS[len(LOADS) // 2]
    if vref is not None:
        VREF = float(vref)
    C, RC = fit_cout_esr()
    if "meta_cout" in ref.files:
        print(f"Cout/ESR auto-extracted: {C*1e12:7.1f}pF / {RC:6.3f}ohm   "
              f"(true {float(ref['meta_cout'])*1e12:7.1f}pF / {float(ref['meta_esr']):6.3f}ohm)")
    return ref


def zmodel(f, R_a, L_a, R_pl=1e12, R_b=1e12, L_b=1e-12):
    """Zout = (R_a + sL_a||R_pl) || (R_b + sL_b) || (ESR + 1/sC).
    - R_pl: damping across L_a; R_pl->inf = classic resonant (R_a+sL_a)||C peak,
      finite = resistive PLATEAU (high-gain LDO open-loop Rout, e.g. V3).
    - branch B (R_b + sL_b): an OPTIONAL 2nd parallel R-L branch for a 2nd
      resonance / different HF roll-off (multi-pole Zout). R_b->inf disables it
      (1-branch recovered), so it is added per-variant only when it helps."""
    s = 1j * TWO_PI * f
    ZA = R_a + (s * L_a * R_pl) / (s * L_a + R_pl)
    ZB = R_b + s * L_b
    ZC = RC + 1.0 / (s * C)
    return 1.0 / (1.0 / ZA + 1.0 / ZB + 1.0 / ZC)


def fit_zout(f, Z):
    """Fit R_a, L_a of (R_a+sL_a)||(ESR+1/sC) to Zout. Robust to flat/overdamped
    Zout (no resonance) and to resonances anywhere in band: bounded TRF (can't run
    away), peak-adaptive weighting, and a peak-significance gate so a flat response
    is fit as ~R (L pole pushed to band-top) instead of a spurious huge resonance."""
    R0 = float(np.abs(Z[0]))
    mag = np.abs(Z)
    fpk = f[int(np.argmax(mag))]
    Q = mag.max() / R0
    w = (1 + 4 * ((f > 0.5 * fpk) & (f < 2 * fpk))) if Q > 1.3 else np.ones_like(f)
    Lmax = 1.0 / ((TWO_PI * (f[0] / 3)) ** 2 * C)   # resonance no lower than f0/3
    Lmin = 1.0 / ((TWO_PI * (f[-1] * 3)) ** 2 * C)  # ... no higher than 3*fmax
    Lpk = float(np.clip(1.0 / ((TWO_PI * fpk) ** 2 * C), Lmin, Lmax))
    Lflat = float(np.clip(1.0 / ((TWO_PI * f[-1]) ** 2 * C), Lmin, Lmax))

    def err_x(resid, inits, bnds):
        best = None
        for p0 in inits:
            s = least_squares(resid, p0, method="trf", bounds=bnds, max_nfev=4000)
            e = float(np.sum(s.fun ** 2))
            if best is None or e < best[0]:
                best = (e, s.x)
        return best

    # ---- 1-branch (R_a, L_a, R_pl), multi-start over peak & plateau regimes ----
    def resid1(p):
        Zm = zmodel(f, np.exp(p[0]), np.exp(p[1]), np.exp(p[2]))
        r = np.log(Zm) - np.log(Z)                  # ln|.| + j.angle  => mag+phase
        return np.concatenate([r.real * w, r.imag * w])
    bnds1 = ([np.log(R0 / 5), np.log(Lmin), np.log(R0 / 3)],
             [np.log(R0 * 5), np.log(Lmax), np.log(1e9)])
    inits1 = [[np.log(R0), np.log(Lpk), np.log(1e9)],
              [np.log(R0), np.log(Lpk), np.log(R0 * 30)],
              [np.log(R0), np.log(Lflat), np.log(max(R0 * 8, R0 / 2))]]
    e1, x1 = err_x(resid1, inits1, bnds1)
    Ra, La, Rpl = np.exp(x1)

    # ---- optional 2nd parallel R-L branch for multi-pole Zout (engaged only if it
    #      beats 1-branch by >40%); otherwise R_b->inf disables it ----
    def resid2(p):
        Zm = zmodel(f, np.exp(p[0]), np.exp(p[1]), np.exp(p[2]), np.exp(p[3]), np.exp(p[4]))
        r = np.log(Zm) - np.log(Z)
        return np.concatenate([r.real * w, r.imag * w])
    bnds2 = ([np.log(R0 / 5), np.log(Lmin), np.log(R0 / 3), np.log(R0 / 3), np.log(Lmin)],
             [np.log(R0 * 5), np.log(Lmax), np.log(1e9), np.log(1e9), np.log(Lmax)])
    inits2 = [[x1[0], x1[1], x1[2], np.log(R0 * 3), np.log(lb)]
              for lb in (Lpk, max(Lpk / 20, Lmin), Lflat)]
    e2, x2 = err_x(resid2, inits2, bnds2)
    if e2 < 0.6 * e1:
        return tuple(np.exp(x2))                     # (R_a, L_a, R_pl, R_b, L_b)
    return (Ra, La, Rpl, 1e9, 1e-12)                 # 2nd branch off


NPS = 3   # PSRR coupling-current REAL sections (i_c real bank = G0 + sum_{i=1..NPS} Gi/(1+s/wi))
NPC = 1   # PSRR coupling-current COMPLEX (2nd-order, conjugate-pair) sections.
          # One signed (b0+b1 s)/(1+s/(Q w0)+(s/w0)^2) section carries non-minimum-phase
          # / notch PSRR phase that strictly-real sections cannot (V4 25deg, V3 10deg).
          # N2=1 validated (analyze_psrr_phase.py): N2>=2 overfits/destabilizes.
SHELF_PH_TRIG = 2.5   # deg: attempt the complex bank if the shelf's PSRR phase RMS
                      # exceeds this even when e_shelf<0.05 (keep-best => zero regression)
MNOISE = 6   # Norton-@vout noise sections (white floor + MNOISE Lorentzians)
NFK = []     # module global: shared Lorentzian corner freqs from the last noise fit
NSPUR_F = []   # module global: intrinsic discrete-spur freqs (Hz, load-independent)
NSPUR_PH = []  # module global: spur SIN injection phase (rad) at the nominal corner
KT4 = 4 * 1.380649e-23 * 300.0   # 4kT at 300K (resistor thermal-noise scale)
NRk = 1e6    # fixed section resistor; gm transconductance sets each section amplitude


def psrr_model(f, R_a, L_a, R_pl, R_b, L_b, G, Q=None):
    """PSRR = i_c(s)*Zout. i_c = REAL bank + optional COMPLEX 2nd-order section:
        i_c = G0 + sum_i Gi/(1+s/wi)  +  (b0 + b1 s)/(1 + s/(Qf w0) + (s/w0)^2)
    G = [G0, G1,w1, G2,w2, G3,w3]; Q = (b0, b1, w0, Qf) or None.
    - real bank only (Q=None / b0=b1=0) = the classic min-phase shelf (1 section)
      or a 3-real-section non-min-phase fit (limited phase reach).
    - + one signed complex-conjugate section -> carries genuine non-minimum-phase /
      notch PSRR phase (V4/V3). Realized as a series R-L-C lowpass state x with two
      VCCS taps (b0 reads V_C=x, b1 reads V_R=a1*dx/dt) -> inductor-bearing but all
      linear/passive + controlled sources, NO laplace_nd, HB/PSS-robust. Pole is
      ALWAYS stable (Re<0 by construction); Qf<=0.5 degrades gracefully to real."""
    s = 1j * TWO_PI * f
    Z = zmodel(f, R_a, L_a, R_pl, R_b, L_b)
    i_c = G[0] + sum(G[1 + 2 * i] / (1 + s / G[2 + 2 * i]) for i in range(NPS))
    if Q is not None and (Q[0] != 0.0 or Q[1] != 0.0):
        b0, b1, w0, Qf = Q
        i_c = i_c + (b0 + b1 * s) / (1 + s / (Qf * w0) + (s / w0) ** 2)
    return i_c * Z                                   # vout/vin


def _sk_fit(s, H, n, n_iter=14):
    """Sanathanan-Koerner rational fit H ~ N(sn)/D(sn) (real coeffs, order n,
    freq-scaled, relative-weighted). Returns (poles, residues, d) in unscaled s
    via partial fractions; None if any pole is complex (caller falls back)."""
    w0 = 1.0 / (np.abs(H) + 1e-15)
    sc = float(np.exp(np.mean(np.log(np.abs(s) + 1e-30))))
    sn = s / sc
    D = np.ones(len(s), complex)
    nc = dc = None
    for _ in range(n_iter):
        w = w0 / np.abs(D)
        cols = [sn ** i for i in range(n + 1)] + [-H * sn ** j for j in range(1, n + 1)]
        A = (np.vstack(cols).T) * w[:, None]
        b = H * w
        x, *_ = np.linalg.lstsq(np.vstack([A.real, A.imag]),
                                np.concatenate([b.real, b.imag]), rcond=None)
        nc = x[:n + 1]; dc = np.concatenate([[1.0], x[n + 1:]])
        D = sum(dc[j] * sn ** j for j in range(n + 1))
    psn = np.roots(dc[::-1])                          # poles in scaled domain
    if np.any(np.abs(psn.imag) > 1e-6 * np.abs(psn.real) + 1e-12):
        return None                                  # complex poles -> need RLC; fall back
    psn = psn.real
    Dp = np.polyder(dc[::-1])
    res = np.array([np.polyval(nc[::-1], p) / np.polyval(Dp, p) for p in psn])
    d = nc[n] / dc[n]
    return psn * sc, res * sc, d                     # unscaled poles, residues, feedthrough


def _aaa_conj(f, y, max_terms=12):
    """AAA rational interpolant on the imaginary axis WITH conjugate samples, so the
    interpolant has real coefficients (conjugate-symmetric poles). Returns (poles,
    residues). Used only to INITIALIZE the bounded section fit (raw AAA over-fits)."""
    from scipy.interpolate import AAA
    s = 1j * TWO_PI * f
    r = AAA(np.concatenate([s, np.conj(s)]), np.concatenate([y, np.conj(y)]),
            max_terms=max_terms)
    poles = r.poles()
    try:
        res = r.residues()
    except Exception:
        res = np.full(len(poles), np.nan, complex)
    return poles, res


def _pair_section(p, r):
    """Conjugate pole pair (p, residue r) -> realizable 2nd-order section parameters
    (b0, b1, w0, Qf) for (b0+b1 s)/(1 + s/(Qf w0) + (s/w0)^2)."""
    B1 = 2 * r.real
    B0 = -2 * (r * np.conj(p)).real
    A1 = -2 * p.real
    A0 = abs(p) ** 2
    w0 = np.sqrt(A0)
    Qf = w0 / A1 if A1 > 1e-30 else 40.0
    return B0 / A0, B1 / A0, w0, Qf            # b0, b1, w0, Qf


def _bank_fit(f, H, zf, n1=NPS):
    """Fit i_c = H/Zout as the (n1 real + 1 complex) section bank: AAA-initialize the
    dominant complex pair + real poles, then polish with least_squares on the EXACT
    realizable form (residual = complex-log of psrr_model/H => mag-dB + phase-deg
    jointly, matching the score). Returns (G[0..2n1], Q=(b0,b1,w0,Qf)) or None."""
    try:
        Zmod = zmodel(f, *zf)
        ic = H / Zmod
        w0lo, w0hi = TWO_PI * f[0] / 10.0, TWO_PI * f[-1] * 10.0
        # ---- AAA init: prune to in-band stable poles, rank, seed ----
        pol, res = _aaa_conj(f, ic)
        band = (np.abs(pol) > w0lo * 3) & (np.abs(pol) < w0hi / 3) & (pol.real < 0)
        pol, res = pol[band], res[band]
        reals, pairs = [], []
        used = np.zeros(len(pol), bool)
        for i, p in enumerate(pol):
            if used[i]:
                continue
            if abs(p.imag) <= 1e-3 * abs(p.real) + 1.0:
                reals.append((p.real, res[i])); used[i] = True
            else:
                j = next((k for k in range(len(pol)) if not used[k] and k != i
                          and abs(pol[k] - np.conj(p)) < 1e-3 * abs(p) + 1.0), None)
                pairs.append((p, res[i])); used[i] = True
                if j is not None:
                    used[j] = True
        reals.sort(key=lambda pr: -abs(pr[1] / pr[0]))                 # by DC weight
        secs = sorted((_pair_section(p, r) for p, r in pairs), key=lambda d: -abs(d[0]))
        p0 = [float(ic[-1].real)]
        for i in range(n1):
            if i < len(reals):
                pr, rr = reals[i]
                p0 += [float((-rr / pr).real), float(np.log(min(max(-pr, w0lo), w0hi)))]
            else:
                p0 += [0.0, float(np.log(TWO_PI * f[len(f) // 2]))]
        if secs:
            b0, b1, w0, Qf = secs[0]
            p0 += [float(b0), float(b1), float(np.log(min(max(w0, w0lo), w0hi))),
                   float(np.log(min(max(Qf, 0.5), 40.0)))]
        else:
            p0 += [0.0, 0.0, float(np.log(TWO_PI * f[len(f) // 2])), float(np.log(2.0))]
        p0 = np.array(p0)
        lo = np.full_like(p0, -np.inf); hi = np.full_like(p0, np.inf)
        k = 1
        for _ in range(n1):
            lo[k + 1], hi[k + 1] = np.log(w0lo), np.log(w0hi); k += 2
        lo[k + 2], hi[k + 2] = np.log(w0lo), np.log(w0hi)
        lo[k + 3], hi[k + 3] = np.log(0.5), np.log(40.0)

        def unpack(p):
            G = [p[0], p[1], np.exp(p[2]), p[3], np.exp(p[4]), p[5], np.exp(p[6])]
            Q = (p[7], p[8], np.exp(p[9]), np.exp(p[10]))
            return G, Q

        def resid(p):
            G, Q = unpack(p)
            r = np.log(psrr_model(f, *zf, G, Q)) - np.log(H)
            return np.concatenate([r.real, r.imag])
        s = least_squares(resid, p0, bounds=(lo, hi), method="trf", max_nfev=20000)
        G, Q = unpack(s.x)
        if not np.all(np.isfinite(np.concatenate([G, Q]))):
            return None
        return G, Q
    except Exception:
        return None


def _psrr_resid(f, H, zf, G, Q=None, sel_hz=1e3):
    """Combined PSRR mag(dB)+phase(rad) RMS residual over f>=sel_hz (the score band).
    The single consistent metric used to KEEP-BEST among shelf / real-bank / complex."""
    m = psrr_model(f, *zf, G, Q)
    sel = f >= sel_hz
    r = np.log(m[sel] / H[sel])
    return float(np.sqrt(np.mean(r.real ** 2 + r.imag ** 2)))


def _shelf(f, H, R_a, L_a, R_pl, R_b, L_b):
    """Min-phase 1-section fit -> G=[g_hf, g_lf-g_hf, wz, 0,big, 0,big]."""
    def resid(p):
        G = [1 / np.exp(p[0]), np.exp(p[1]) - 1 / np.exp(p[0]), np.exp(p[2]), 0, 1e9, 0, 1e9]
        r = np.log(psrr_model(f, R_a, L_a, R_pl, R_b, L_b, G)) - np.log(H)
        return np.concatenate([r.real, r.imag])
    Z8 = np.abs(zmodel(np.array([8e6]), R_a, L_a, R_pl, R_b, L_b)[0])
    Rpass0 = Z8 / np.abs(H[np.argmin(np.abs(f - 8e6))])
    g_lf0 = np.abs(H[0]) / np.abs(zmodel(np.array([f[0]]), R_a, L_a, R_pl, R_b, L_b)[0])
    fpk = f[np.argmax(np.abs(zmodel(f, R_a, L_a, R_pl, R_b, L_b)))]
    s1 = least_squares(resid, [np.log(Rpass0), np.log(g_lf0), np.log(TWO_PI * fpk)], method="lm")
    Rpass, g_lf, wz = np.exp(s1.x)
    G = [1 / Rpass, g_lf - 1 / Rpass, wz, 0.0, 1e9, 0.0, 1e9]
    return G, float(np.sqrt(np.mean(resid(s1.x) ** 2)))


def fit_psrr(f, H, R_a, L_a, R_pl, R_b, L_b):
    """PSRR coupling-current bank -> (G, Q). G=[G0,G1,w1,G2,w2,G3,w3] are the real
    first-order sections; Q=(b0,b1,w0,Qf) is the optional complex 2nd-order section
    (b0=b1=0 => inert). Selector is KEEP-BEST on one combined mag+phase residual, so
    it can never regress:
      * compute the min-phase SHELF; if it is clean (e_shelf<0.05 AND phase<2.5deg)
        return it with an INERT complex section -> min-phase variants stay on the
        shelf exactly as before (zero regression on base + the A-layer + v2).
      * else build candidates {shelf, real-only SK bank, complex bank} and return the
        lowest-residual one. The complex bank (AAA-initialized, least_squares-polished
        on the EXACT realizable form) carries the non-minimum-phase / notch phase the
        strictly-real sections cannot (V4 25->~1deg, V3 10->~1deg, V1 6->~2deg)."""
    zf = (R_a, L_a, R_pl, R_b, L_b)
    Q0 = (0.0, 0.0, TWO_PI * float(np.sqrt(f[0] * f[-1])), 1.0)   # inert default section
    G_shelf, e_shelf = _shelf(f, H, *zf)
    sel = f >= 1e3
    shelf_ph = np.degrees(np.sqrt(np.mean(
        np.angle(psrr_model(f, *zf, G_shelf)[sel] / H[sel]) ** 2)))
    if e_shelf < 0.05 and shelf_ph < SHELF_PH_TRIG:
        return G_shelf, Q0                            # min-phase -> shelf, complex inert

    shelf_resid = _psrr_resid(f, H, zf, G_shelf, Q0)
    cands = [("shelf", G_shelf, Q0, shelf_resid)]
    # real-only SK bank (the previous non-min-phase path; kept as a fallback candidate)
    sk = _sk_fit(1j * TWO_PI * f, H / zmodel(f, *zf), NPS)
    if sk is not None:
        poles, res, d = sk
        secs = sorted([(-p, -r / p) for p, r in zip(poles, res)])   # (w_i=-pole, G_i=-res/pole)
        G = [float(d)]
        for w_i, G_i in secs[:NPS]:
            G += [float(G_i), float(max(w_i, 1.0))]
        while len(G) < 1 + 2 * NPS:
            G += [0.0, 1e9]
        cands.append(("sk", G, Q0, _psrr_resid(f, H, zf, G, Q0)))
    # complex bank: n1 real + 1 signed complex-conjugate section (the new path)
    bank = _bank_fit(f, H, zf)
    if bank is not None:
        Gb, Qb = bank
        cands.append(("complex", Gb, Qb, _psrr_resid(f, H, zf, Gb, Qb)))
    best = min(cands, key=lambda c: c[3])
    # PREFER the complex bank when adequate. A pure-REAL SK fit of a notch can show a
    # lower ANALYTIC residual yet REALIZE with large phase error (fragile near-pole-zero
    # cancellation around the notch -> V4 250u read 25deg in ngspice). The complex
    # section is the physically-faithful non-min-phase form and realizes robustly, so
    # take it whenever its residual is within 2x of the best AND absolutely good.
    comp = next((c for c in cands if c[0] == "complex"), None)
    if comp is not None and comp[3] <= max(2.0 * best[3], 0.15):
        return comp[1], comp[2]
    return best[1], best[2]


def fit_noise_bank(zfits, M=MNOISE):
    """DECOUPLED Norton-@vout noise block. The intrinsic output-noise PSD is
    reproduced by a current source injected at vout: Sv_out = In(f)*|Zout|, so the
    target Norton current is In_target = Sv_meas/|Zout|. This DECOUPLES noise from
    the Zout branch synthesis (the old branch-A series-noise/Cout-divider form is
    base-specific and re-shapes when branch B is added for multi-pole Zout, e.g. V3
    5.6->21.8dB). Probe (harness/probe_noise.py) showed white + M Lorentzians fits
    In_target across ALL variants to <2.1dB at M=6 (v3 21.8->2.0, base 1.2->0.9).

    JOINT fit over the load corners: SHARED corner freqs fk (load-independent poles)
    + per-corner amplitudes, so emit interpolates amplitudes quad-in-ln(iload) while
    the corners stay fixed. Realized as a white-R floor + M fixed-R||C Lorentzian
    sections, each transconducted into vout (gm sets the per-corner amplitude):
        section current PSD = g^2 * 4kT*NRk / (1+(f/fk)^2),  fk = 1/(2pi NRk Ck)
    Returns dict(fk=[..M..], gw={il:g}, gk={il:[..M..]}).  All passive+VCCS -> HB/PSS."""
    targets = {}
    for il in LOADS:
        gn = ref[f"noise_{il}"]; f = gn[:, 0]; Sv = gn[:, 1]
        Zmod = np.abs(zmodel(f, *zfits[il]))
        targets[il] = (f, (Sv / Zmod) ** 2)            # (f, In^2 target)
    f0 = targets[LOADS[0]][0][0]; f1 = targets[LOADS[0]][0][-1]
    nL = len(LOADS)

    def model_il(fks, row, f):                          # row=[logwhite, logamp_1..M]
        out = np.exp(row[0]) * np.ones_like(f)
        for k in range(M):
            out = out + np.exp(row[1 + k]) / (1.0 + (f / fks[k]) ** 2)
        return out

    MIN_LOG_GAP = np.log(2.0)              # adjacent corners >= 2x apart (anti-degeneracy)

    def resid(p):
        fks = np.exp(p[:M])
        rest = p[M:].reshape(nL, M + 1)
        r = []
        for j, il in enumerate(LOADS):
            f, In2 = targets[il]
            r.append(np.log(model_il(fks, rest[j], f) + 1e-80) - np.log(In2 + 1e-80))
        # SEPARATION penalty: keep the M shared corners spread out so two sections
        # cannot collapse onto one pole (which makes anti-correlated giant amplitudes
        # -> the +15.7dB inter-corner interpolation overshoot). Pushes adjacent
        # sorted log-corners >= MIN_LOG_GAP apart.
        gaps = np.diff(np.sort(p[:M]))
        r.append(30.0 * np.maximum(0.0, MIN_LOG_GAP - gaps))
        return np.concatenate(r)

    fks0 = np.log(np.logspace(np.log10(f0 * 1.5), np.log10(f1 / 1.5), M))
    init = list(fks0)
    lob = list(np.log(np.full(M, f0 / 5))); hib = list(np.log(np.full(M, f1 * 5)))
    for il in LOADS:
        f, In2 = targets[il]
        init += [float(np.log(In2[-3:].mean() + 1e-80))]
        init += list(np.log(np.interp(np.exp(fks0), f, In2) + 1e-80))
        lob += [-200.0] * (M + 1); hib += [60.0] * (M + 1)
    s = least_squares(resid, init, bounds=(lob, hib), method="trf", max_nfev=30000)
    fks = np.exp(s.x[:M]); rest = s.x[M:].reshape(nL, M + 1)
    order = np.argsort(fks)                 # SORT sections ascending by corner freq so
    fks = fks[order]                        # 'section k' is the same pole at every corner
    gw, gk = {}, {}
    for j, il in enumerate(LOADS):
        gw[il] = float(np.sqrt(max(np.exp(rest[j, 0]), 1e-44) / (KT4 * NRk)))
        gk[il] = [float(np.sqrt(max(np.exp(rest[j, 1 + k]), 1e-44) / (KT4 * NRk)))
                  for k in order]
    return dict(fk=[float(x) for x in fks], gw=gw, gk=gk)


def fit_all():
    global NFK
    P = {}
    zfits = {}
    print(f"{'load':>5} | {'R_a':>7} {'L_a[uH]':>8} {'R_pl':>9} {'R_b':>9} {'Zrms':>6} | "
          f"{'G0':>9} {'G1':>9} {'w1[MHz]':>7} {'sec':>4} {'Prms':>6} {'Pdeg':>5} | {'Nrms':>6} {'Vreg':>7}")
    for il in LOADS:
        gz = ref[f"z_{il}"]; fz = gz[:, 0]; Z = gz[:, 1] + 1j*gz[:, 2]
        gp = ref[f"p_{il}"]; fp = gp[:, 0]; H = gp[:, 1] + 1j*gp[:, 2]
        dl = ref["dc_loadreg"]; iv = ng.amps(il)
        R_a, L_a, R_pl, R_b, L_b = fit_zout(fz, Z)
        G, Q = fit_psrr(fp, H, R_a, L_a, R_pl, R_b, L_b)   # G=[G0,G1,w1,..]; Q=(b0,b1,w0,Qf)
        zfits[il] = (R_a, L_a, R_pl, R_b, L_b)
        zrms = np.sqrt(np.mean((20*np.log10(np.abs(zmodel(fz, R_a, L_a, R_pl, R_b, L_b))/np.abs(Z)))**2))
        Hm = psrr_model(fp, R_a, L_a, R_pl, R_b, L_b, G, Q)
        prms = np.sqrt(np.mean((20*np.log10(np.abs(Hm)/np.abs(H)))**2))
        sel = fp >= 1e3
        pph = np.degrees(np.sqrt(np.mean(np.angle(Hm[sel] / H[sel]) ** 2)))
        cpx = 1 if (Q[0] != 0.0 or Q[1] != 0.0) else 0
        nsec = 1 + sum(1 for i in (1, 2) if abs(G[1 + 2 * i]) > 1e-12) + cpx
        vout_dc = np.interp(iv, dl[:, 0], dl[:, 1])
        vreg = vout_dc + R_a * iv               # so Vout = vreg - R_a*iload matches DC
        P[il] = dict(iv=iv, R_a=R_a, L_a=L_a, R_pl=R_pl, R_b=R_b, L_b=L_b,
                     G0=G[0], G1=G[1], w1=G[2], G2=G[3], w2=G[4], G3=G[5], w3=G[6],
                     pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3],
                     vreg=vreg, _zrms=zrms, _prms=prms, _pph=pph, _nsec=nsec, _cpx=cpx)
    # ---- decoupled Norton-@vout noise block (joint over corners) ----
    NB = fit_noise_bank(zfits)
    NFK = NB["fk"]
    for il in LOADS:
        P[il]["gnw"] = NB["gw"][il]          # gnw/gn1.. avoid ngspice's CASE-INSENSITIVE
        for k in range(MNOISE):              # param clash with PSRR's G1/G2/G3
            P[il][f"gn{k+1}"] = NB["gk"][il][k]
        nr = _noise_resid(il, zfits[il], NB["gw"][il], NB["gk"][il])
        p = P[il]
        cmark = (f"{p['pcw0']/TWO_PI/1e6:.2f}MHz/Q{p['pcq']:.1f}" if p['_cpx'] else "")
        print(f"{il:>5} | {p['R_a']:7.2f} {p['L_a']*1e6:8.3f} {p['R_pl']:9.1f} {p['R_b']:9.1f} "
              f"{p['_zrms']:6.2f} | {p['G0']:+9.2e} {p['G1']:+9.2e} {p['w1']/TWO_PI/1e6:7.3f} "
              f"{p['_nsec']:3d}{'*' if p['_cpx'] else ' '} {p['_prms']:6.2f} {p['_pph']:5.1f} | "
              f"{nr:6.2f} {p['vreg']*1e3:7.2f}  {cmark}")
    print(f"      noise corners fk[Hz] = " + " ".join(f"{x:.3g}" for x in NFK))
    fit_spurs(P, zfits)
    return P


def predict(P_il, f, nfk=None):
    """ANALYTIC Zout / PSRR / output-noise PSD for ONE load corner, from its fitted
    params P_il (= one entry of fit_all()'s dict) at frequencies f. Uses the SAME
    transfer functions the fitter optimizes -- zmodel (Zout), psrr_model (PSRR=i_c*Zout)
    and the decoupled Norton noise In*|Zout| (In^2 = white floor + Lorentzians, identical
    to _noise_resid) -- so the GUI's before/after overlay IS the fit quality itself: pure
    numpy, NO simulator. This is what Tab 4 (Compare) plots against the imported GT.

    nfk: the shared noise-corner freqs (module NFK after a fit; FitResult carries a copy).
    Returns dict(Zout=complex[], PSRR=complex[], noise=Sv[V/rtHz]) on the given f grid."""
    if nfk is None:
        nfk = NFK
    f = np.asarray(f, dtype=float)
    zf = (P_il["R_a"], P_il["L_a"], P_il["R_pl"], P_il["R_b"], P_il["L_b"])
    Z = zmodel(f, *zf)
    G = [P_il["G0"], P_il["G1"], P_il["w1"], P_il["G2"], P_il["w2"], P_il["G3"], P_il["w3"]]
    Q = (P_il["pcb0"], P_il["pcb1"], P_il["pcw0"], P_il["pcq"])
    H = psrr_model(f, *zf, G, Q)
    In2 = (P_il["gnw"] ** 2) * KT4 * NRk * np.ones_like(f)
    for k in range(len(nfk)):
        gk = P_il.get(f"gn{k+1}", 0.0)
        In2 = In2 + (gk ** 2) * KT4 * NRk / (1.0 + (f / nfk[k]) ** 2)
    Sv = np.sqrt(In2) * np.abs(Z)
    return dict(Zout=Z, PSRR=H, noise=Sv)


@dataclass
class FitResult:
    """Self-contained result of fit_variant(): per-corner params P plus the module-level
    fit state (Cout/ESR, noise corners, spur tones, nominal corner, Vref) so a caller
    (the GUI) need not reach into fit_model module globals. Feed P[il] + nfk to predict()."""
    P: dict
    loads: list
    nominal: str
    cout: float
    esr: float
    nfk: list
    spur_f: list
    spur_ph: list
    vref: float


def fit_variant(vkey, nominal=None, vref=None):
    """In-process entry point: load reference <vkey> and fit it -> FitResult. Mirrors what
    __main__ / run_matrix do (load -> fit_all) but returns a self-contained bundle. emit /
    emit_va still read module state, so call them immediately after for the same DUT (the
    harness models one DUT at a time, exactly as the CLI does)."""
    load(vkey, nominal=nominal, vref=vref)
    P = fit_all()
    return FitResult(P=P, loads=list(LOADS), nominal=NOMINAL, cout=C, esr=RC,
                     nfk=list(NFK), spur_f=list(NSPUR_F), spur_ph=list(NSPUR_PH),
                     vref=VREF)


def fit_spurs(P, zfits):
    """Discrete-spur block (Part B). The GT's measured intrinsic spurs (ref spurs_{il}
    = vout-referred f/amp/phase, characterized by spur_char) are reproduced as
    DETERMINISTIC current tones injected at vout: a current I_k at vout yields a vout
    tone I_k*|Zout(f_k)|, so I_k = vout_amp_k / |Zout_model(f_k)| (same Norton-@vout
    decoupling as the noise block -> spurs inherit the fitted Zout shaping). f_k and
    phase are load-independent (set by on-chip oscillators); only the amplitude I_k is
    interpolated quad-in-ln(iload) (through the clamped _pexpr path). Selector: empty
    table (no detected spurs) -> NSPUR_F=[] -> emit nothing (zero regression)."""
    global NSPUR_F, NSPUR_PH
    NSPUR_F, NSPUR_PH = [], []
    if "spur_F" not in ref.files or len(ref["spur_F"]) == 0:
        return
    F = [float(x) for x in ref["spur_F"]]
    twin0 = float(ref["spur_twin0"])
    nom = NOMINAL
    sp_nom = ref[f"spurs_{nom}"]
    NSPUR_F = F
    for k, fk in enumerate(F):
        ph_k = float(sp_nom[k, 2])
        Zc = zmodel(np.array([fk]), *zfits[nom])[0]
        # GT vout tone: A*cos(2pi f t + (ph - 2pi f twin0))  [abs cos-phase; FFT phase
        # is referenced to the window origin t=twin0]. Inject I*cos(2pi f t + th) ->
        # vout = I|Z|cos(.. + th + ang Z). Match -> th = (ph-2pi f twin0) - ang Z.
        # ngspice/VA SIN is sin-based, so realize cos via +pi/2.
        th = (ph_k - 2 * np.pi * fk * twin0) - np.angle(Zc)
        NSPUR_PH.append(float(np.angle(np.exp(1j * (th + np.pi / 2)))))   # sin-phase, wrapped to [-pi,pi]
    for il in LOADS:
        sp = ref[f"spurs_{il}"]
        for k, fk in enumerate(F):
            Zc = abs(zmodel(np.array([fk]), *zfits[il])[0])
            P[il][f"sa{k+1}"] = float(sp[k, 1] / Zc)        # injection current amplitude
    amps = "  ".join(f"{fk/1e6:.3f}MHz:I={P[nom][f'sa{k+1}']*1e6:.2f}uA" for k, fk in enumerate(F))
    print(f"      spur tones ({len(F)}): {amps}")


def _noise_resid(il, zf, gw, gk):
    """Reconstructed-output-PSD log-RMS (dB) vs GT, for the fitted Norton bank."""
    gn = ref[f"noise_{il}"]; f = gn[:, 0]; Sv = gn[:, 1]
    Zmod = np.abs(zmodel(f, *zf))
    In2 = (gw ** 2) * KT4 * NRk * np.ones_like(f)
    for k in range(MNOISE):
        In2 = In2 + (gk[k] ** 2) * KT4 * NRk / (1.0 + (f / NFK[k]) ** 2)
    Smod = np.sqrt(In2) * Zmod
    return float(np.sqrt(np.mean((20 * np.log10((Smod + 1e-30) / (Sv + 1e-30))) ** 2)))


# Interpolation envelope margins. The clamp is TIGHT (was [min/1.5, max*1.5], a loose
# band-aid that let v3 pcw0->1.245e8 and v1 R_pl->38.4 escape) but kept just wide enough
# (a fraction of a percent) that it is INACTIVE at the corners -> the deg<=2 poly still
# passes through every corner exactly -> the 3-corner scorecard is byte-identical, while
# between/beyond corners every param is bounded to its measured corner envelope.
CLAMP_M = 1.005      # log (magnitude) params: multiplicative margin on [min, max]
CLAMP_ADD = 0.005    # linear (signed) params: additive margin as a fraction of the span


def _poly(P, key, logspace):
    u = np.array([np.log(P[il]["iv"]) for il in LOADS])
    y = np.array([P[il][key] for il in LOADS], dtype=float)
    y = np.log(y) if logspace else y
    deg = min(len(LOADS) - 1, 2)                # 3 corners -> quadratic; general for 1/2 corners
    return np.polyfit(u, y, deg)                # np order hi->lo (c2 u^2 + c1 u + c0)


def _body(c):
    """ngspice/Verilog-A polynomial body in u=ln(ic) for coeffs c (np hi->lo order).
    For the standard 3-corner (deg-2) case this is byte-identical to the legacy
    `(c0)*u*u + (c1)*u + (c2)` form, so the emitted at-corner values are unchanged."""
    d = len(c) - 1
    parts = []
    for i, ci in enumerate(c):
        p = d - i
        if p == 0:
            parts.append(f"({ci:.6e})")
        elif p == 1:
            parts.append(f"({ci:.6e})*u")
        elif p == 2:
            parts.append(f"({ci:.6e})*u*u")
        else:
            parts.append(f"({ci:.6e})*u**{p}")
    return " + ".join(parts)


def _pexpr(P, key, logspace):
    """Interpolation expr for a model param, in u=ln(ic). The deg<=2 poly is forced
    exactly through the load corners, but a non-monotonic corner pattern lets it
    overshoot FAR outside the measured envelope at off-corner loads (verified: +15.7dB
    in the spur band; v3 pcw0->1.245e8; v1 R_pl->38.4). CLAMP every param to its corner
    envelope with a small margin so off-corner loads stay inside the measured range
    while the clamp is INACTIVE at the corners (-> at-corner value, hence the score, is
    unchanged). LINEAR params are clamped too now (previously unclamped -> pcb1/G2/G3
    could overshoot). min/max/exp are valid in both ngspice (.param) and Verilog-A."""
    c = _poly(P, key, logspace)
    body = _body(c)
    vals = [float(P[il][key]) for il in LOADS]
    if logspace:
        lo, hi = min(vals) / CLAMP_M, max(vals) * CLAMP_M
        return f"min(max(exp({body}),{lo:.6e}),{hi:.6e})"
    pad = CLAMP_ADD * (max(vals) - min(vals))
    lo, hi = min(vals) - pad, max(vals) + pad
    return f"min(max({body},{lo:.6e}),{hi:.6e})"


def build_pwl(vreg121):
    """Nonlinear branch-A conductance table: (Vdrop, I) from the GT DC curve, where
    Vdrop=vreg121-Vout, I=Iload. Local slope = GT load-dep Zout(0) (preserves small
    signal); saturation = device current limit (dropout). Fine at low I, coarse to 6mA."""
    dl, dd = ref["dc_loadreg"], ref["dc_dropout"]
    il = np.concatenate([dl[::6, 0], dd[dd[:, 0] > 5e-4, 0]])
    vo = np.concatenate([dl[::6, 1], dd[dd[:, 0] > 5e-4, 1]])
    vdrop = vreg121 - vo
    order = np.argsort(vdrop)
    vdrop, il = vdrop[order], il[order]
    keep = np.concatenate([[True], np.diff(vdrop) > 1e-5])     # strictly ascending
    vdrop, il = vdrop[keep], il[keep]
    return " ".join(f"{v:.6e},{i:.6e}" for v, i in zip(vdrop, il)).replace(" ", ",")


def _noise_net():
    """Build the decoupled Norton-@vout noise bank: white-R floor + MNOISE fixed
    R||C Lorentzian sections, each transconducted into vout (gm = gw/g_k sets the
    per-corner amplitude). Fixed R=NRk + C set each corner fk; gm is interpolated."""
    lines = [f"Rnw  nw 0 {NRk:.6e}", "Gnw  0 vout nw 0 {gnw}      $ white floor"]
    for k in range(MNOISE):
        Ck = 1.0 / (TWO_PI * NFK[k] * NRk)
        lines += [f"Rn{k+1}  nk{k+1} 0 {NRk:.6e}",
                  f"Cn{k+1}  nk{k+1} 0 {Ck:.6e}        $ corner {NFK[k]:.4g} Hz",
                  f"Gn{k+1}  0 vout nk{k+1} 0 {{gn{k+1}}}"]
    return "\n".join(lines)


def _spur_net():
    """Intrinsic discrete-spur bank: one deterministic current tone per fundamental
    injected at vout (DC=0 AC=0 -> inert in .op/.dc/.ac/.noise; reproduces the GT's
    vout tone via I_k*|Zout|). Empty when no spurs were detected (selector off)."""
    if not NSPUR_F:
        return ""
    lines = ["* ---- intrinsic discrete spurs: deterministic current tones @vout ----",
             "* (DC=0 AC=0 -> inert in .op/.dc/.ac/.noise; vout tone = I_k*|Zout(f_k)|).",
             "* Node order '0 vout' injects current INTO vout (matches the noise/PSRR",
             "* sign convention) so the reproduced tone phase matches the GT."]
    for k, (fk, ph) in enumerate(zip(NSPUR_F, NSPUR_PH)):
        lines.append(f"Isp{k+1} 0 vout DC 0 AC 0 SIN(0 {{sa{k+1}}} {fk:.6e} 0 0 {np.degrees(ph):.4f})")
    return "\n".join(lines)


def _spur_manifest(name):
    """PSS/HB fundamental manifest for the emitted spur tones: the f_k the user adds
    to the analysis `funds`, plus the commensurate GCD base (one fund) vs incommensurate
    (separate funds) verdict. Returned as a SPICE comment block + a sidecar dict."""
    if not NSPUR_F:
        return "", None
    import math
    binhz = NSPUR_F[0]
    bins = [int(round(f / 15625.0)) for f in NSPUR_F]   # spur_char.BINHZ grid
    g = bins[0]
    for b in bins[1:]:
        g = math.gcd(g, b)
    base = g * 15625.0
    order = max(int(round(f / base)) for f in NSPUR_F)
    commensurate = order <= 40
    flist = " ".join(f"{f:.6e}" for f in NSPUR_F)
    if commensurate:
        verdict = f"commensurate: single PSS fund {base/1e6:.4f}MHz, maxharm>={order}"
    else:
        verdict = f"incommensurate: declare {len(NSPUR_F)} separate HB funds"
    cmt = ("* ============================================================\n"
           f"* SPUR FUNDAMENTALS (add to PSS/HB funds with the {name} model):\n"
           f"*   f_k [Hz] = {flist}\n"
           f"*   {verdict}\n"
           "* External supply/bias spurs are NOT emitted: inject the aggressor tone at\n"
           "* the vin port in the system TB; the model's PSRR path carries it to vout.\n"
           "* ============================================================")
    side = dict(model=name, spur_f_hz=list(NSPUR_F),
                spur_phase_rad=list(NSPUR_PH), base_hz=base, max_order=order,
                commensurate=commensurate)
    return cmt, side


def emit(P, path):
    vreg121 = P[NOMINAL]["vreg"]
    pwl_tab = build_pwl(vreg121)
    specs = ([("R_a", True), ("L_a", True), ("R_pl", True), ("R_b", True), ("L_b", True),
              ("G0", False), ("G1", False), ("w1", True), ("G2", False), ("w2", True),
              ("G3", False), ("w3", True),
              ("pcb0", False), ("pcb1", False), ("pcw0", True), ("pcq", True), ("gnw", True)]
             + [(f"gn{k+1}", True) for k in range(MNOISE)]
             + [(f"sa{k+1}", True) for k in range(len(NSPUR_F))] + [("vreg", False)])
    defs = "\n".join(f".param {k} = {{{_pexpr(P, k, ls)}}}" for k, ls in specs)
    noise_net = _noise_net()
    spur_net = _spur_net()
    spur_cmt, spur_side = _spur_manifest(path.stem)
    txt = f"""* ============================================================
* CANDIDATE behavioral LDO model  (subckt: ldo_model, ports: vin vout)
* Fitted to ground-truth by harness/fit_model.py. All linear/passive +
* controlled sources -> PSS/HB-robust (NO laplace_nd). OP-parameterized by iload
* via quadratic-in-ln(iload) interpolation through corners {{{','.join(LOADS)}}}.
*   Zout = (R_a + sL_a||R_pl) || (R_b+sL_b) || (ESR+1/sCout)  Cout/ESR auto-extracted;
*          R_pl damps the L rise (inf=peak / finite=plateau); branch B (R_b->inf=off)
*          adds a 2nd resonance/roll-off for multi-pole LDOs (engaged only if it helps)
*   PSRR = i_c*Zout, i_c = G0 + sum Gi/(1+s/wi) [real bank] + one signed COMPLEX 2nd-
*          order section (b0+b1 s)/(1+s/(Q w0)+(s/w0)^2): shelf (1 real)=min-phase, 3 real
*          =non-min-phase mag, + complex section = non-min-phase / notch PHASE (V4/V3).
*          LP filter Rs are noiseless VCCS-conductances (no thermal noise from signal path)
*   Noise= DECOUPLED Norton @vout: white + {MNOISE} Lorentzian current sections fit to
*          In=Sv/|Zout| -> output PSD = In*|Zout| exactly, independent of Zout synthesis
* ============================================================
.subckt ldo_model vin vout iload={NOMINAL} slew_en=0
.param ic = {{min(max(iload,{LOADS[0]}),{LOADS[-1]})}}
.param u  = {{ln(ic)}}
.param VREG121 = {vreg121:.6e}    $ nominal-corner Vreg (DC-curve table reference)
{defs}
.param COUT={C:.6e} ESR={RC:.6e}    $ auto-extracted physical output cap / ESR
.param Cps=1p Rp1={{1/(w1*Cps)}} Rp2={{1/(w2*Cps)}} Rp3={{1/(w3*Cps)}}
* complex PSRR section: a1=1/(Q.w0), a2=1/w0^2 ; series R-L-C w/ Cpc fixed -> Rpc,Lpc
.param Cpc=1p pca1={{1/(pcq*pcw0)}} pca2={{1/(pcw0*pcw0)}}
.param Rpc={{pca1/Cpc}} Lpc={{pca2/Cpc}} gqb1={{pcb1/pca1}}
*
* ---- DC operating point: regulated Vreg (constant). vin->vout coupling is
*      handled ENTIRELY by the PSRR path below (small-signal-consistent: its DC
*      limit g_lf*R_a = the measured PSRR LF). A broadband line-reg term here
*      would create a parasitic flat PSRR floor, so it is intentionally omitted.
Breg vreg 0 V = {{vreg}}
*
* ---- Zout: branch A (R_a + L_a) || branch C (ESR+Cout) || branch B (opt) ----
La   vout nA  {{L_a}}
Rpl  vout nA  {{R_pl}}            $ damping across L_a (R_pl->inf = peak; finite = plateau)
* branch-A conductance: linear R_a (slew_en=0, PSS/HB) OR nonlinear DC-curve w/
* current-limit+dropout (slew_en=1). La in series stays -> resonance + di/dt slew.
Bra  vreg nA I = {{(slew_en > 0.5) ? pwl(V(vreg,nA)-(vreg-VREG121),{pwl_tab}) : V(vreg,nA)/R_a}}
Cout vout nC  {{COUT}}
Resr nC  vreg {{ESR}}
* ---- optional branch B: 2nd parallel R-L for multi-pole Zout (R_b->inf = off) ----
Lb   vout nbb {{L_b}}
Rbb  nbb  vreg {{R_b}}
*
* ---- intrinsic output noise: DECOUPLED Norton current @vout (white + {MNOISE} Lorentzians).
* Each section = fixed R||C thermal noise (sets corner) x VCCS gm (sets amplitude),
* injected at vout so output PSD = In(f)*|Zout(f)|. Decoupled from the Zout branches
* above -> immune to branch-B re-shaping; fit to In=Sv/|Zout| across load corners.
{noise_net}
*
* ---- PSRR path: i_c = G0 + sum_i Gi*LP_i(vin-1.05), injected into vout (x Zout).
*      Bank of signed first-order real-pole sections (+ one complex section below).
*      The LP filter resistors are realized as NOISELESS VCCS-conductances (Grp*),
*      NOT physical R: the PSRR coupling is a signal-transfer path, so its filter
*      elements must contribute NO thermal noise (else they corrupt the decoupled
*      Norton noise block). This matches the Verilog-A mirror (which is already
*      noiseless) and is I-V identical to a resistor in .op/.dc/.ac/.tran. ----
Vrf  vrf 0 DC {VREF:g}               $ nominal-vin reference (model fit at vin={VREF:g})
Gd   0   vout vin vrf {{G0}}         $ feedthrough  G0*(vin-1.05)
Grp1 vin np1 vin np1 {{1/Rp1}}   $ noiseless conductance (= 1/Rp1)
Cp1  np1 vrf {{Cps}}             $ np1 = LP1(vin), DC-referenced to 1.05
Gs1  0   vout np1 vrf {{G1}}         $ G1*(LP1(vin)-1.05)
Grp2 vin np2 vin np2 {{1/Rp2}}
Cp2  np2 vrf {{Cps}}
Gs2  0   vout np2 vrf {{G2}}         $ G2*(LP2(vin)-1.05)  (0 if min-phase)
Grp3 vin np3 vin np3 {{1/Rp3}}
Cp3  np3 vrf {{Cps}}
Gs3  0   vout np3 vrf {{G3}}         $ G3*(LP3(vin)-1.05)  (0 if min-phase)
* ---- PSRR complex-conjugate 2nd-order section: carries non-min-phase / notch
*      PHASE that the real shelf cannot. Series Rpc-Lpc-Cpc lowpass state x=V(ncs2,vrf);
*      Gqb0 reads V_C=x, Gqb1 reads V_R=a1*dx/dt -> i_c += (pcb0+pcb1 s)/(1+a1 s+a2 s^2).
*      Inert when pcb0=pcb1=0 (min-phase variants). Rpc is a NOISELESS VCCS-conductance
*      (Grpc); Lpc/Cpc are noiseless -> the whole section adds no thermal noise. No laplace.
Grpc vin  ncs1 vin ncs1 {{1/Rpc}}    $ noiseless conductance (= 1/Rpc)
Lpc  ncs1 ncs2 {{Lpc}}
Cpc  ncs2 vrf  {{Cpc}}
Gqb0 0 vout ncs2 vrf {{pcb0}}        $ inject b0*x
Gqb1 0 vout vin  ncs1 {{gqb1}}       $ inject b1*dx/dt = (b1/a1)*V_R
{spur_net}
.ends ldo_model
{spur_cmt}
"""
    path.write_text(txt)
    print(f"\nwrote {path}")
    if spur_side is not None:
        import json
        sp_path = path.with_name(path.stem + "_spurs.json")
        sp_path.write_text(json.dumps(spur_side, indent=2))
        print(f"wrote {sp_path}")


def _va_expr(c, logspace):
    body = f"({c[0]:.6e})*u*u + ({c[1]:.6e})*u + ({c[2]:.6e})"
    return f"exp({body})" if logspace else f"({body})"


def emit_va(P, path, tbl_path):
    """Emit an equivalent Verilog-A model for Cadence Spectre. Mirrors the validated
    SPICE topology. Noise is a decoupled Norton @vout (white + MNOISE R||C-Lorentzian
    sections -> matches In=Sv/|Zout|). Nonlinear dropout via $table_model reading .tbl."""
    specs = ([("R_a", True), ("L_a", True), ("R_pl", True), ("R_b", True), ("L_b", True),
              ("G0", False), ("G1", False), ("w1", True), ("G2", False), ("w2", True),
              ("G3", False), ("w3", True),
              ("pcb0", False), ("pcb1", False), ("pcw0", True), ("pcq", True), ("gnw", True)]
             + [(f"gn{k+1}", True) for k in range(MNOISE)]
             + [(f"sa{k+1}", True) for k in range(len(NSPUR_F))] + [("vreg", False)])
    asg = "\n      ".join(f"{k} = {_pexpr(P, k, ls)};" for k, ls in specs)
    vreg121 = P[NOMINAL]["vreg"]
    # export dropout table (Vdrop relative to nominal vreg, I) for $table_model
    dl, dd = ref["dc_loadreg"], ref["dc_dropout"]
    il = np.concatenate([dl[::6, 0], dd[dd[:, 0] > 5e-4, 0]])
    vo = np.concatenate([dl[::6, 1], dd[dd[:, 0] > 5e-4, 1]])
    vd = vreg121 - vo
    o = np.argsort(vd); vd, il = vd[o], il[o]
    keep = np.concatenate([[True], np.diff(vd) > 1e-5]); vd, il = vd[keep], il[keep]
    tbl_path.write_text("\n".join(f"{v:.6e} {i:.6e}" for v, i in zip(vd, il)) + "\n")
    nk_nodes = ", ".join(f"nk{k+1}" for k in range(MNOISE))
    gvars = ", ".join(["gnw"] + [f"gn{k+1}" for k in range(MNOISE)]
                      + [f"sa{k+1}" for k in range(len(NSPUR_F))])
    Cn_par = "\n  ".join(
        f"parameter real Cn{k+1} = {1.0/(TWO_PI*NFK[k]*NRk):.6e};   // corner {NFK[k]:.4g} Hz"
        for k in range(MNOISE))
    nsec = "\n    ".join(
        f"I(nk{k+1}) <+ V(nk{k+1})/NRk + Cn{k+1}*ddt(V(nk{k+1}));\n"
        f"    I(nk{k+1}) <+ white_noise(4*`P_K*$temperature/NRk, \"nk{k+1}\");\n"
        f"    I(vout)    <+ gn{k+1}*V(nk{k+1});"
        for k in range(MNOISE))
    # deterministic intrinsic spur tones at vout ($abstime -> correct in tran AND PSS/HB).
    # NEGATIVE sign so I(vout)<+ ... injects current INTO vout (matches the SPICE
    # '0 vout' source order), so the reproduced tone phase matches the GT.
    spur_va = "\n    ".join(
        f"I(vout) <+ -sa{k+1}*sin(`M_TWO_PI*{fk:.6e}*$abstime + ({ph:.6f}));"
        for k, (fk, ph) in enumerate(zip(NSPUR_F, NSPUR_PH)))
    if not spur_va:
        spur_va = "// (no intrinsic spurs for this variant)"
    va = f"""// ============================================================
// Behavioral LDO model for Cadence Spectre  (auto-gen: harness/fit_model.py)
// Equivalent to model/ldo_model.lib (validated in ngspice). NO laplace_nd.
//   Zout = (R_a + sL_a||R_pl) || (R_b+sL_b) || (ESR+1/sCout) ; R_pl damps L
//   PSRR = i_c*Zout, i_c = G0 + sum Gi/(1+s/wi) [real bank] + one signed COMPLEX 2nd-
//          order section (b0+b1 s)/(1+s/(Q w0)+(s/w0)^2) -> non-min-phase / notch PHASE
//          (V4/V3). LP filter resistors are noiseless conductances (no thermal noise).
//   Noise= DECOUPLED Norton @vout: white-R + {MNOISE} R||C-Lorentzian current sections,
//          transconducted into vout (gm sets amplitude) -> In*|Zout| = Sv. pnoise/hbnoise.
//   slew_en=1 : nonlinear DC-curve branch (current-limit + dropout) via $table_model
// Params interpolated as quadratics in ln(iload) through corners {{{','.join(LOADS)}}}.
// ============================================================
`include "constants.vams"
`include "disciplines.vams"

module ldo_model(vin, vout);
  inout vin, vout;
  electrical vin, vout, vrg, nA, nC, nbb, np1, np2, np3, vrf, ncs1, ncs2, nw, {nk_nodes};

  parameter real iload   = {_amps(NOMINAL):.6e} from (0:inf);
  parameter integer slew_en = 0 from [0:1];
  parameter real Cout = {C:.6e};   // auto-extracted physical output cap
  parameter real ESR  = {RC:.6e};   // auto-extracted physical ESR
  parameter real VREG121 = {vreg121:.6e};
  parameter real NRk = {NRk:.6e};   // fixed noise-section resistor (gm sets amplitude)
  {Cn_par}

  real u, ic, R_a, L_a, R_pl, R_b, L_b, G0, G1, w1, G2, w2, G3, w3, {gvars}, vreg, Cps;
  real pcb0, pcb1, pcw0, pcq, pca1, pca2, Rpc, Lpc, Cpc, gqb1;

  analog begin
    @(initial_step) begin
      ic = min(max(iload, {_amps(LOADS[0]):.6e}), {_amps(LOADS[-1]):.6e});
      u  = ln(ic);
      {asg}
      Cps = 1e-12;
      // complex PSRR section: a1=1/(Q.w0), a2=1/w0^2 ; series R-L-C (Cpc fixed)
      Cpc = 1e-12;
      pca1 = 1.0/(pcq*pcw0); pca2 = 1.0/(pcw0*pcw0);
      Rpc = pca1/Cpc; Lpc = pca2/Cpc; gqb1 = pcb1/pca1;
    end

    // ---- references / DC operating point ----
    V(vrf) <+ {VREF:g};                  // nominal-vin reference (no DC coupling)
    V(vrg) <+ vreg;                  // regulated output reference (vreg = real var)

    // ---- Zout branch C: series Cout + ESR (vout -> vrg) ----
    I(vout, nC) <+ Cout*ddt(V(vout, nC));
    V(nC, vrg)  <+ ESR*I(nC, vrg);

    // ---- Zout branch B: optional 2nd R-L (R_b->inf disables it) ----
    I(vout, nbb) <+ idt(V(vout, nbb))/L_b;
    V(nbb, vrg)  <+ R_b*I(nbb, vrg);

    // ---- Zout branch A: (L_a || R_pl) + (R_a | nonlinear dropout) ----
    // L_a||R_pl as summed current contributions (idt form admits the parallel R)
    I(vout, nA) <+ idt(V(vout, nA))/L_a + V(vout, nA)/R_pl;
    if (slew_en == 0)
      V(nA, vrg) <+ R_a*I(nA, vrg);
    else                             // $table_model control string may be Spectre-version specific
      I(vrg, nA) <+ $table_model(V(vrg, nA) - (vreg - VREG121), "{tbl_path.name}", "1L");

    // ---- intrinsic output noise: decoupled Norton current @vout ----
    // white floor: R-only node nw (V PSD = 4kT*NRk) transconducted by gw
    I(nw)   <+ V(nw)/NRk;
    I(nw)   <+ white_noise(4*`P_K*$temperature/NRk, "nw");
    I(vout) <+ gnw*V(nw);
    // {MNOISE} Lorentzian sections: R||C node nk (corner fk) transconducted by g_k
    {nsec}

    // ---- PSRR path: i_c = G0 + sum Gi*LP_i(vin-1.05) into vout (x Zout) ----
    // signed real-pole section bank (R_i = 1/(wi*Cps), so 1/R_i = wi*Cps)
    I(vin, np1) <+ (V(vin) - V(np1))*(w1*Cps);
    I(np1, vrf) <+ Cps*ddt(V(np1, vrf));
    I(vin, np2) <+ (V(vin) - V(np2))*(w2*Cps);
    I(np2, vrf) <+ Cps*ddt(V(np2, vrf));
    I(vin, np3) <+ (V(vin) - V(np3))*(w3*Cps);
    I(np3, vrf) <+ Cps*ddt(V(np3, vrf));
    I(vout)     <+ G0*(V(vin) - V(vrf)) + G1*(V(np1) - V(vrf))
                   + G2*(V(np2) - V(vrf)) + G3*(V(np3) - V(vrf));

    // ---- PSRR complex-conjugate 2nd-order section (non-min-phase / notch phase) ----
    // series R-L-C lowpass state x=V(ncs2,vrf); b0 reads V_C=x, b1 reads V_R=a1*dx/dt.
    // i_c += (pcb0+pcb1 s)/(1+a1 s+a2 s^2)*(vin-vrf). Inert when pcb0=pcb1=0. No laplace.
    I(vin, ncs1)  <+ V(vin, ncs1)/Rpc;
    I(ncs1, ncs2) <+ idt(V(ncs1, ncs2))/Lpc;
    I(ncs2, vrf)  <+ Cpc*ddt(V(ncs2, vrf));
    I(vout)       <+ pcb0*V(ncs2, vrf) + gqb1*V(vin, ncs1);

    // ---- intrinsic discrete spurs: deterministic current tones @vout ----
    {spur_va}
  end
endmodule
"""
    path.write_text(va)
    print(f"wrote {path}\nwrote {tbl_path}")


def _selftest(vkey="base"):
    """Verify predict() reproduces the analytic Zout/PSRR/noise the fitter itself uses
    (sanity for the GUI Compare overlay): predict at the GT freq grid must match the
    fit_all per-corner residual metrics exactly. No simulator, fast."""
    res = fit_variant(vkey)
    ok = True
    for il in res.loads:
        gz = ref[f"z_{il}"]; fz = gz[:, 0]; Zg = gz[:, 1] + 1j * gz[:, 2]
        gp = ref[f"p_{il}"]; fp = gp[:, 0]; Hg = gp[:, 1] + 1j * gp[:, 2]
        gn = ref[f"noise_{il}"]; fn = gn[:, 0]
        pr_z = predict(res.P[il], fz, res.nfk)
        pr_p = predict(res.P[il], fp, res.nfk)
        pr_n = predict(res.P[il], fn, res.nfk)
        zrms = np.sqrt(np.mean((20 * np.log10(np.abs(pr_z["Zout"]) / np.abs(Zg))) ** 2))
        prms = np.sqrt(np.mean((20 * np.log10(np.abs(pr_p["PSRR"]) / np.abs(Hg))) ** 2))
        nr = _noise_resid(il, (res.P[il]["R_a"], res.P[il]["L_a"], res.P[il]["R_pl"],
                              res.P[il]["R_b"], res.P[il]["L_b"]),
                          res.P[il]["gnw"], [res.P[il][f"gn{k+1}"] for k in range(MNOISE)])
        # predict's noise must equal the _noise_resid reconstruction at the same grid
        Smod = pr_n["noise"]
        nr2 = float(np.sqrt(np.mean((20 * np.log10((Smod + 1e-30) / (gn[:, 1] + 1e-30))) ** 2)))
        match = abs(zrms - res.P[il]["_zrms"]) < 1e-9 and abs(prms - res.P[il]["_prms"]) < 1e-9 \
            and abs(nr2 - nr) < 1e-9
        ok = ok and match
        print(f"  {il:>5}: predict zrms={zrms:.4f} (fit {res.P[il]['_zrms']:.4f})  "
              f"prms={prms:.4f} (fit {res.P[il]['_prms']:.4f})  npsd={nr2:.4f} (fit {nr:.4f})  "
              f"{'OK' if match else 'MISMATCH'}")
    print(f"selftest {'PASS' if ok else 'FAIL'} (predict == fitter analytic) "
          f"nominal={res.nominal} vref={res.vref} cout={res.cout*1e12:.1f}pF esr={res.esr:.3f}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    ap.add_argument("--selftest", action="store_true",
                    help="verify predict() matches the fitter analytic (no emit)")
    a = ap.parse_args()
    vkey = a.variant
    if a.selftest:
        import sys
        sys.exit(0 if _selftest(vkey) else 1)
    res = fit_variant(vkey)
    P = res.P
    name = "ldo_model" if vkey == "base" else f"ldo_{vkey}"
    mdir = ng.ROOT / "model"
    emit(P, mdir / f"{name}.lib")
    emit_va(P, mdir / f"{name}.va", mdir / f"{name}_dropout.tbl")
