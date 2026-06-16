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


def gate(Vo, Vk, p, pol):
    """Compliance knee, anchored at the dropout rail and ->1 in saturation:
       sink:   tanh( (Vo/Vk)^p )           loses current as Vo->0
       source: tanh( ((Vdd-Vo)/Vk)^p )     loses current as Vo->Vdd
    p sets knee SHARPNESS (p~1 soft simple-mirror knee; p>>1 hard cascode knee)."""
    arg = (Vo / Vk) if pol == "sink" else ((VDD0 - Vo) / Vk)
    return np.tanh(np.power(np.clip(arg, 0.0, None), p))


def _fit_iv(Vo, I, vc, pol, rout):
    """ANCHOR the operating point (Idc=I(vc)) and the small-signal conductance
    (g0=1/rout, consistent with Y(s)), then estimate the knee (Vk,p) from the
    10%/90% crossings -- a parameter-free 2-point gate fit, no optimizer fragility."""
    Idc = float(np.interp(vc, Vo, I))
    g0 = (1.0 if pol == "sink" else -1.0) / max(rout, 1.0)
    Iplat = np.median(np.sort(I)[-8:])
    a10, a90 = np.arctanh(0.1), np.arctanh(0.9)
    if pol == "sink":
        x10 = float(np.interp(0.1 * Iplat, I, Vo))
        x90 = float(np.interp(0.9 * Iplat, I, Vo))
    else:
        x10 = VDD0 - float(np.interp(0.1 * Iplat, I[::-1], Vo[::-1]))
        x90 = VDD0 - float(np.interp(0.9 * Iplat, I[::-1], Vo[::-1]))
    if x10 > 0 and x90 > x10:
        p = float(np.log(a90 / a10) / np.log(x90 / x10))
        Vk = float(x90 / a90 ** (1.0 / p))
    else:                                                          # degenerate -> soft default
        p, Vk = 1.0, max(x90, 0.05)
    m = (Idc + g0 * (Vo - vc)) * gate(Vo, Vk, p, pol)
    ss = 1 - np.sum((I - m) ** 2) / np.sum((I - I.mean()) ** 2)
    return dict(idc=Idc, g0=g0, vknee=Vk, knee_p=p, iv_r2=ss)


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
    """DC I-V at TNOM: (Idc + g0*(Vo-vc)) * compliance-knee."""
    Vo = np.asarray(Vo, float)
    return (p["idc"] + p["g0"] * (Vo - p["vc"])) * gate(Vo, p["vknee"], p["knee_p"], p["pol"])


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
