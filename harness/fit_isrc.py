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

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

VDD0 = 1.05
TNOM = 55.0


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
    """In^2(f) = iw^2 + kf/f  (af=1).  Linear LS in 1/f."""
    y = In**2
    x = 1.0 / f
    A = np.vstack([np.ones_like(x), x]).T
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    iw2 = max(coef[0], 0.0)
    kf = max(coef[1], 0.0)
    fit = iw2 + kf * x
    r2 = 1 - np.sum((y - fit) ** 2) / np.sum((y - y.mean()) ** 2)
    return dict(in_white=np.sqrt(iw2), in_kf=kf, in_r2=float(r2))


def _fit_temp(temps, idcT):
    Tk = np.asarray(temps) + 273.15
    b, a = np.polyfit(Tk, idcT, 1)                              # idc = a + b*Tk
    idc55 = a + b * (TNOM + 273.15)
    didt = b                                                    # dIdc/dT [A/K] = [A/degC]
    ptat = idcT[-1] / idcT[0]
    return dict(idc55=idc55, didt=didt, ptat=ptat)


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
    """Idc temperature law (PTAT/CTAT slope), T in degC."""
    return p["idc55"] + p["didt"] * (np.asarray(T_C, float) - TNOM)


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
            f"didt={p['didt']*1e9:+7.3f}nA/C ptat={p['ptat']:.3f} ivR2={p['iv_r2']:.4f}")


if __name__ == "__main__":
    from isrc_variants import VARIANTS
    WORK = HERE.parent / "work_isrc"
    for name in VARIANTS:
        p = fit_isrc(WORK / f"{name}.npz")
        print(_fmt(p))
