"""Fit the BEHAVIORAL current-source model to a GT characterization npz
(harness/isrc_char.py output). This is the deliverable form (behavioral), fit
against the MOS-transistor-level object -- the LDO pattern.

Model per current port  (I_pin = current delivered at `out`, fn of Vo, Vdd, T):

  I_pin(Vo,Vdd,T) = ( Idc(T) + g0*(Vo-vc) + gdd*(Vdd-Vdd0) ) * GATE(Vo)
      Idc(T) = idc55 + didt*(T-55)          [G1 DC bias, G2 temp/PTAT]
      g0     = output conductance 1/rout      [G7/G8]
      gdd    = dIpin/dVdd  (signed)           [G4/G8 PSRR sign]
      GATE   = 0.5*(1+tanh((Vo-Vk)/Vw))  sink / 0.5*(1+tanh((Vhk-Vo)/Vw)) source
               -> compliance knee (saturation->triode)   [G5]
  plus output cap Cp (Y=g0+sCp)               [G7]
  plus current-noise PSD  In^2(f)=iw^2 + kf/f [G3]
  plus 1-pole on the supply term if PSRR rolls within band  [G4 freq]

Returns a flat param dict consumed by emit_isrc.emit_isrc.
"""
import sys
import pathlib

import numpy as np
from scipy.optimize import least_squares

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

VDD0 = 1.05
TNOM = 55.0
TEMP_QUAD_MIN_PTS = 5       # >=5 UNIQUE temps before a quadratic Idc(T) is even attempted
TEMP_QUAD_MIN_GAIN = 0.10   # quadratic must cut SSE by >=10% vs linear (adversarial keep-best)
TEMP_QUAD_RESID_FLOOR = 1e-4  # ...AND the linear fit must miss by >=0.01% RMS (rel. to mean Idc):
                              # below this the data IS linear and the relative-SSE test would
                              # otherwise engage a meaningless curvature term on float-point dust


def gate(Vo, Vk, p, pol, side=None, vhi=None):
    """Compliance knee, ->1 in saturation, ->0 at the compliance limit. The knee SIDE is
    DECOUPLED from pol (a bias reference can be a current SINK whose compliance ceiling is at the
    HIGH-Vo rail -- the real WuR refs -- not at Vo->0). `side` selects the orientation; when None
    it falls back to the legacy pol map (sink->low-side, source->high-side at VDD0):
       'lo'   : tanh( (Vo/Vk)^p )            loses current as Vo->0      (e.g. NMOS sink)
       'hi'   : tanh( ((vhi-Vo)/Vk)^p )      loses current as Vo->vhi    (high-side ceiling; vhi
                                             is a FITTED rail, not assumed = VDD0/supply)
       'none' : 1                            no knee in the swept compliance (flat ref, e.g. PTAT)
    p sets knee SHARPNESS (p~1 soft simple-mirror knee; p>>1 hard cascode knee)."""
    Vo = np.asarray(Vo, float)
    if side is None:
        side = "lo" if pol == "sink" else "hi"
        vhi = VDD0 if vhi is None else vhi
    if side == "none":
        return np.ones_like(Vo)
    if side == "hi":
        arg = ((VDD0 if vhi is None else vhi) - Vo) / Vk
    else:                                                # 'lo'
        arg = Vo / Vk
    return np.tanh(np.power(np.clip(arg, 0.0, None), p))


def _cross_from_top(Vs, Is, level):
    """The Vo on the HIGH-Vo FALLING edge where I crosses `level` (I full below, collapses toward
    the ceiling). Walks down from Vo_max and linear-interpolates the bracketing pair; if the curve
    never falls to `level` within the sweep, returns Vo_max (a lower bound on the ceiling)."""
    for k in range(Vs.size - 1, 0, -1):
        a, b = Is[k - 1], Is[k]                          # a at lower Vo (higher I), b at higher Vo
        if (b < level <= a) or (b <= level < a):
            t = (level - a) / (b - a) if b != a else 0.0
            return float(Vs[k - 1] + t * (Vs[k] - Vs[k - 1]))
    return float(Vs[-1])


def _detect_knee(Vs, Is, Iplat):
    """Detect the compliance knee SIDE + params from the DATA (not from pol):
      'lo'   current collapses as Vo->0   (rises from ~0; NMOS sink)          tanh((Vo/Vk)^p)
      'hi'   current collapses as Vo->vhi  (flat then drops at a ceiling)      tanh(((vhi-Vo)/Vk)^p)
      'none' flat across the whole swept compliance (no knee; e.g. PTAT)       1
    Returns (side, vhi, Vk, p). Robust to the non-monotonic flat-then-collapse curve that the
    legacy interp-on-I assumed away (the root cause of the 63% real-ref misfit)."""
    a10, a90 = np.arctanh(0.1), np.arctanh(0.9)
    if Iplat <= 0 or Vs.size < 2:
        return "none", float(Vs[-1]), max(float(Vs[-1]), 0.05), 1.0
    # A knee SIDE is PROPOSED from the endpoint fractions (sensitive to a sharp end-collapse like
    # the real WuR refs, where only the last point hits 0). Endpoint NOISE can over-propose a
    # spurious knee on a flat ref -> _fit_iv's KEEP-BEST-vs-'none' rejects it on fit quality, so the
    # proposal can stay sensitive here without smoothing away a real sharp collapse.
    flo, fhi = Is[0] / Iplat, Is[-1] / Iplat
    lo_drop, hi_drop = flo < 0.9, fhi < 0.9
    if not lo_drop and not hi_drop:                      # full current at BOTH ends -> no knee
        return "none", float(Vs[-1]), max(float(Vs[-1]), 0.05), 1.0
    if hi_drop and (not lo_drop or fhi <= flo):          # high-side ceiling collapse
        vhi = _cross_from_top(Vs, Is, 0.02 * Iplat)      # where I->~0 near the top (the rail)
        x90 = _cross_from_top(Vs, Is, 0.9 * Iplat)       # upper plateau edge (lower Vo)
        x10 = _cross_from_top(Vs, Is, 0.1 * Iplat)       # closer to vhi (higher Vo)
        u90, u10 = vhi - x90, vhi - x10                  # u = vhi-Vo (gate arg numerator)
        if u90 > u10 > 0:
            p = float(np.log(a90 / a10) / np.log(u90 / u10))
            Vk = float(u90 / a90 ** (1.0 / p))
        else:
            p, Vk = 1.0, max(vhi - x90, 0.05)
        return "hi", float(vhi), float(max(Vk, 1e-3)), float(np.clip(p, 0.3, 12.0))
    # low-side knee (legacy NMOS-sink shape): I rises monotonically from ~0 at Vo=0
    x10 = float(np.interp(0.1 * Iplat, Is, Vs))
    x90 = float(np.interp(0.9 * Iplat, Is, Vs))
    if x10 > 0 and x90 > x10:
        p = float(np.log(a90 / a10) / np.log(x90 / x10))
        Vk = float(x90 / a90 ** (1.0 / p))
    else:
        p, Vk = 1.0, max(x90, 0.05)
    return "lo", float(Vs[-1]), float(max(Vk, 1e-3)), float(np.clip(p, 0.3, 12.0))


def _fit_iv(Vo, I, vc, pol, rout):
    """ANCHOR the operating point (Idc=I(vc)) and the small-signal conductance (g0=1/rout,
    consistent with Y(s)), then DETECT the compliance knee side+params from the data (_detect_knee)
    -- a flat ref gets NO gate, a high-side-ceiling ref gets a fitted-rail high-side knee, an
    NMOS-sink-shaped ref gets the legacy low-side knee. No optimizer fragility (closed-form)."""
    Vo = np.asarray(Vo, float); I = np.asarray(I, float)
    order = np.argsort(Vo)
    Vs, Is = Vo[order], I[order]
    Idc = float(np.interp(vc, Vs, Is))
    g0 = (1.0 if pol == "sink" else -1.0) / max(rout, 1.0)
    Iplat = float(np.median(np.sort(Is)[-8:]))
    side, vhi, Vk, p = _detect_knee(Vs, Is, Iplat)

    def _sse(sd, vh, vk, pp):
        m = (Idc + g0 * (Vs - vc)) * gate(Vs, vk, pp, pol, side=sd, vhi=vh)
        return float(np.sum((Is - m) ** 2))
    # KEEP-BEST vs NO-KNEE: a genuine knee beats 'none' by a wide margin (the real chip), but a
    # spurious knee from endpoint noise (flat ref) or a high-side collapse that does not COMPLETE
    # in the sweep (vhi falls back to Vs[-1]) fits WORSE than no gate -> 'none' wins. This makes
    # the detector self-correcting instead of trusting a hard threshold / a fallback ceiling.
    cand = [(side, vhi, Vk, p)]
    if side != "none":
        cand.append(("none", float(Vs[-1]), max(float(Vs[-1]), 0.05), 1.0))
    side, vhi, Vk, p = min(cand, key=lambda c: _sse(*c))

    m = (Idc + g0 * (Vs - vc)) * gate(Vs, Vk, p, pol, side=side, vhi=vhi)
    ss = 1 - np.sum((Is - m) ** 2) / max(np.sum((Is - Is.mean()) ** 2), 1e-300)
    return dict(idc=Idc, g0=g0, vknee=Vk, knee_p=p, knee_side=side, vhi=float(vhi), iv_r2=float(ss))


def _fit_psrr(f, g):
    """gdd (signed LF) + optional 1-pole. g = dIpin/dVdd [S] complex."""
    gdd = float(g[0].real)
    mag = np.abs(g)
    wp = None
    if mag[0] > 0 and mag[-1] < mag[0] / np.sqrt(2):           # rolls >3dB in band
        fc = float(np.interp(mag[0] / np.sqrt(2), mag[::-1], f[::-1]))
        wp = 2 * np.pi * fc
    return dict(gdd=gdd, psrr_pole_w=wp)


def _fit_noise(f, In):
    """In(f) = sqrt(in_white^2 + in_kf/f)  (af=1). Fit in LOG-AMPLITUDE space (least_squares on
    log(model) - log(In)) so the white floor and the flicker tail are weighted EQUALLY across the
    decades -- which is what the dB PSD score does.

    A linear LS in In^2 (power) is dominated by the LARGE low-freq flicker values and barely
    constrains the small HF white floor, so in_white lands far too high: on the real PMU sinks the
    power LS read in_white 5-30x high and the noise PSD scored 9-15 dB off. The log fit recovers the
    true HF floor -> <1.5 dB on the real i500n/i3p6u/i1p5u (proven against the @noise_i GT)."""
    f = np.asarray(f, float); In = np.asarray(In, float)
    ok = np.isfinite(f) & np.isfinite(In) & (f > 0) & (In > 0)
    f, In = f[ok], In[ok]
    if f.size < 3:                                       # too few points for a stable 2-param fit
        iw = float(In.min()) if f.size else 0.0
        kf = float(max((In.max() ** 2 - iw ** 2) * f.min(), 0.0)) if f.size else 0.0
        return dict(in_white=iw, in_kf=kf, in_r2=0.0)
    order = np.argsort(f); f, In = f[order], In[order]
    lz = np.log(In)
    iw0 = max(float(In.min()), 1e-30)                    # white ~ the spectrum's HF floor (its min)
    kf0 = max(float((In[0] ** 2 - iw0 ** 2) * f[0]), iw0 ** 2 * f[0] * 1e-6)

    def _resid(p):
        return np.log(np.sqrt(np.exp(p[0]) ** 2 + np.exp(p[1]) / f)) - lz
    try:
        s = least_squares(_resid, [np.log(iw0), np.log(kf0)], method="lm", max_nfev=10000)
        iw, kf = float(np.exp(s.x[0])), float(np.exp(s.x[1]))
        if not (np.isfinite(iw) and np.isfinite(kf)):
            raise ValueError("non-finite noise fit")
    except Exception:                                    # robust fallback = the legacy power LS
        x = 1.0 / f
        coef, *_ = np.linalg.lstsq(np.vstack([np.ones_like(x), x]).T, In ** 2, rcond=None)
        iw, kf = float(np.sqrt(max(coef[0], 0.0))), float(max(coef[1], 0.0))
    pred = np.sqrt(iw ** 2 + kf / f)
    r2 = 1.0 - np.sum((np.log(pred) - lz) ** 2) / max(np.sum((lz - lz.mean()) ** 2), 1e-30)
    return dict(in_white=iw, in_kf=kf, in_r2=float(r2))


def _fit_temp(temps, idcT, min_quad_pts=TEMP_QUAD_MIN_PTS, quad_improve=TEMP_QUAD_MIN_GAIN):
    """Idc(T) law referenced to TNOM. LINEAR by default (idc55 + didt*(T-TNOM)); engages a
    2nd-order curvature term d2 ONLY with >=min_quad_pts UNIQUE temps AND an adversarial
    keep-best win (quadratic cuts SSE by >=quad_improve). <5 pts / no-curvature -> d2=0.0 and
    idc55/didt come from the EXACT linear code (byte-identical to the pre-2nd-order model)."""
    temps = np.asarray(temps, float)
    idcT = np.asarray(idcT, float)
    Tk = temps + 273.15
    b, a = np.polyfit(Tk, idcT, 1)                              # idc = a + b*Tk  (UNCHANGED)
    idc55 = a + b * (TNOM + 273.15)
    didt = b                                                    # dIdc/dT [A/K] = [A/degC]
    d2 = 0.0
    # 2nd-order only with enough UNIQUE pts AND a clear keep-best win (real PTAT has bandgap/beta
    # curvature; <5 pts a quadratic is exactly/over-determined -> oscillates). np.unique guards
    # duplicate temps from satisfying the gate.
    if np.unique(temps).size >= int(min_quad_pts):
        x = Tk - (TNOM + 273.15)                               # centered -> VA/predict form
        c2, c1, c0 = np.polyfit(x, idcT, 2)
        lin = a + b * Tk
        quad = c0 + c1 * x + c2 * x * x
        sse_lin = float(np.sum((idcT - lin) ** 2))
        sse_quad = float(np.sum((idcT - quad) ** 2))
        # the linear residual must be physically REAL (>= TEMP_QUAD_RESID_FLOOR RMS-relative to the
        # mean Idc), not float-point/interp dust -- else the relative-SSE keep-best can engage a
        # negligible curvature term on essentially-linear silicon data.
        resid_rms_rel = np.sqrt(sse_lin / idcT.size) / (abs(np.mean(idcT)) + 1e-30)
        if (np.isfinite(c2) and resid_rms_rel > TEMP_QUAD_RESID_FLOOR
                and sse_quad < (1.0 - float(quad_improve)) * sse_lin):
            idc55, didt, d2 = float(c0), float(c1), float(c2)  # quadratic wins (anchored at TNOM)
    ptat = idcT[-1] / idcT[0]
    return dict(idc55=idc55, didt=didt, d2=d2, ptat=ptat)


def fit_isrc(npz_path):
    # accept a path OR an already-loaded mapping (npz/dict view) -- report.py and the
    # air-gap digest_import feed a per-port dict view without round-tripping to disk.
    d = npz_path if hasattr(npz_path, "keys") else np.load(npz_path, allow_pickle=True)
    pol = str(d["pol"])
    vc = float(d["vc"])
    Vo = np.asarray(d["iv_v"], float)
    I = np.asarray(d["iv_i"], float)
    rout = float(d["rout"])
    iv = _fit_iv(Vo, I, vc, pol, rout)
    ps = _fit_psrr(np.asarray(d["psrr_f"], float), np.asarray(d["psrr_g"]))
    nz = _fit_noise(np.asarray(d["nz_f"], float), np.asarray(d["nz_in"], float))
    tp = _fit_temp(np.asarray(d["temps"], float), np.asarray(d["idcT"], float))
    cp = float(d["cp"])
    return dict(name=str(d["name"]), pol=pol, vc=vc, cp=cp,
                **iv, **ps, **nz, **tp)


# --------------------------------------------------------------------------- predict
# Pure-numpy ANALYTIC model curves (the exact transfer fns the fitter anchors) -- the
# current-port twin of fit_model.predict. report.py diffs these vs the GT arrays, no
# simulator. (Emitted-netlist / VA fidelity incl. the probe-sign needs crossval_isrc /
# isrc_spectre.py --sc; this analytic predictor cannot see an emit-side sign bug.)
def predict_iv(p, Vo):
    """DC I-V at TNOM: (Idc + g0*(Vo-vc)) * compliance-knee (data-detected side/ceiling)."""
    Vo = np.asarray(Vo, float)
    return (p["idc"] + p["g0"] * (Vo - p["vc"])) * gate(
        Vo, p["vknee"], p["knee_p"], p["pol"], side=p.get("knee_side"), vhi=p.get("vhi"))


def predict_idcT(p, T_C):
    """Idc temperature law (PTAT/CTAT slope + optional 2nd-order curvature), T in degC.
    p.get('d2',0.0) keeps legacy (linear-only) param dicts valid; d2=0 -> the exact linear law."""
    dT = np.asarray(T_C, float) - TNOM
    return p["idc55"] + p["didt"] * dT + p.get("d2", 0.0) * dT * dT


def predict_y(p, f):
    """Output admittance Y(s) = g0 + s*Cp (magnitude is what report diffs)."""
    w = 2 * np.pi * np.asarray(f, float)
    return p["g0"] + 1j * w * p["cp"]


def predict_psrr(p, f):
    """current-PSRR dIpin/dVdd: signed gdd, +1-pole if it rolls in band."""
    f = np.asarray(f, float)
    wp = p.get("psrr_pole_w")
    if wp:
        return p["gdd"] / (1.0 + 1j * (2 * np.pi * f) / wp)
    return np.full(f.shape, complex(p["gdd"]))


def predict_noise(p, f):
    """output current-noise density In(f) = sqrt(iw^2 + kf/f)."""
    f = np.asarray(f, float)
    return np.sqrt(p["in_white"] ** 2 + p["in_kf"] / f)


def _fmt(p):
    return (f"{p['name']:<16} {p['pol']:<6} idc55={p['idc55']*1e6:7.3f}uA "
            f"g0={p['g0']*1e9:+8.3f}nS vk={p['vknee']:.3f} kp={p['knee_p']:5.2f} "
            f"gdd={p['gdd']*1e9:+9.3f}nS cp={p['cp']*1e15:5.1f}fF "
            f"didt={p['didt']*1e9:+7.3f}nA/C d2={p.get('d2',0.0)*1e9:+7.3f}nA/C2 "
            f"ptat={p['ptat']:.3f} ivR2={p['iv_r2']:.4f}")


if __name__ == "__main__":
    from isrc_variants import VARIANTS
    WORK = HERE.parent / "work_isrc"
    for name in VARIANTS:
        p = fit_isrc(WORK / f"{name}.npz")
        print(_fmt(p))
