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
from dataclasses import dataclass, field
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
CFT = 0.0                  # GATED vin->vout feedthrough cap [F]; 0.0 = disabled (legacy
                           # path byte-identical). Enabled by fit_cft() ONLY when the PSRR
                           # injection i_c = H/Zout shows an unambiguous jwC tail at every
                           # corner (real part: package/pass-device Cgd, ~174fF on Target B).


_amps = ng.amps   # canonical corner-key->amps (see ng.amps); module alias for the emit f-strings


def fit_cft():
    """Extract + GATE the vin->vout feedthrough cap C_ft from the PSRR transfer's
    equivalent injection i_c = H/Zout. A physical feedthrough cap (pass-device Cgd /
    package coupling) makes i_c an exact jwC tail at GHz with a LOAD-INDEPENDENT C
    (Target B: 172.6/174.7/176.0 fF per corner). Fit ic ~ G_hf + jwC (weighted
    complex LS, 2 real unknowns) on the TOP 1.5 DECADES of every corner; enable
    ONLY when the tail is unambiguously a feedthrough cap at EVERY corner:
      (a) C_LS > 0;
      (b) per-point rel-std of Im(ic)/(2pi f) over the top decade < 10%;
      (c) dominance w*C_LS/|ic(f_top)| > 0.5 at the band top;
      (d) (G_hf + jwC) tail-fit relative RMS < 5%;
      (e) cross-corner spread of C_LS (max-min)/mean < 20%.
    Synthetic refs miss by orders of magnitude (base rel-std 81%/dominance 0.08,
    v3_miller C negative) -> gate stays SILENT and the legacy path is untouched."""
    global CFT
    cs = []
    for il in LOADS:
        kp = f"p_{il}"
        kz = f"z_{il}"
        if kp not in ref.files or kz not in ref.files:
            return
        gp = ref[kp]; fp = gp[:, 0]; H = gp[:, 1] + 1j * gp[:, 2]
        gz = ref[kz]; fz = gz[:, 0]; Zz = gz[:, 1] + 1j * gz[:, 2]
        if il == NOMINAL:           # the wideband _hf sweeps are nominal-corner-only;
            khf = f"p_{NOMINAL}_hf"     # use them when they EXTEND the in-band range
            if khf in ref.files:
                gh = ref[khf]
                m = gh[:, 0] > fp[-1]
                if m.any():
                    fp = np.concatenate([fp, gh[m, 0]])
                    H = np.concatenate([H, gh[m, 1] + 1j * gh[m, 2]])
            kzhf = f"z_{NOMINAL}_hf"
            if kzhf in ref.files:
                gh = ref[kzhf]
                m = gh[:, 0] > fz[-1]
                if m.any():
                    fz = np.concatenate([fz, gh[m, 0]])
                    Zz = np.concatenate([Zz, gh[m, 1] + 1j * gh[m, 2]])
        # interpolate Z (complex, log-f) onto the H grid -> equivalent injection ic
        zr = np.interp(np.log(fp), np.log(fz), Zz.real)
        zi = np.interp(np.log(fp), np.log(fz), Zz.imag)
        Zi = zr + 1j * zi
        with np.errstate(divide="ignore", invalid="ignore"):
            ic = H / Zi
        # ROBUSTNESS: zero/non-finite H or Z samples (real-export artifacts) poison the
        # weighted LS (w=1/|ic| -> inf) and the tail reads. Mask them out FIRST; if too
        # few clean points survive in the fit window, the gate stays silent.
        good = (np.isfinite(ic) & (np.abs(ic) > 0)
                & np.isfinite(H) & (np.abs(H) > 0)
                & np.isfinite(Zi) & (np.abs(Zi) > 0))
        fp, ic = fp[good], ic[good]
        if fp.size == 0:
            return
        top = fp >= fp[-1] / 10.0 ** 1.5            # top 1.5 decades = the fit band
        if top.sum() < 8:
            return
        ft, yt = fp[top], ic[top]
        w = 1.0 / np.abs(yt)                        # relative weighting (zeros masked above)
        A = np.vstack([np.ones(len(ft)), 1j * TWO_PI * ft]).T * w[:, None]
        b = yt * w
        try:
            x, *_ = np.linalg.lstsq(np.vstack([A.real, A.imag]),
                                    np.concatenate([b.real, b.imag]), rcond=None)
        except np.linalg.LinAlgError:
            return
        g_hf, c_ls = float(x[0]), float(x[1])
        if not np.isfinite(c_ls) or c_ls <= 0 or not np.isfinite(g_hf):    # (a)
            return
        td = fp >= fp[-1] / 10.0                    # top decade = the consistency band
        cpt = ic[td].imag / (TWO_PI * fp[td])       # per-point capacitance read
        mu = float(np.mean(cpt))
        if not np.isfinite(mu) or mu <= 0 or not float(np.std(cpt)) / abs(mu) <= 0.10:  # (b)
            return
        if TWO_PI * fp[-1] * c_ls / abs(ic[-1]) < 0.5:                  # (c)
            return
        rel = np.abs((g_hf + 1j * TWO_PI * ft * c_ls) - yt) / np.abs(yt)
        if float(np.sqrt(np.mean(rel ** 2))) > 0.05:                    # (d)
            return
        cs.append(c_ls)
    cmean = float(np.mean(cs))
    if not np.isfinite(cmean) or cmean <= 0:        # NaN-poisoning guard (final acceptance)
        return
    if (max(cs) - min(cs)) / cmean > 0.20:                              # (e)
        return
    CFT = cmean
    print(f"  feedthrough gate: i_c HF tail is jwC -> C_ft={CFT*1e15:.1f}fF "
          f"(vin->vout cap enabled)")


def fit_cout_esr():
    """Auto-extract the physical Cout/ESR (load-independent) from the CAPACITIVE
    HF TAIL of the wideband nominal Zout. Above all resonances the L-branch is
    high-Z and Z -> ESR + 1/(jwC), so ESR = Re(Z) and C = -1/(w*Im(Z)). Reading
    the tail directly (not a full fit) is robust to multi-pole mid-band shapes
    (a full fit gets pulled off, e.g. V3 -> 1606pF). Replaces the old hardcoded
    1n/0.5 so the fitter works on LDOs with any output cap."""
    kz, khf = f"z_{NOMINAL}", f"z_{NOMINAL}_hf"
    g = ref[khf] if khf in ref.files else ref[kz]
    f = g[:, 0]; Z = g[:, 1] + 1j * g[:, 2]
    # CROSS-CHECK z_hf against the in-band z where they overlap: a z_hf exported with a
    # wrong scale/units (real-Cadence trap) silently poisons this extraction while every
    # other consumer of z_<corner> looks fine. On mismatch, trust z and say so.
    if khf in ref.files and kz in ref.files:
        gz = ref[kz]; fz = gz[:, 0]; Zz = gz[:, 1] + 1j * gz[:, 2]
        m = (fz >= f[0]) & (fz <= f[-1])
        if m.sum() >= 3:
            zi = np.interp(np.log(fz[m]), np.log(f), np.abs(Z))
            mis = float(np.median(np.abs(20 * np.log10(zi / np.abs(Zz[m])))))
            if mis > 6.0:
                print(f"  WARNING: z_hf disagrees with z by {mis:.1f}dB (median, overlap band) -> "
                      f"ignoring z_hf for Cout/ESR; check the z_hf export scale/units")
                f, Z = fz, Zz
    # GATED C_ft DE-EMBED: the Zout bench AC-grounds vin, so an enabled vin->vout
    # feedthrough cap shunts the output during the Zout measurement. Remove it so
    # the remaining branch-C extraction sees the PHYSICAL output cap only.
    if CFT > 0.0:
        Z = 1.0 / (1.0 / Z - 1j * TWO_PI * f * CFT)
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
    # GHOST-CAP GATE: in the very band the C estimate came from, a real shunt cap
    # DOMINATES Z, so |Z| ~ |ESR+1/jwC| there (the extraction's own premise). On a
    # capless part (HF = rds/parasitics, not 1/jwC; or a near-real Im from a noisy
    # HF export) the median lands on a huge ghost cap whose branch sits ORDERS below
    # the measured |Z| -- impossible for a shunt that big (real 5.8G LDO: 14nF
    # claimed against a 681ohm peak at 10MHz). Judge only on capacitive points
    # (Im<0 keeps v7-style inductive ESL tails out; the LC tank peak itself has
    # |phase|<45deg so it never enters `cap` -- no false fire on a high-Q peak,
    # which sits legitimately ABOVE both branches). If the data sits >12dB above
    # the branch (or C came out non-positive), fall back to the LARGEST cap whose
    # impedance clears |Z| everywhere: C = 1/(2pi*max(f*|Z|)) -- it still provides
    # the final HF rolloff but leaves the R/L branches free to fit the mid-band.
    band = sel & (Z.imag < 0)
    ghost = Cc <= 0
    if not ghost and band.sum() >= 3:
        zb = np.abs(Rc + 1.0 / (1j * TWO_PI * f[band] * Cc))
        ghost = bool(np.median(np.abs(Z[band]) / zb) > 4.0)
    if not ghost:
        # ...and NOWHERE in the sweep may the data sit far above the claimed branch.
        # A real LC tank peak does sit above both branches (|Ztot|~Q*|Zc|, Q<~10 in
        # this family) so the threshold is generous (26dB); a ghost cap misses by
        # 50dB+. Catches the ghost-C + tail-resistance combo the band test passes.
        zb = np.abs(Rc + 1.0 / (1j * TWO_PI * f * Cc))
        ghost = bool(np.max(np.abs(Z) / zb) > 20.0)
    if ghost:
        Cc_med = Cc
        Cc = float(1.0 / (TWO_PI * np.max(f * np.abs(Z))))
        # GHOST ADJUDICATION (multi-stage PDN vs true ghost). The two ratio tests
        # above cannot tell a REAL bulk cap behind a decoupling ladder (v10_3lc:
        # 200p on-die + 3n -> 2n -> 8n -> 10n; the L-isolated anti-resonances sit
        # 40dB+ above the 10nF branch exactly like a ghost does) from a genuinely
        # impossible shunt (real capless 5.8G part: 14nF vs a 681ohm peak). What
        # CAN tell them apart is the evidence: fit the full Zout model with each
        # candidate C and keep the one that explains the measured nominal Zout
        # (keep-best with a 1dB margin, envelope = the safe default). A true ghost
        # shorts the band where the data is high -> its fit is FAR worse (digest:
        # env wins by >10dB); v10's 10nF bulk read wins by ~6dB (composite 57 vs
        # 161 scored). Gate-silent variants never reach this -> byte-identical.
        kz2 = f"z_{NOMINAL}"
        if np.isfinite(Cc_med) and Cc_med > 0 and kz2 in ref.files:
            gz2 = ref[kz2]
            fz2, Zz2 = gz2[:, 0], gz2[:, 1] + 1j * gz2[:, 2]
            sel2 = fz2 >= 1e3                   # the score band (score.py zrms)

            def _zrms_with(cand):
                global C, RC
                keep = (C, RC)
                C, RC = cand, Rc
                try:
                    Zm = zmodel(fz2, *fit_zout(fz2, Zz2))
                    return float(np.sqrt(np.mean(
                        (20 * np.log10(np.abs(Zm[sel2]) / np.abs(Zz2[sel2]))) ** 2)))
                finally:
                    C, RC = keep
            r_med, r_env = _zrms_with(Cc_med), _zrms_with(Cc)
            if r_med < r_env - 1.0:
                print(f"  ghost-cap gate OVERTURNED by evidence: median C={Cc_med*1e12:.2f}pF "
                      f"fits nominal Zout to {r_med:.2f}dB vs envelope {Cc*1e12:.2f}pF "
                      f"{r_env:.2f}dB -> real shunt behind a multi-stage PDN; keeping median")
                return Cc_med, Rc
        print(f"  ghost-cap gate: HF band is not a shunt cap (capless part or bad z_hf) -> "
              f"Cout={Cc*1e12:.2f}pF (envelope fallback), ESR={Rc:.3f}ohm")
    return Cc, Rc


def load(vkey="base", nominal=None, vref=None):
    """Load a variant reference and auto-extract its physical Cout/ESR.
    nominal: nominal corner KEY (default = middle of `loads`, the contract's nom corner).
    vref:    nominal vin reference for the emitted model (default = keep module VREF=1.05).
    Both default to the legacy behavior, so existing callers are unchanged."""
    global ref, LOADS, NOMINAL, VREF, C, RC, CFT
    CFT = 0.0                      # reset per load (multi-variant processes)
    ref = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
    LOADS = [str(x) for x in ref["loads"]]
    NOMINAL = nominal if nominal is not None else LOADS[len(LOADS) // 2]
    if vref is not None:
        VREF = float(vref)
    fit_cft()                      # BEFORE fit_cout_esr: de-embed C_ft from Zout
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
    Y = 1.0 / ZA + 1.0 / ZB + 1.0 / ZC
    if CFT > 0.0:                 # gated vin->vout feedthrough cap: the Zout bench
        Y = Y + s * CFT            # AC-grounds vin -> C_ft shunts the output
    return 1.0 / Y


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
MNOISE = 6   # Norton-@vout noise sections (white floor + MNOISE Lorentzians);
             # fit_all() RAISES this (adaptive, max NOISE_M_MAX) when the joint fit
             # cannot hold the shape -- real capless part: flicker/RTN keeps rising
             # through the bottom decades while the In-dip at the Zout peak competes
             # for sections; 6 corners over 6+ decades then sacrifice the LF tail
             # (-20dB @1kHz on the real 5.8G LDO). All 14 synthetic refs fit <=3.7dB
             # at M=6, below the trigger -> they never adapt = byte-identical.
NOISE_ADAPT_TRIG = 4.0   # dB worst-corner Sv log-RMS that triggers growing the bank
NOISE_M_MAX = 10         # adaptive ceiling (sections are cheap: R||C + VCCS each)
NFK = []     # module global: shared Lorentzian corner freqs from the last noise fit
NOISE_MODE = "norton"   # noise-block structure: "norton" (legacy Norton current bank
             # @vout) | "hybrid" (GATED: series voltage-noise bank in branch A + the
             # Norton white floor). The hybrid is attempted ONLY when the adaptive
             # Norton fit stalls above NOISE_ADAPT_TRIG (real loop-shaped parts where
             # In=Sv/|Zout| falls steeper than -20dB/dec at LF) and kept ONLY if
             # strictly better -- all 14 synthetic refs stay on "norton" untouched.
NFKV = []    # module global: shared series voltage-bank Lorentzian corner freqs (hybrid)
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
    if CFT > 0.0:                 # gated feedthrough cap injects i = sC_ft*(vin-vout);
        i_c = i_c + s * CFT        # to 1st order (|H|<<1) that is an extra sC_ft term
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
        if CFT > 0.0:             # psrr_model adds sC_ft itself -> de-tail the bank
            ic = ic - 1j * TWO_PI * f * CFT          # target (else the tail is fit twice)
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
    ic_t = H / zmodel(f, *zf)
    if CFT > 0.0:                 # de-tail: psrr_model adds the sC_ft term itself
        ic_t = ic_t - 1j * TWO_PI * f * CFT
    sk = _sk_fit(1j * TWO_PI * f, ic_t, NPS)
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
    # GATED full-real-bank polish (feedthrough parts only): with C_ft enabled the
    # de-tailed i_c keeps a +90deg LF high-pass character (G0 ~ -G1 cancellation)
    # that the 1-section shelf cannot carry, SK refuses (complex poles) and the
    # AAA-init complex bank degenerates on (Target B: shelf 6.3deg -> 2.3deg).
    # Polish all NPS real sections from the shelf init on the exact realizable
    # form; keep-best decides. Gated on CFT -> legacy path byte-identical.
    if CFT > 0.0:
        w0lo, w0hi = TWO_PI * f[0] / 10.0, TWO_PI * f[-1] * 10.0

        def unpack3(p):
            return [p[0], p[1], np.exp(p[2]), p[3], np.exp(p[4]), p[5], np.exp(p[6])]

        def resid3(p):
            r = np.log(psrr_model(f, *zf, unpack3(p))) - np.log(H)
            return np.concatenate([r.real, r.imag])
        p0 = np.array([G_shelf[0], G_shelf[1],
                       np.log(np.clip(G_shelf[2], w0lo, w0hi)),
                       0.0, np.log(TWO_PI * f[0] * 10), 0.0,
                       np.log(TWO_PI * f[len(f) // 2])])
        lo3 = np.array([-np.inf, -np.inf, np.log(w0lo), -np.inf, np.log(w0lo),
                        -np.inf, np.log(w0lo)])
        hi3 = np.array([np.inf, np.inf, np.log(w0hi), np.inf, np.log(w0hi),
                        np.inf, np.log(w0hi)])
        try:
            s3 = least_squares(resid3, p0, bounds=(lo3, hi3), method="trf",
                               max_nfev=20000)
            G3 = unpack3(s3.x)
            if np.all(np.isfinite(G3)):
                cands.append(("bank3", G3, Q0, _psrr_resid(f, H, zf, G3, Q0)))
        except Exception:
            pass
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
        In2 = (Sv / Zmod) ** 2                         # In^2 target
        # GRID EQUALIZATION: the log-residual weighs every SAMPLE equally, so a
        # linear-frequency noise export (typical pnoise binning; real 5.8G LDO) puts
        # ~no samples below 100kHz -> the flicker/RTN tail simply is not in the fit
        # (-20dB @1kHz). If the grid is not log-uniform, resample onto a uniform log
        # grid (24/dec) first. Log-uniform exports (all 14 refs) skip this untouched.
        dl = np.diff(np.log(f))
        pos = dl[dl > 0]
        if pos.size and np.max(dl) > 2.5 * np.min(pos):
            fu = np.logspace(np.log10(f[0]), np.log10(f[-1]),
                             max(int(24 * np.log10(f[-1] / f[0])), 24))
            In2 = np.exp(np.interp(np.log(fu), np.log(f), np.log(In2 + 1e-80)))
            f = fu
            print(f"      noise grid for {il} is not log-uniform -> "
                  f"resampled to {len(fu)} log points for the bank fit")
        targets[il] = (f, In2)
    f0 = targets[LOADS[0]][0][0]; f1 = targets[LOADS[0]][0][-1]
    nL = len(LOADS)
    MIN_LOG_GAP = np.log(2.0)              # adjacent corners >= 2x apart (anti-degeneracy)

    def fit_M(M, fks_init=None):
        def model_il(fks, row, f):                      # row=[logwhite, logamp_1..M]
            out = np.exp(row[0]) * np.ones_like(f)
            for k in range(M):
                out = out + np.exp(row[1 + k]) / (1.0 + (f / fks[k]) ** 2)
            return out

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

        if fks_init is not None:
            fks0 = np.log(np.sort(np.asarray(fks_init, float)))
        else:
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
        # worst-corner Sv-domain log-RMS (Sv=sqrt(In2)*|Z| with the same Z -> the In2
        # log-ratio /2 IS the Sv dB error); drives the adaptive-M trigger below
        worst = 0.0
        for j, il in enumerate(LOADS):
            f, In2 = targets[il]
            m = model_il(fks, rest[j], f)
            worst = max(worst, float(np.sqrt(np.mean(
                (10.0 * np.log10((m + 1e-80) / (In2 + 1e-80))) ** 2))))
        order = np.argsort(fks)             # SORT sections ascending by corner freq so
        fks = fks[order]                    # 'section k' is the same pole at every corner
        gw, gk = {}, {}
        for j, il in enumerate(LOADS):
            gw[il] = float(np.sqrt(max(np.exp(rest[j, 0]), 1e-44) / (KT4 * NRk)))
            gk[il] = [float(np.sqrt(max(np.exp(rest[j, 1 + k]), 1e-44) / (KT4 * NRk)))
                      for k in order]
        return dict(fk=[float(x) for x in fks], gw=gw, gk=gk), worst

    def worst_point_freq(NB):
        """Frequency where the current bank misfits worst (max log-error over corners)."""
        fbest, ebest = None, -1.0
        for il in LOADS:
            f, In2t = targets[il]
            m = (NB["gw"][il] ** 2) * KT4 * NRk * np.ones_like(f)
            for k, fk in enumerate(NB["fk"]):
                m = m + (NB["gk"][il][k] ** 2) * KT4 * NRk / (1.0 + (f / fk) ** 2)
            e = np.abs(10.0 * np.log10((m + 1e-80) / (In2t + 1e-80)))
            j = int(np.argmax(e))
            if e[j] > ebest:
                ebest, fbest = float(e[j]), float(f[j])
        return fbest

    # ADAPTIVE M: start at the legacy M (bit-identical when it suffices). While the
    # worst corner misfits by > NOISE_ADAPT_TRIG, GREEDILY INSERT one section at the
    # worst-fit frequency and refit warm-started from the current corners (a fresh
    # logspace init at larger M lands in worse local minima on these shapes). Stop
    # when an insertion stops helping -- a residual that no added low-pass section
    # can remove (e.g. the In-dip at the Zout peak: the bank is monotone-down + white,
    # it cannot dip below its own HF floor; that is the known loop-noise-shape bound).
    best, wbest = fit_M(M)
    while wbest > NOISE_ADAPT_TRIG and len(best["fk"]) < NOISE_M_MAX:
        fstar = worst_point_freq(best)
        fstar = float(np.clip(fstar, f0 / 5 * 1.01, f1 * 5 * 0.99))
        NB2, w2 = fit_M(len(best["fk"]) + 1, fks_init=list(best["fk"]) + [fstar])
        if w2 < wbest - 1e-9:
            best, wbest = NB2, w2
        else:
            break
    if len(best["fk"]) != M:
        print(f"      noise bank ADAPTED: {M} -> {len(best['fk'])} Lorentzians "
              f"(worst-corner Sv fit {wbest:.2f}dB)")
    best["worst"] = wbest          # additive: drives the gated hybrid attempt in fit_all
    return best


def _za(f, R_a, L_a, R_pl):
    """Branch-A series impedance ZA = R_a + jwL_a||R_pl (hybrid noise path only).
    A series voltage source between the regulation rail and R_a reaches vout via
    T = Zsh/(ZA+Zsh) = Zout/ZA (Zsh = the parallel rest: branches B, C, gated C_ft;
    the noise bench load is a current source = AC-open), so T is computed from the
    SAME zmodel Zout the rest of the fit uses."""
    s = 1j * TWO_PI * f
    return R_a + (s * L_a * R_pl) / (s * L_a + R_pl)


def fit_noise_hybrid(zfits, M=4):
    """GATED alternative noise structure ("hybrid"): a VOLTAGE-noise bank in series
    with branch A (between the regulation source and R_a) + the existing Norton
    WHITE floor at vout:
        Sv_model^2 = Vn^2(f)*|T(f)|^2 + (gnw*sqrt(KT4*NRk))^2*|Zout(f)|^2
        Vn^2(f)    = snw^2*KT4*NRk + sum_k snk^2*KT4*NRk/(1+(f/fvk)^2)
        T(f)       = Zout(f)/ZA(f),   ZA = R_a + jwL_a||R_pl
    Why: on a real loop-shaped part In = Sv/|Zout| falls ~-34dB/dec at LF -- steeper
    than any Lorentzian sum (each falls at most -20dB/dec in amplitude), so the
    Norton bank stalls (~6.7-6.8dB worst corner on Target B). A voltage bank AHEAD
    of the output divider carries that shape naturally (prototype: 0.5-0.8dB with 3
    active Lorentzians whose amplitudes are load-independent to 0.1%).
    JOINT LS over the load corners, same recipe as fit_noise_bank: shared log corner
    freqs fvk + per-corner amplitudes, grid equalization for non-log-uniform exports,
    MIN_LOG_GAP separation penalty on the shared log-corners, log-domain residual,
    1e-44 amplitude floor before the sqrt->gain conversion. Greedy section insertion
    up to 8 while worst > NOISE_ADAPT_TRIG (M=4 sufficed on the real part).
    Returns dict(fkv=[..], snw={il}, snk={il:[..]}, gw={il}, worst=...)."""
    targets = {}
    for il in LOADS:
        gn = ref[f"noise_{il}"]; f = gn[:, 0]; Sv = gn[:, 1]
        # GRID EQUALIZATION (same rule as fit_noise_bank): resample non-log-uniform
        # exports onto a uniform log grid so the LF tail is actually in the fit.
        dl = np.diff(np.log(f))
        pos = dl[dl > 0]
        if pos.size and np.max(dl) > 2.5 * np.min(pos):
            fu = np.logspace(np.log10(f[0]), np.log10(f[-1]),
                             max(int(24 * np.log10(f[-1] / f[0])), 24))
            Sv = np.exp(np.interp(np.log(fu), np.log(f), np.log(Sv + 1e-80)))
            f = fu
        Z = zmodel(f, *zfits[il])
        T2 = np.abs(Z / _za(f, zfits[il][0], zfits[il][1], zfits[il][2])) ** 2
        targets[il] = (f, Sv ** 2, T2, np.abs(Z) ** 2)
    f0 = targets[LOADS[0]][0][0]; f1 = targets[LOADS[0]][0][-1]
    nL = len(LOADS)
    MIN_LOG_GAP = np.log(2.0)              # adjacent corners >= 2x apart (anti-degeneracy)

    def fit_M(M, fks_init=None):
        def model_il(fks, row, il):        # row = [log snw2, log snk2_1..M, log gw2]
            f, _, T2, Z2 = targets[il]
            Vn2 = np.exp(row[0]) * np.ones_like(f)
            for k in range(M):
                Vn2 = Vn2 + np.exp(row[1 + k]) / (1.0 + (f / fks[k]) ** 2)
            return Vn2 * T2 + np.exp(row[M + 1]) * Z2

        def resid(p):
            fks = np.exp(p[:M])
            rest = p[M:].reshape(nL, M + 2)
            r = []
            for j, il in enumerate(LOADS):
                Sv2 = targets[il][1]
                r.append(np.log(model_il(fks, rest[j], il) + 1e-80) - np.log(Sv2 + 1e-80))
            gaps = np.diff(np.sort(p[:M]))
            r.append(30.0 * np.maximum(0.0, MIN_LOG_GAP - gaps))
            return np.concatenate(r)

        if fks_init is not None:
            fks0 = np.log(np.sort(np.asarray(fks_init, float)))
        else:
            fks0 = np.log(np.logspace(np.log10(f0 * 1.5), np.log10(f1 / 1.5), M))
        init = list(fks0)
        lob = list(np.log(np.full(M, f0 / 5))); hib = list(np.log(np.full(M, f1 * 5)))
        for il in LOADS:
            f, Sv2, T2, Z2 = targets[il]
            Vt2 = Sv2 / (T2 + 1e-80)             # voltage-domain target (Sv/|T|)^2
            init += [float(np.log(np.min(Vt2) + 1e-80))]          # snw2: white <= floor
            init += list(np.log(np.interp(np.exp(fks0), f, Vt2) + 1e-80))
            init += [float(np.log(np.mean(Sv2[-3:] / (Z2[-3:] + 1e-80)) + 1e-80))]  # gw2
            lob += [-200.0] * (M + 2); hib += [60.0] * (M + 2)
        s = least_squares(resid, init, bounds=(lob, hib), method="trf", max_nfev=30000)
        fks = np.exp(s.x[:M]); rest = s.x[M:].reshape(nL, M + 2)
        # worst-corner Sv-domain log-RMS (Sv^2 log-ratio in 10log10 IS the Sv dB error)
        worst = 0.0
        for j, il in enumerate(LOADS):
            Sv2 = targets[il][1]
            m = model_il(fks, rest[j], il)
            worst = max(worst, float(np.sqrt(np.mean(
                (10.0 * np.log10((m + 1e-80) / (Sv2 + 1e-80))) ** 2))))
        order = np.argsort(fks)            # SORT ascending so 'section k' is the same
        fks = fks[order]                   # pole at every corner (emit interpolates)
        snw, snk, gw = {}, {}, {}
        for j, il in enumerate(LOADS):
            snw[il] = float(np.sqrt(max(np.exp(rest[j, 0]), 1e-44) / (KT4 * NRk)))
            snk[il] = [float(np.sqrt(max(np.exp(rest[j, 1 + k]), 1e-44) / (KT4 * NRk)))
                       for k in order]
            gw[il] = float(np.sqrt(max(np.exp(rest[j, M + 1]), 1e-44) / (KT4 * NRk)))
        return dict(fkv=[float(x) for x in fks], snw=snw, snk=snk, gw=gw), worst

    def worst_point_freq(NH):
        """Frequency where the hybrid misfits worst (max log-error over corners)."""
        fbest, ebest = None, -1.0
        for il in LOADS:
            f, Sv2, T2, Z2 = targets[il]
            Vn2 = (NH["snw"][il] ** 2) * KT4 * NRk * np.ones_like(f)
            for k, fk in enumerate(NH["fkv"]):
                Vn2 = Vn2 + (NH["snk"][il][k] ** 2) * KT4 * NRk / (1.0 + (f / fk) ** 2)
            m = Vn2 * T2 + (NH["gw"][il] ** 2) * KT4 * NRk * Z2
            e = np.abs(10.0 * np.log10((m + 1e-80) / (Sv2 + 1e-80)))
            j = int(np.argmax(e))
            if e[j] > ebest:
                ebest, fbest = float(e[j]), float(f[j])
        return fbest

    best, wbest = fit_M(M)
    while wbest > NOISE_ADAPT_TRIG and len(best["fkv"]) < 8:
        fstar = float(np.clip(worst_point_freq(best), f0 / 5 * 1.01, f1 * 5 * 0.99))
        NH2, w2 = fit_M(len(best["fkv"]) + 1, fks_init=list(best["fkv"]) + [fstar])
        if w2 < wbest - 1e-9:
            best, wbest = NH2, w2
        else:
            break
    best["worst"] = wbest
    return best


def fit_all():
    global NFK, MNOISE, NOISE_MODE, NFKV
    NOISE_MODE = "norton"          # reset per fit (precedent: fit_spurs resets NSPUR_*)
    NFKV = []
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
    MNOISE = len(NFK)                        # adaptive bank size -> emit/resid follow
    # GATED hybrid noise structure: attempted ONLY when the adaptive Norton bank
    # STALLED above the trigger (all 14 synthetic refs fit <=3.7dB -> never fires
    # there) and kept ONLY if strictly better (keep-best -> zero regression).
    if NB["worst"] > NOISE_ADAPT_TRIG:
        NH = fit_noise_hybrid(zfits)
        # STRUCTURAL-SWITCH MARGIN: only change topology for a clear (>0.5dB) win --
        # a marginal 0.05dB "improvement" must not flap the emitted structure.
        if NH["worst"] < NB["worst"] - 0.5:
            NOISE_MODE = "hybrid"
            NFKV = NH["fkv"]
            NFK = []
            MNOISE = 0
            print(f"      noise HYBRID engaged: series bank {len(NFKV)} sections @ fvk="
                  + " ".join(f"{x:.3g}" for x in NFKV)
                  + f" (worst {NB['worst']:.2f} -> {NH['worst']:.2f} dB)")
    if NOISE_MODE == "hybrid":
        for il in LOADS:
            P[il]["gnw"] = NH["gw"][il]      # Norton WHITE floor gain reused (same name)
            P[il]["snw"] = NH["snw"][il]     # series-bank white + Lorentzian gains;
            for k in range(len(NFKV)):       # sn* avoids ngspice's CASE-INSENSITIVE
                P[il][f"sn{k+1}"] = NH["snk"][il][k]   # clash space (no g/G names)
            nr = _noise_resid(il, zfits[il], P[il])
            p = P[il]
            cmark = (f"{p['pcw0']/TWO_PI/1e6:.2f}MHz/Q{p['pcq']:.1f}" if p['_cpx'] else "")
            print(f"{il:>5} | {p['R_a']:7.2f} {p['L_a']*1e6:8.3f} {p['R_pl']:9.1f} {p['R_b']:9.1f} "
                  f"{p['_zrms']:6.2f} | {p['G0']:+9.2e} {p['G1']:+9.2e} {p['w1']/TWO_PI/1e6:7.3f} "
                  f"{p['_nsec']:3d}{'*' if p['_cpx'] else ' '} {p['_prms']:6.2f} {p['_pph']:5.1f} | "
                  f"{nr:6.2f} {p['vreg']*1e3:7.2f}  {cmark}")
    else:
        for il in LOADS:
            P[il]["gnw"] = NB["gw"][il]          # gnw/gn1.. avoid ngspice's CASE-INSENSITIVE
            for k in range(MNOISE):              # param clash with PSRR's G1/G2/G3
                P[il][f"gn{k+1}"] = NB["gk"][il][k]
            nr = _noise_resid(il, zfits[il], P[il])
            p = P[il]
            cmark = (f"{p['pcw0']/TWO_PI/1e6:.2f}MHz/Q{p['pcq']:.1f}" if p['_cpx'] else "")
            print(f"{il:>5} | {p['R_a']:7.2f} {p['L_a']*1e6:8.3f} {p['R_pl']:9.1f} {p['R_b']:9.1f} "
                  f"{p['_zrms']:6.2f} | {p['G0']:+9.2e} {p['G1']:+9.2e} {p['w1']/TWO_PI/1e6:7.3f} "
                  f"{p['_nsec']:3d}{'*' if p['_cpx'] else ' '} {p['_prms']:6.2f} {p['_pph']:5.1f} | "
                  f"{nr:6.2f} {p['vreg']*1e3:7.2f}  {cmark}")
        print(f"      noise corners fk[Hz] = " + " ".join(f"{x:.3g}" for x in NFK))
    fit_spurs(P, zfits)
    return P


def noise_model_sv(P_il, f, Z, nfk=None, nfkv=None, nmode=None):
    """THE shared model output-noise reconstruction Sv [V/rtHz] -- single source of
    truth used by fit_all's per-corner print (_noise_resid), predict() (the GUI /
    report / crossval path) and _selftest, so they agree to machine precision.
    Dispatches on nmode (None -> the module NOISE_MODE of the LAST fit; a held
    FitResult must pass its own result.nmode/result.nfkv or a later fit in the same
    process silently re-dispatches it):
      norton (legacy): Sv = sqrt(In^2)*|Zout|, In^2 = white + Lorentzians @ nfk
      hybrid (gated):  Sv^2 = Vn^2(f)*|Zout/ZA|^2 + (gnw^2*KT4*NRk)*|Zout|^2,
                       Vn^2 = (snw^2 + sum snk^2/(1+(f/fvk)^2))*KT4*NRk
    Z is the COMPLEX zmodel Zout already evaluated at f (so the C_ft shunt, branch B
    etc. are inherited); nfk/nfkv default to the module state of the last fit."""
    f = np.asarray(f, dtype=float)
    if nmode is None:
        nmode = NOISE_MODE
    if nmode == "hybrid":
        if nfkv is None:
            nfkv = NFKV
        ZA = _za(f, P_il["R_a"], P_il["L_a"], P_il["R_pl"])
        T2 = np.abs(Z / ZA) ** 2
        Vn2 = (P_il.get("snw", 0.0) ** 2) * KT4 * NRk * np.ones_like(f)
        for k in range(len(nfkv)):
            sk = P_il.get(f"sn{k+1}", 0.0)
            Vn2 = Vn2 + (sk ** 2) * KT4 * NRk / (1.0 + (f / nfkv[k]) ** 2)
        return np.sqrt(Vn2 * T2 + (P_il["gnw"] ** 2) * KT4 * NRk * np.abs(Z) ** 2)
    if nfk is None:
        nfk = NFK
    In2 = (P_il["gnw"] ** 2) * KT4 * NRk * np.ones_like(f)
    for k in range(len(nfk)):
        gk = P_il.get(f"gn{k+1}", 0.0)
        In2 = In2 + (gk ** 2) * KT4 * NRk / (1.0 + (f / nfk[k]) ** 2)
    return np.sqrt(In2) * np.abs(Z)


def predict(P_il, f, nfk=None, nfkv=None, nmode=None):
    """ANALYTIC Zout / PSRR / output-noise PSD for ONE load corner, from its fitted
    params P_il (= one entry of fit_all()'s dict) at frequencies f. Uses the SAME
    transfer functions the fitter optimizes -- zmodel (Zout), psrr_model (PSRR=i_c*Zout)
    and the shared noise reconstruction noise_model_sv (identical to _noise_resid) --
    so the GUI's before/after overlay IS the fit quality itself: pure
    numpy, NO simulator. This is what Tab 4 (Compare) plots against the imported GT.

    nfk: the shared noise-corner freqs (module NFK after a fit; FitResult carries a copy).
    nfkv: hybrid series-bank corner freqs (module NFKV fallback; FitResult.nfkv copy).
    nmode: noise-block structure ("norton"|"hybrid"; None -> module NOISE_MODE). A held
    FitResult MUST pass result.nmode (+ result.nfkv) or a fit of ANOTHER variant in the
    same process changes what its noise prediction means.
    Returns dict(Zout=complex[], PSRR=complex[], noise=Sv[V/rtHz]) on the given f grid."""
    if nfk is None:
        nfk = NFK
    f = np.asarray(f, dtype=float)
    zf = (P_il["R_a"], P_il["L_a"], P_il["R_pl"], P_il["R_b"], P_il["L_b"])
    Z = zmodel(f, *zf)
    G = [P_il["G0"], P_il["G1"], P_il["w1"], P_il["G2"], P_il["w2"], P_il["G3"], P_il["w3"]]
    Q = (P_il["pcb0"], P_il["pcb1"], P_il["pcw0"], P_il["pcq"])
    H = psrr_model(f, *zf, G, Q)
    Sv = noise_model_sv(P_il, f, Z, nfk=nfk, nfkv=nfkv, nmode=nmode)
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
    cft: float = 0.0     # gated vin->vout feedthrough cap (0.0 = gate silent/disabled)
    nmode: str = "norton"                    # noise-block structure ("norton" | gated "hybrid")
    nfkv: list = field(default_factory=list)  # hybrid series-bank corner freqs (empty = norton)


def fit_variant(vkey, nominal=None, vref=None):
    """In-process entry point: load reference <vkey> and fit it -> FitResult. Mirrors what
    __main__ / run_matrix do (load -> fit_all) but returns a self-contained bundle. emit /
    emit_va still read module state, so call them immediately after for the same DUT (the
    harness models one DUT at a time, exactly as the CLI does)."""
    load(vkey, nominal=nominal, vref=vref)
    P = fit_all()
    return FitResult(P=P, loads=list(LOADS), nominal=NOMINAL, cout=C, esr=RC,
                     nfk=list(NFK), spur_f=list(NSPUR_F), spur_ph=list(NSPUR_PH),
                     vref=VREF, cft=CFT, nmode=NOISE_MODE, nfkv=list(NFKV))


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


def _noise_resid(il, zf, P_il):
    """Reconstructed-output-PSD log-RMS (dB) vs GT, for the fitted noise block
    (dispatches on NOISE_MODE through the shared noise_model_sv reconstruction)."""
    gn = ref[f"noise_{il}"]; f = gn[:, 0]; Sv = gn[:, 1]
    Smod = noise_model_sv(P_il, f, zmodel(f, *zf))
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
    per-corner amplitude). Fixed R=NRk + C set each corner fk; gm is interpolated.
    HYBRID (gated): keep the white Norton floor, plus a series VOLTAGE-noise bank
    between the regulation rail (vreg) and branch A's new rail vrgn -- a chain of
    VCVS sections, each sensing an RC-filtered thermal-noise node (R alone = white,
    R||C = Lorentzian @ fvk); the E-gain (snw/sn_k) sets each per-corner amplitude.
    Branch A then hangs off vrgn (Bra line in emit), branches B/C stay on vreg."""
    lines = [f"Rnw  nw 0 {NRk:.6e}", "Gnw  0 vout nw 0 {gnw}      $ white floor"]
    if NOISE_MODE == "hybrid":
        K = len(NFKV)
        lines += ["* series voltage-noise bank (hybrid): vreg -> vrgn feeds branch A",
                  f"Rvw  nvw 0 {NRk:.6e}",
                  f"Evw  vreg {'x1' if K else 'vrgn'} nvw 0 {{snw}}     $ white voltage section"]
        for k in range(K):
            Ck = 1.0 / (TWO_PI * NFKV[k] * NRk)
            nxt = f"x{k+2}" if k < K - 1 else "vrgn"
            lines += [f"Rv{k+1}  nv{k+1} 0 {NRk:.6e}",
                      f"Cv{k+1}  nv{k+1} 0 {Ck:.6e}        $ corner {NFKV[k]:.4g} Hz",
                      f"Ev{k+1}  x{k+1} {nxt} nv{k+1} 0 {{sn{k+1}}}"]
        return "\n".join(lines)
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
             + ([("snw", True)] + [(f"sn{k+1}", True) for k in range(len(NFKV))]
                if NOISE_MODE == "hybrid" else [])
             + [(f"sa{k+1}", True) for k in range(len(NSPUR_F))] + [("vreg", False)])
    defs = "\n".join(f".param {k} = {{{_pexpr(P, k, ls)}}}" for k, ls in specs)
    noise_net = _noise_net()
    spur_net = _spur_net()
    spur_cmt, spur_side = _spur_manifest(path.stem)
    # gated vin->vout feedthrough cap: load-independent -> emitted LITERAL (like COUT/
    # ESR, not interpolated). Both fragments render to the EMPTY STRING when the gate
    # is silent, so the legacy .lib is byte-identical.
    cft_par = (f"\n.param CFTP={CFT:.6e}    $ gated vin->vout feedthrough cap"
               if CFT > 0 else "")
    cft_inst = ("\n* ---- gated vin->vout feedthrough cap (pass-device/package coupling) ----"
                "\nCft vin vout {CFTP}" if CFT > 0 else "")
    # branch A's regulation rail: vreg (legacy) or vrgn (hybrid noise: the series
    # voltage-noise bank from _noise_net sits between vreg and vrgn). Two LITERAL
    # template fragments so the legacy render stays byte-equal. NOTE the
    # (vreg-VREG121) inside pwl() is the .param vreg (a NUMBER), not the node --
    # it stays 'vreg' in BOTH fragments.
    if NOISE_MODE == "hybrid":
        bra_line = (f"Bra  vrgn nA I = {{(slew_en > 0.5) ? "
                    f"pwl(V(vrgn,nA)-(vreg-VREG121),{pwl_tab}) : V(vrgn,nA)/R_a}}")
    else:
        bra_line = (f"Bra  vreg nA I = {{(slew_en > 0.5) ? "
                    f"pwl(V(vreg,nA)-(vreg-VREG121),{pwl_tab}) : V(vreg,nA)/R_a}}")
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
.param COUT={C:.6e} ESR={RC:.6e}    $ auto-extracted physical output cap / ESR{cft_par}
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
{bra_line}
Cout vout nC  {{COUT}}
Resr nC  vreg {{ESR}}
* ---- optional branch B: 2nd parallel R-L for multi-pole Zout (R_b->inf = off) ----
Lb   vout nbb {{L_b}}
Rbb  nbb  vreg {{R_b}}{cft_inst}
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
             + ([("snw", True)] + [(f"sn{k+1}", True) for k in range(len(NFKV))]
                if NOISE_MODE == "hybrid" else [])
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
    # ---- GATED hybrid noise fragments: branch A's rail (arail), the noise-node
    # list, the per-section gains/Cv params and the noise contributions are all
    # selected by NOISE_MODE; the legacy ("norton") selections render BYTE-EQUAL
    # to the previous unconditional template.
    arail = "vrgn" if NOISE_MODE == "hybrid" else "vrg"
    if NOISE_MODE == "hybrid":
        noise_nodes = "nw, vrgn, nvw" + "".join(f", nv{k+1}" for k in range(len(NFKV)))
        gvars = ", ".join(["gnw", "snw"] + [f"sn{k+1}" for k in range(len(NFKV))]
                          + [f"sa{k+1}" for k in range(len(NSPUR_F))])
        Cn_par = "\n  ".join(
            f"parameter real Cv{k+1} = {1.0/(TWO_PI*NFKV[k]*NRk):.6e};   // corner {NFKV[k]:.4g} Hz"
            for k in range(len(NFKV)))
        sersum = " + ".join(["snw*V(nvw)"]
                            + [f"sn{k+1}*V(nv{k+1})" for k in range(len(NFKV))])
        vsec = "\n    ".join(
            f"I(nv{k+1}) <+ V(nv{k+1})/NRk + Cv{k+1}*ddt(V(nv{k+1}));\n"
            f"    I(nv{k+1}) <+ white_noise(4*`P_K*$temperature/NRk, \"nv{k+1}\");"
            for k in range(len(NFKV)))
        noise_va = (
            "// ---- intrinsic output noise (HYBRID): Norton white floor @vout + a series\n"
            f"    // voltage-noise bank in branch A (vrg -> vrgn): white + {len(NFKV)} "
            "Lorentzian V-sections\n"
            "    I(nw)   <+ V(nw)/NRk;\n"
            "    I(nw)   <+ white_noise(4*`P_K*$temperature/NRk, \"nw\");\n"
            "    I(vout) <+ gnw*V(nw);\n"
            f"    V(vrgn, vrg) <+ {sersum};\n"
            "    I(nvw) <+ V(nvw)/NRk;\n"
            "    I(nvw) <+ white_noise(4*`P_K*$temperature/NRk, \"nvw\");\n"
            f"    {vsec}")
    else:
        noise_nodes = f"nw, {nk_nodes}"
        noise_va = (
            "// ---- intrinsic output noise: decoupled Norton current @vout ----\n"
            "    // white floor: R-only node nw (V PSD = 4kT*NRk) transconducted by gw\n"
            "    I(nw)   <+ V(nw)/NRk;\n"
            "    I(nw)   <+ white_noise(4*`P_K*$temperature/NRk, \"nw\");\n"
            "    I(vout) <+ gnw*V(nw);\n"
            f"    // {MNOISE} Lorentzian sections: R||C node nk (corner fk) transconducted by g_k\n"
            f"    {nsec}")
    # deterministic intrinsic spur tones at vout ($abstime -> correct in tran AND PSS/HB).
    # NEGATIVE sign so I(vout)<+ ... injects current INTO vout (matches the SPICE
    # '0 vout' source order), so the reproduced tone phase matches the GT.
    spur_va = "\n    ".join(
        f"I(vout) <+ -sa{k+1}*sin(`M_TWO_PI*{fk:.6e}*$abstime + ({ph:.6f}));"
        for k, (fk, ph) in enumerate(zip(NSPUR_F, NSPUR_PH)))
    if not spur_va:
        spur_va = "// (no intrinsic spurs for this variant)"
    # gated vin->vout feedthrough cap (load-independent literal; empty when silent)
    cft_par_va = (f"\n  parameter real Cft = {CFT:.6e};   // gated vin->vout feedthrough cap"
                  if CFT > 0 else "")
    cft_va = ("\n\n    // ---- gated vin->vout feedthrough cap (pass-device/package) ----"
              "\n    I(vin, vout) <+ Cft*ddt(V(vin, vout));" if CFT > 0 else "")
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
  electrical vin, vout, vrg, nA, nC, nbb, np1, np2, np3, vrf, ncs1, ncs2, {noise_nodes};

  parameter real iload   = {_amps(NOMINAL):.6e} from (0:inf);
  parameter integer slew_en = 0 from [0:1];
  parameter real Cout = {C:.6e};   // auto-extracted physical output cap
  parameter real ESR  = {RC:.6e};   // auto-extracted physical ESR{cft_par_va}
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
    V(nbb, vrg)  <+ R_b*I(nbb, vrg);{cft_va}

    // ---- Zout branch A: (L_a || R_pl) + (R_a | nonlinear dropout) ----
    // L_a||R_pl as summed current contributions (idt form admits the parallel R)
    I(vout, nA) <+ idt(V(vout, nA))/L_a + V(vout, nA)/R_pl;
    if (slew_en == 0)
      V(nA, {arail}) <+ R_a*I(nA, {arail});
    else                             // $table_model control string may be Spectre-version specific
      I({arail}, nA) <+ $table_model(V({arail}, nA) - (vreg - VREG121), "{tbl_path.name}", "1L");

    {noise_va}

    // ---- PSRR path: i_c = G0 + sum Gi*LP_i(vin-1.05) into vout (x Zout) ----
    // signed real-pole section bank (R_i = 1/(wi*Cps), so 1/R_i = wi*Cps)
    I(vin, np1) <+ (V(vin) - V(np1))*(w1*Cps);
    I(np1, vrf) <+ Cps*ddt(V(np1, vrf));
    I(vin, np2) <+ (V(vin) - V(np2))*(w2*Cps);
    I(np2, vrf) <+ Cps*ddt(V(np2, vrf));
    I(vin, np3) <+ (V(vin) - V(np3))*(w3*Cps);
    I(np3, vrf) <+ Cps*ddt(V(np3, vrf));
    // NEGATIVE sign so I(vout)<+ ... injects current INTO vout (matches the SPICE
    // 'G* 0 vout' source order / the .lib +H realization; same convention as spurs).
    I(vout)     <+ -(G0*(V(vin) - V(vrf)) + G1*(V(np1) - V(vrf))
                   + G2*(V(np2) - V(vrf)) + G3*(V(np3) - V(vrf)));

    // ---- PSRR complex-conjugate 2nd-order section (non-min-phase / notch phase) ----
    // series R-L-C lowpass state x=V(ncs2,vrf); b0 reads V_C=x, b1 reads V_R=a1*dx/dt.
    // i_c += (pcb0+pcb1 s)/(1+a1 s+a2 s^2)*(vin-vrf). Inert when pcb0=pcb1=0. No laplace.
    I(vin, ncs1)  <+ V(vin, ncs1)/Rpc;
    I(ncs1, ncs2) <+ idt(V(ncs1, ncs2))/Lpc;
    I(ncs2, vrf)  <+ Cpc*ddt(V(ncs2, vrf));
    I(vout)       <+ -(pcb0*V(ncs2, vrf) + gqb1*V(vin, ncs1));   // INTO vout (see bank above)

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
                              res.P[il]["R_b"], res.P[il]["L_b"]), res.P[il])
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
          f"nominal={res.nominal} vref={res.vref} cout={res.cout*1e12:.1f}pF esr={res.esr:.3f}"
          + (f" cft={res.cft*1e15:.1f}fF" if res.cft > 0 else "")
          + (f" nmode={res.nmode}" if getattr(res, "nmode", "norton") == "hybrid" else ""))
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
