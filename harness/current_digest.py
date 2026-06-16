"""Current-PORT air-gap channel: the CURRENT twin of report.py's [7] GT DIGEST.

A red-zone LDO/PMU model report can now carry the CURRENT-output ports (the bias
sinks/sources) the same way it carries Zout/PSRR/noise: as a machine-readable
[8] CURRENT PORTS block. Pasting the report hands the modeler the actual device-level
current curves (I-V knee, Idc(T)/PTAT, admittance, current-PSRR, current-noise) so the
BEHAVIORAL current model (fit_isrc) can be re-fit and the discrepancy reproduced
LOCALLY -- no npz crosses the gap.

This module is pure numpy (no simulator). It does three things:
  * map current-port GT in/out of the flat `ref` npz under a `iport_<pin>__*` namespace
    (registry key `iports`); `port_view` returns the exact dict fit_isrc consumes.
  * `current_section_lines(ref)`  -> the [8] block: per-port fit-vs-GT scorecard +
    plain-language diagnosis + the log-resampled GT digest (report.py prints it).
  * `parse_current_digest(text)`  -> {pin: fit_isrc-ready dict} (digest_import reads it).

Honest scope (mirrors [7]'s sim-note): the scorecard is the ANALYTIC behavioral fit vs
the device GT -- does the model FORM capture the transistor-level source. Emitted-netlist
fidelity (VA/ngspice, incl. the probe-sign convention) needs the simulator twin
crossval_isrc / cadence/isrc_spectre.py --sc.
"""
import re

import numpy as np

import fit_isrc

PREFIX = "iport_"
# per-port namespaced keys in the flat ref npz
_K = dict(iv="iv", idcT="idcT", y="y", psrr="psrr", noise="noise",
          vc="vc", vdd="vdd", pol="pol", pin="pin")


def _san(pin):
    """ref-key-safe token for a pin name (the human pin is kept verbatim under __pin)."""
    return re.sub(r"[^0-9A-Za-z]+", "_", str(pin)).strip("_") or "p"


def _key(pin, what):
    return f"{PREFIX}{_san(pin)}__{_K[what]}"


def list_iports(ref):
    """Pin names of the current ports embedded in `ref`, in registry order."""
    reg = ref.get("iports")
    if reg is None:
        return []
    return [str(x) for x in np.atleast_1d(reg)]


def embed_port(ref, pin, src):
    """Write one current port's GT into `ref` under the namespace. `src` is any mapping
    with the fit_isrc/isrc_char schema (iv_v/iv_i, ac_f/ac_y, psrr_f/psrr_g, nz_f/nz_in,
    temps/idcT, vc, pol[, cp, rout, vdd]). rout/cp are NOT stored -- reconstructed from
    the admittance curve on read, identically to isrc_char."""
    Y = np.asarray(src["ac_y"])
    PG = np.asarray(src["psrr_g"])
    ref[_key(pin, "iv")] = np.c_[np.asarray(src["iv_v"], float), np.asarray(src["iv_i"], float)]
    ref[_key(pin, "idcT")] = np.c_[np.asarray(src["temps"], float), np.asarray(src["idcT"], float)]
    ref[_key(pin, "y")] = np.c_[np.asarray(src["ac_f"], float), Y.real, Y.imag]
    ref[_key(pin, "psrr")] = np.c_[np.asarray(src["psrr_f"], float), PG.real, PG.imag]
    ref[_key(pin, "noise")] = np.c_[np.asarray(src["nz_f"], float), np.asarray(src["nz_in"], float)]
    ref[_key(pin, "vc")] = np.array(float(src["vc"]))
    ref[_key(pin, "vdd")] = np.array(float(src.get("vdd", fit_isrc.VDD0)))
    ref[_key(pin, "pol")] = np.array(str(src["pol"]))
    ref[_key(pin, "pin")] = np.array(str(pin))
    reg = list_iports(ref)
    if str(pin) not in reg:
        reg.append(str(pin))
    ref["iports"] = np.array(reg)
    return ref


def port_view(ref, pin):
    """Per-port dict in the exact schema fit_isrc consumes (rout/cp reconstructed from Y,
    same formula as isrc_char: rout=1/|Re Y(f0)|, cp=|Im Y(fmax)|/2pi.fmax)."""
    iv = np.asarray(ref[_key(pin, "iv")], float)
    yc = np.asarray(ref[_key(pin, "y")], float)
    pc = np.asarray(ref[_key(pin, "psrr")], float)
    nz = np.asarray(ref[_key(pin, "noise")], float)
    td = np.asarray(ref[_key(pin, "idcT")], float)
    f = yc[:, 0]
    Y = yc[:, 1] + 1j * yc[:, 2]
    rout = 1.0 / abs(Y[0].real) if Y[0].real != 0 else float("inf")
    cp = abs(Y[-1].imag) / (2 * np.pi * f[-1]) if f[-1] > 0 else 0.0
    return dict(name=str(ref[_key(pin, "pin")]), pol=str(ref[_key(pin, "pol")]),
                vc=float(ref[_key(pin, "vc")]), vdd=float(ref[_key(pin, "vdd")]),
                iv_v=iv[:, 0], iv_i=iv[:, 1], ac_f=f, ac_y=Y, rout=rout, cp=cp,
                psrr_f=pc[:, 0], psrr_g=pc[:, 1] + 1j * pc[:, 2],
                nz_f=nz[:, 0], nz_in=nz[:, 1], temps=td[:, 0], idcT=td[:, 1])


# --------------------------------------------------------------- model-vs-GT metrics
def _rms_db(model, gt):
    return float(np.sqrt(np.mean(
        (20 * np.log10((np.abs(model) + 1e-30) / (np.abs(gt) + 1e-30))) ** 2)))


def diff_metrics(view, p):
    """Behavioral-fit vs device-GT residuals for one current port (p = fit_isrc(view))."""
    Vo, I = view["iv_v"], view["iv_i"]
    Iplat = float(np.median(np.sort(I)[-8:]))
    Im = fit_isrc.predict_iv(p, Vo)
    ivrms = float(np.sqrt(np.mean(((Im - I) / (Iplat + 1e-30)) ** 2)) * 100.0)
    Ym = fit_isrc.predict_y(p, view["ac_f"])
    yrms = _rms_db(Ym, view["ac_y"])
    Pm = fit_isrc.predict_psrr(p, view["psrr_f"])
    prms = _rms_db(Pm, view["psrr_g"])
    Nm = fit_isrc.predict_noise(p, view["nz_f"])
    nrms = _rms_db(Nm, view["nz_in"])
    idcT_g = view["idcT"]
    ptat_g = float(idcT_g[-1] / idcT_g[0]) if idcT_g[0] else float("nan")
    Tg = view["temps"]
    idcT_m = fit_isrc.predict_idcT(p, Tg)
    ptat_m = float(idcT_m[-1] / idcT_m[0]) if idcT_m[0] else float("nan")
    ptat_rms = float(np.sqrt(np.mean(((idcT_m - idcT_g) / (idcT_g + 1e-30)) ** 2)) * 100.0)
    gdd_gt = float(view["psrr_g"][0].real)
    return dict(
        pin=view["name"], pol=view["pol"], vc=view["vc"],
        idc_ua=p["idc"] * 1e6, ivrms=ivrms,
        rout_M=(1.0 / abs(p["g0"]) / 1e6 if p["g0"] else float("inf")),
        cp_fF=p["cp"] * 1e15, yrms=yrms,
        gdd_nS=p["gdd"] * 1e9, gdd_sign=("+" if p["gdd"] >= 0 else "-"),
        gt_sign=("+" if gdd_gt >= 0 else "-"),
        sign_ok=((p["gdd"] >= 0) == (gdd_gt >= 0)), prms=prms,
        in1k_fA=float(np.interp(1e3, view["nz_f"], view["nz_in"])) * 1e15, nrms=nrms,
        ptat_g=ptat_g, ptat_m=ptat_m, ptat_rms=ptat_rms,
        pole=p.get("psrr_pole_w"))


def _diagnose(m):
    d = []
    if m["ivrms"] > 5.0:
        d.append(f"port '{m['pin']}': behavioral I-V/knee misfits the device by "
                 f"{m['ivrms']:.1f}% RMS -- raise the knee sharpness p or widen the "
                 f"compliance sweep (the saturation->triode corner is under-resolved).")
    if not m["sign_ok"]:
        d.append(f"port '{m['pin']}': current-PSRR SIGN MISMATCH (model {m['gdd_sign']} "
                 f"vs GT {m['gt_sign']} dIpin/dVdd) -- the supply pushes this bias the "
                 f"OTHER way; check the gdd sign and the sink/source probe convention.")
    elif m["prms"] > 3.0:
        d.append(f"port '{m['pin']}': current-PSRR magnitude off by {m['prms']:.1f}dB "
                 f"-- a 1-pole rolloff in band the flat gdd misses (fit a psrr_pole).")
    if m["nrms"] > 3.0:
        d.append(f"port '{m['pin']}': current-noise PSD shape off by {m['nrms']:.1f}dB "
                 f"-- retune the white floor (in_white) / flicker corner (in_kf).")
    if np.isfinite(m["ptat_g"]) and abs(m["ptat_m"] - m["ptat_g"]) > 0.05 * abs(m["ptat_g"]):
        d.append(f"port '{m['pin']}': temp slope off -- PTAT ratio model {m['ptat_m']:.3f} "
                 f"vs GT {m['ptat_g']:.3f} ({m['ptat_rms']:.1f}% RMS over the temp points).")
    if m["yrms"] > 3.0:
        d.append(f"port '{m['pin']}': output admittance |Y| off by {m['yrms']:.1f}dB "
                 f"-- the g0+sCp form misses a 2nd-order term (cascode/Wilson zero).")
    return d


# ------------------------------------------------------------------ digest resampling
def _logresample_complex(f, Z, ppd=6):
    f = np.asarray(f, float)
    n = max(int(ppd * np.log10(f[-1] / f[0])) + 1, 6) if f[-1] > f[0] else len(f)
    g = np.logspace(np.log10(f[0]), np.log10(f[-1]), n)
    re = np.interp(np.log(g), np.log(f), Z.real)
    im = np.interp(np.log(g), np.log(f), Z.imag)
    return g, re + 1j * im


def _logresample_real(f, y, ppd=6):
    f = np.asarray(f, float)
    n = max(int(ppd * np.log10(f[-1] / f[0])) + 1, 6) if f[-1] > f[0] else len(f)
    g = np.logspace(np.log10(f[0]), np.log10(f[-1]), n)
    return g, np.exp(np.interp(np.log(g), np.log(f), np.log(np.asarray(y, float) + 1e-300)))


def _iv_subsample(Vo, I, npts=64):
    idx = np.unique(np.linspace(0, len(Vo) - 1, min(len(Vo), npts)).astype(int))
    return Vo[idx], I[idx]


def emit_digest_block(ref):
    """The machine-readable [8] data: one '# iport' section per port."""
    L = []
    for pin in list_iports(ref):
        v = port_view(ref, pin)
        L.append(f"  # iport {pin}  pol={v['pol']}  vc={v['vc']:.6g}  vdd={v['vdd']:.6g}")
        Vo, I = _iv_subsample(v["iv_v"], v["iv_i"])
        L.append("  # iv     columns: Vo[V], I[A]")
        L += [f"  {a:.5e}, {b:.6e}" for a, b in zip(Vo, I)]
        L.append("  # idcT   columns: T[C], Idc[A]")
        L += [f"  {a:.4g}, {b:.6e}" for a, b in zip(v["temps"], v["idcT"])]
        gf, gy = _logresample_complex(v["ac_f"], v["ac_y"])
        L.append("  # iy     columns: f[Hz], Yre[S], Yim[S]")
        L += [f"  {a:.5e}, {b.real:.5e}, {b.imag:.5e}" for a, b in zip(gf, gy)]
        pf, pg = _logresample_complex(v["psrr_f"], v["psrr_g"])
        L.append("  # ipsrr  columns: f[Hz], PIre[S], PIim[S]")
        L += [f"  {a:.5e}, {b.real:.5e}, {b.imag:.5e}" for a, b in zip(pf, pg)]
        nf, nin = _logresample_real(v["nz_f"], v["nz_in"])
        L.append("  # inoise columns: f[Hz], In[A/rtHz]")
        L += [f"  {a:.5e}, {b:.5e}" for a, b in zip(nf, nin)]
    return L


def current_section_lines(ref):
    """Full [8] block for report.py: scorecard + diagnosis + machine-readable digest.
    Returns [] when `ref` carries no current ports (back-compat: pure voltage report)."""
    pins = list_iports(ref)
    if not pins:
        return []
    rows = []
    for pin in pins:
        v = port_view(ref, pin)
        rows.append(diff_metrics(v, fit_isrc.fit_isrc(v)))
    L = []
    pr = L.append
    pr("\n[8] CURRENT PORTS  -- behavioral current model vs device GT  (analytic, no sim)")
    pr("-" * 84)
    pr(f"  {'pin':<14}{'pol':<7}{'Idc[uA]':>9}{'IVrms%':>8}{'rout[M]':>9}{'Cp[fF]':>8}"
       f"{'gdd[nS]':>10}{'sign':>5}{'Prms':>6}{'In@1k[fA]':>11}{'PTAT':>7}")
    for m in rows:
        sign = m["gdd_sign"] if m["sign_ok"] else f"{m['gdd_sign']}!{m['gt_sign']}"
        pr(f"  {m['pin']:<14}{m['pol']:<7}{m['idc_ua']:>9.3f}{m['ivrms']:>8.2f}"
           f"{m['rout_M']:>9.1f}{m['cp_fF']:>8.1f}{m['gdd_nS']:>+10.3f}{sign:>5}"
           f"{m['prms']:>6.2f}{m['in1k_fA']:>11.1f}{m['ptat_g']:>7.3f}")
    pr("  (sign 'x!y' = model x vs GT y mismatch; PTAT = GT Idc(125C)/Idc(-40C))")
    dg = []
    for m in rows:
        dg += _diagnose(m)
    pr("  DIAGNOSIS:")
    if dg:
        for s in dg:
            pr("   - " + s)
    else:
        pr("   - all current-port analytic metrics within tolerance; behavioral form "
           "captures the device GT.")
    pr("   NOTE: emitted-netlist fidelity (VA/ngspice, incl. the dIpin/dVdd probe sign)")
    pr("         is NOT in this analytic block -- run cadence/isrc_spectre.py --sc.")
    pr("\n[8d] CURRENT GT DIGEST  (per port; machine-readable; digest_import rebuilds it)")
    pr("-" * 84)
    L += emit_digest_block(ref)
    return L


# --------------------------------------------------------------------------- parsing
_HDR = re.compile(r"#\s*iport\s+(\S+)\s+pol=(\w+)\s+vc=([-\d.eE+]+)\s+vdd=([-\d.eE+]+)")
_SUBS = {"# iv": "iv", "# idcT": "idcT", "# iy": "iy", "# ipsrr": "ipsrr",
         "# inoise": "inoise"}


def parse_current_digest(text):
    """Parse a pasted [8] CURRENT GT DIGEST into {pin: fit_isrc-ready dict}. Ignores any
    [7]/voltage content. rout/cp are reconstructed from the iy curve, as in port_view."""
    ports, pin, sub = {}, None, None
    for raw in text.splitlines():
        line = raw.strip()
        m = _HDR.search(line)
        if m:
            pin = m.group(1)
            ports[pin] = dict(pol=m.group(2), vc=float(m.group(3)), vdd=float(m.group(4)),
                              iv=[], idcT=[], iy=[], ipsrr=[], inoise=[])
            sub = None
            continue
        key = next((v for k, v in _SUBS.items() if line.startswith(k)), None)
        if key is not None:
            sub = key
            continue
        if pin is None or sub is None or not line or line.startswith("#"):
            continue
        toks = [t.strip() for t in line.split(",")]
        try:
            row = [float(t) for t in toks]
        except ValueError:
            sub = None                      # left the digest (e.g. footer '='*84)
            continue
        ports[pin][sub].append(row)
    return {pin: _assemble(d) for pin, d in ports.items() if d["iv"]}


def _assemble(d):
    iv = np.array(d["iv"], float)
    td = np.array(d["idcT"], float)
    yc = np.array(d["iy"], float)
    pc = np.array(d["ipsrr"], float)
    nz = np.array(d["inoise"], float)
    f = yc[:, 0]
    Y = yc[:, 1] + 1j * yc[:, 2]
    rout = 1.0 / abs(Y[0].real) if Y[0].real != 0 else float("inf")
    cp = abs(Y[-1].imag) / (2 * np.pi * f[-1]) if f[-1] > 0 else 0.0
    return dict(name="paste", pol=d["pol"], vc=d["vc"], vdd=d["vdd"], rout=rout, cp=cp,
                iv_v=iv[:, 0], iv_i=iv[:, 1], temps=td[:, 0], idcT=td[:, 1],
                ac_f=f, ac_y=Y, psrr_f=pc[:, 0], psrr_g=pc[:, 1] + 1j * pc[:, 2],
                nz_f=nz[:, 0], nz_in=nz[:, 1])
