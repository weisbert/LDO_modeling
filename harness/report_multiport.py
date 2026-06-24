"""Multi-port REPORT data layer -- the data a GUI "Report" tab consumes to draw per-port
GT-vs-model overlays and to print a copy-pasteable debug report.

This is the multi-port twin of report.py / current_digest.py: it does NOT re-fit. It takes
the structured result of fit_multiport.fit_multiport(npz, manifest) and REPRODUCES, port by
port, the EXACT same overlay arrays + scores the GUI Compare tab draws -- so the plotted
"model" curve IS the emitted model, not a fresh fit:

  voltage rail o : evaluate fit_model.zmodel / psrr_model / noise_model_sv on each load
                   corner's params P[il] (per-supply PSRR from P[il]["_psrr"][s]), under the
                   rail's own Cout/ESR module context -- byte-identical to fit_multiport's
                   own `err` block (fit_multiport._fit_voltage_output lines ~143-165).
  current sink   : assemble a fit_isrc-schema view from the IN-SITU npz keys (y_<c>_<load>,
                   pi_<c>_<s>_<load>, iv_<c>_<label> -- importmp.current_ports), then reuse
                   fit_isrc.fit_isrc + fit_isrc.predict_* + current_digest.diff_metrics
                   UNCHANGED (the SAME math as ModelerCore.current_compare, only the data
                   source differs). Ports come from manifest['i_out'] / result['current'],
                   NOT the air-gap digest registry (current_digest.list_iports), which a real
                   in-situ extraction never populates. Current-noise is not measured in-situ.

Two public functions:
  port_views(result, npz_path, manifest) -> list[dict]   (voltage rails first, then sinks)
  debug_report(result, npz_path, manifest) -> str        (one copy-pasteable text block)

Pure numpy, Qt-free, importable exactly like the other harness modules.
"""
import contextlib
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_model as FM            # the per-output transfer functions we reuse   # noqa: E402
import fit_multiport as FMP       # _fm_globals / FitResult export helpers        # noqa: E402


# --------------------------------------------------------------- modeling-quality grade
# Per-metric quality bars (good, marginal) -- under good = clearly usable, under marginal =
# usable with caveats, above = REVIEW. Grounded in score.py / report.py weights and the
# report._diagnose firing levels (~2-3 dB); real-LDO fits land ~0.1-1 dB. TUNABLE here -- the
# LDO designer owns the bar. dB unless noted.
GRADE_BARS = dict(
    v_zrms=(1.0, 3.0), v_prms=(1.0, 3.0), v_nrms=(1.5, 3.0),     # voltage rail, worst over corners
    c_ivrms=(2.0, 5.0),                                         # current I-V knee, % RMS
    # |Y| calibrated to the VALIDATED synthetic library's g0+sCp residual: a simple mirror fits
    # <3dB, but a cascode/Wilson reference's 2nd-order output zero leaves up to ~7dB (v6=7.16,
    # v8=6.28) -- an accepted model-scope limit, "usable with caveat", not "not ready". >8dB is a
    # genuine admittance defect. (Real WuR refs land 1.9-7.0dB = in-family, NOT a real-chip bug.)
    c_yrms=(3.0, 8.0), c_prms=(1.0, 3.0),                       # current |Y| / current-PSRR, dB
)
_VERDICT = {0: "USABLE", 1: "USABLE — minor caveats", 2: "REVIEW — not ready"}
_BADGE = {0: "OK", 1: "~", 2: "!!"}


def _level(val, bars):
    """0=good / 1=marginal / 2=poor for a lower-is-better metric; non-finite -> 0 (not measured,
    can't fault the model for a channel that has no in-situ GT)."""
    if val is None or not np.isfinite(val):
        return 0
    good, marg = bars
    return 0 if val <= good else (1 if val <= marg else 2)


def grade_port(view):
    """A clear modeling-quality verdict for ONE port, so the user knows at a glance whether the
    fit is usable. Returns {"level":0/1/2, "verdict":str, "badge":str, "metrics":[(name,val,unit,
    level)...], "reasons":[str]}. level = the worst metric; a current-PSRR SIGN FLIP forces REVIEW
    (a wrong-sign bias is unusable regardless of magnitude). Pure data off the view (no re-fit)."""
    items, reasons = [], []
    if view["kind"] == "voltage":
        w = view.get("worst", {})
        for name, key, bk in (("Zout", "zrms", "v_zrms"),
                              ("PSRR", "prms", "v_prms"),
                              ("noise", "nrms", "v_nrms")):
            val = float(w.get(key, np.nan))
            items.append((name, val, "dB", _level(val, GRADE_BARS[bk])))
    else:
        m = view.get("metrics", {})
        if not m.get("sign_ok", True):
            reasons.append("current-PSRR SIGN FLIP vs GT (bias pushed the wrong way)")
            items.append(("PSRR sign", float("nan"), "", 2))
        for name, key, bk, unit in (("I-V", "ivrms", "c_ivrms", "%"),
                                    ("|Y|", "yrms", "c_yrms", "dB"),
                                    ("cPSRR", "prms", "c_prms", "dB")):
            v = m.get(key, np.nan)
            val = float(v) if v is not None else float("nan")
            lv = _level(val, GRADE_BARS[bk])
            if key == "prms" and not m.get("cpsrr_observable", True):
                lv = 0                # supply->Iout coupling unobservable -> not a model defect
            items.append((name, val, unit, lv))
    level = max((lv for *_, lv in items), default=0)
    for name, val, unit, lv in items:
        if lv >= 1 and np.isfinite(val):
            reasons.append(f"{name} {'high' if lv == 2 else 'marginal'} ({val:.2f}{unit})")
    return dict(level=level, verdict=_VERDICT[level], badge=_BADGE[level],
                metrics=items, reasons=reasons)


def overall_grade(views):
    """The worst per-port verdict across all ports + which ports drove it. {"level","verdict",
    "badge","offenders":[pin...]}. The headline 'can I use this model?' answer."""
    graded = [(v, v.get("grade") or grade_port(v)) for v in views]
    level = max((g["level"] for _, g in graded), default=0)
    offenders = [v["pin"] for v, g in graded if g["level"] == level and level > 0]
    return dict(level=level, verdict=_VERDICT[level], badge=_BADGE[level], offenders=offenders)


# --------------------------------------------------------------- voltage-rail overlay
@contextlib.contextmanager
def _rail_context(cout, esr, nmode="norton"):
    """Set fit_model's module globals to one voltage rail's physical Cout/ESR (zmodel reads
    FM.C/FM.RC) + the rail's noise-block mode (norton | hybrid -- the multi-port fit's gated
    keep-best). This is the SAME state fit_multiport._fit_voltage_output established when it
    produced the rail's `err` block, so the model arrays we evaluate here equal the emitted model."""
    with FMP._fm_globals():
        FM.C, FM.RC = float(cout), float(esr)
        FM.CFT = 0.0
        FM.NOISE_MODE = nmode
        yield


def _voltage_corner(P_il, nfk, sp, il, supplies, prim, err, nmode="norton", nfkv=None):
    """Reproduce ONE load corner's GT + model overlay arrays + scores, exactly as the GUI
    Compare tab (gt_corner/predict_corner) and fit_multiport's per-corner err do.

    sp = the rail's single-port npz dict (z_<il>/p_<il>/noise_<il> + per-supply p arrays via
    supplies). `P_il` carries the fitted params incl. _psrr={s:(G,Q)}; `err` is the matching
    fit_multiport err row {zrms, psrr:{s:(rms_db,phase_deg)}, nrms} (scores reused verbatim --
    no re-scoring, so the table here == fit_multiport.report's table)."""
    zf = (P_il["R_a"], P_il["L_a"], P_il["R_pl"], P_il["R_b"], P_il["L_b"])
    gz = sp[f"z_{il}"]
    fz, Zg = gz[:, 0], gz[:, 1] + 1j * gz[:, 2]
    Zm = FM.zmodel(fz, *zf)

    # per-supply PSRR (primary supply drives the headline Hg/Hm panels)
    psrr = {}
    for s in supplies:
        gp = supplies[s][il]
        fp, Hg = gp[:, 0], gp[:, 1] + 1j * gp[:, 2]
        G, Q = P_il["_psrr"][s]
        Hm = FM.psrr_model(fp, *zf, G, Q)
        psrr[s] = dict(fp=fp, Hg=Hg, Hm=Hm)
    # headline panel = primary supply (matches the single-PSRR slot the GUI/fit_model use)
    head = psrr[prim]

    gn = sp[f"noise_{il}"]
    fn, Sg = gn[:, 0], gn[:, 1]
    Sm = FM.noise_model_sv(P_il, fn, FM.zmodel(fn, *zf), nfk=nfk, nfkv=nfkv, nmode=nmode)

    # scores: REUSE the fit_multiport err row (already model-vs-GT on these same arrays)
    scores = dict(zrms=float(err["zrms"]),
                  psrr={s: tuple(err["psrr"][s]) for s in err.get("psrr", {})},
                  nrms=float(err["nrms"]))
    return dict(
        il=il, fz=fz, Zg=Zg, Zm=Zm,
        fp=head["fp"], Hg=head["Hg"], Hm=head["Hm"],
        psrr_supplies={s: dict(Hg=psrr[s]["Hg"], Hm=psrr[s]["Hm"]) for s in psrr},
        fn=fn, Sg=Sg, Sm=Sm, scores=scores)


def _voltage_view(o, fit, sp, supplies, prim):
    """Assemble one voltage rail's view dict (see module docstring / contract)."""
    P, nfk = fit["P"], fit["nfk"]
    nmode, nfkv = fit.get("nmode", "norton"), fit.get("nfkv", [])
    loads = [e["il"] for e in fit["err"]]            # the actual fit corners, in fit order
    errmap = {e["il"]: e for e in fit["err"]}
    corners = {}
    with _rail_context(fit["cout"], fit["esr"], nmode):
        for il in loads:
            corners[il] = _voltage_corner(P[il], nfk, sp, il, supplies, prim, errmap[il],
                                          nmode, nfkv)
    # worst-case rollup over this rail (PSRR worst over every supply)
    zr = [c["scores"]["zrms"] for c in corners.values()]
    pr = [v[0] for c in corners.values() for v in c["scores"]["psrr"].values()]
    nr = [c["scores"]["nrms"] for c in corners.values()]
    return dict(kind="voltage", name=o, pin=fit.get("pin", o),
                loads=loads, cout=float(fit["cout"]), esr=float(fit["esr"]),
                supplies=list(supplies), corners=corners,
                worst=dict(zrms=max(zr, default=0.0), prms=max(pr, default=0.0),
                           nrms=max(nr, default=0.0)))


# --------------------------------------------------------------- current-sink overlay
def _insitu_current_view(ref, c, cp, row, manifest, tnom_c):
    """One IN-SITU current sink's overlay+score view, built from the multi-port npz keys
    (y_<c>_<load>, pi_<c>_<s>_<load>, iv_<c>_<label>) -- the SAME data fit_multiport
    ._fit_current_ports fits and emit consumes. The in-situ twin of ModelerCore
    .current_compare: it ASSEMBLES a fit_isrc-schema `view` from the in-situ arrays, then
    reuses fit_isrc.fit_isrc + predict_* + current_digest.diff_metrics UNCHANGED (identical
    downstream math). The air-gap digest registry (current_digest.list_iports) is NOT used --
    a real extraction never populates it; the current ports come from manifest['i_out'].

    Current-NOISE is a panel ONLY when coverage.inoise measured noise_i_<c>_<load> (A/rtHz);
    absent otherwise (the honest stub -> nrms NaN, kept OUT of the grade). I-V / Idc(T) are
    panels only when their coverage measurements ran (iv_<c>_<label> present, >=2 temps).
    `present` names the panels that have REAL in-situ GT so the GUI draws exactly those.

    `cp` = importmp.current_ports(ref, manifest)[c] = {loads, y:{il:arr}, pi:{(s,il):arr}}.
    `row` = the matching result['current'] row (for the designer pin); may be None."""
    import current_digest as CD
    import fit_isrc as ISR
    m = manifest
    meta = (m.get("i_out") or {}).get(c, {})
    pol = str(meta.get("pol", "sink"))
    sink_dc = float(meta.get("dc", 0.0))                 # the bias compliance / OP voltage
    pin = str((row or {}).get("pin", meta.get("pin", c)))
    loads = list(cp.get("loads", []))
    # OP load: the large-signal row's label if present, else the middle load.
    op_load = row.get("il") if (row and row.get("il") in loads) else (
        loads[len(loads) // 2] if loads else None)

    present, notes = set(), []

    # ---- I-V (+ Idc(T)) from iv_<c>_<label> ----
    ivmap = FMP._iv_for_sink(ref, c)
    if ivmap:
        labels = sorted(ivmap)
        tmap = {lbl: FMP._temp_of_label(ref, lbl) for lbl in labels}
        op_lbl = min(labels, key=lambda l: abs(tmap[l] - tnom_c) if np.isfinite(tmap[l]) else 1e30)
        a = ivmap[op_lbl]; order = np.argsort(a[:, 0])
        iv_v, iv_i = a[order, 0], a[order, 1]
        present.add("iv")
        Tlist, idcT_l = [], []
        for lbl in labels:
            t = tmap[lbl]
            if not np.isfinite(t):
                continue
            b = ivmap[lbl]; o2 = np.argsort(b[:, 0])
            Tlist.append(t); idcT_l.append(float(np.interp(sink_dc, b[o2, 0], b[o2, 1])))
        if len(Tlist) >= 2:
            tt = np.argsort(Tlist)
            temps = np.asarray(Tlist)[tt]; idcT = np.asarray(idcT_l)[tt]
            present.add("idcT")
        else:
            temps = np.array([tnom_c]); idcT = np.array([float(np.interp(sink_dc, iv_v, iv_i))])
    else:                                                # no I-V coverage -> placeholder, no panel
        iv_v = np.linspace(0.0, ISR.VDD0, 16); iv_i = np.zeros_like(iv_v)
        temps = np.array([tnom_c]); idcT = np.array([0.0])
        notes.append("I-V compliance: not swept in-situ (no iv_<port> coverage)")

    # ---- admittance |Y| from y_<c>_<load> ----
    if cp.get("y"):
        il_y = op_load if op_load in cp["y"] else next(iter(cp["y"]))
        g = cp["y"][il_y]; ac_f = g[:, 0]; ac_y = g[:, 1] + 1j * g[:, 2]
        present.add("y")
    else:
        ac_f = np.logspace(0, 8, 32); ac_y = np.full(ac_f.shape, 1e-9 + 0j)
        notes.append("output admittance: no y_<port> measured")

    # ---- current-PSRR from pi_<c>_<s>_<load>, per supply ----
    psrr_raw = {}
    for (s, il2), arr in cp.get("pi", {}).items():
        if s not in psrr_raw or il2 == op_load:          # prefer the OP-load array per supply
            psrr_raw[s] = arr
    if psrr_raw:
        order_s = [s for s in (m.get("current_psrr_supplies") or []) if s in psrr_raw] \
            + [s for s in psrr_raw if s not in (m.get("current_psrr_supplies") or [])]
        prim_s = order_s[0]
        # current-PSRR transfer is dI/dVdd = -pi (importmp stores pi = -I/Vsup); negate so the
        # report's gdd/sign equals what emit ships (_fit_current_largesignal fits gdd on -PI) and
        # the digest-path convention (current_digest psrr_g = signed dI/dVdd).
        gp = psrr_raw[prim_s]; psrr_f = gp[:, 0]; psrr_g = -(gp[:, 1] + 1j * gp[:, 2])
        present.add("psrr")
    else:
        psrr_f = ac_f.copy(); psrr_g = np.zeros(psrr_f.shape, complex)
        notes.append("current-PSRR: no pi_<port> measured")

    # current-noise: measured ONLY when coverage.inoise ran (noise_i_<c>_<load>, A/rtHz). When
    # present -> a real 'noise' panel + a graded-for-DISPLAY nrms (kept OUT of the pass/fail grade
    # composite until cross-validated -- grade_port does not read nrms). Absent -> placeholder +
    # nulled nrms (the honest stub), exactly as before.
    nik = FMP._noise_i_for_sink(ref, c)
    if nik:
        nlbl = op_load if op_load in nik else next(iter(nik))
        na = nik[nlbl]; nz_f = na[:, 0]; nz_in = na[:, 1]
        present.add("noise")
    else:
        notes.append("current-noise: not measured in-situ (enable coverage.inoise to measure)")
        nz_f = ac_f.copy(); nz_in = np.full(nz_f.shape, 1e-15)

    rout = 1.0 / abs(ac_y[0].real) if ac_y[0].real != 0 else float("inf")
    cpar = abs(ac_y[-1].imag) / (2 * np.pi * ac_f[-1]) if ac_f[-1] > 0 else 0.0
    view = dict(name=c, pol=pol, vc=sink_dc, vdd=ISR.VDD0,
                iv_v=iv_v, iv_i=iv_i, ac_f=ac_f, ac_y=ac_y, rout=rout, cp=cpar,
                psrr_f=psrr_f, psrr_g=psrr_g, nz_f=nz_f, nz_in=nz_in, temps=temps, idcT=idcT)
    p = ISR.fit_isrc(view)
    models = dict(iv=ISR.predict_iv(p, iv_v), y=ISR.predict_y(p, ac_f),
                  psrr=ISR.predict_psrr(p, psrr_f), noise=ISR.predict_noise(p, nz_f),
                  idcT=ISR.predict_idcT(p, temps))
    metrics = CD.diff_metrics(view, p)
    # null channels with no in-situ GT so the diagnosis never fires on placeholder data
    if "noise" not in present:
        metrics["nrms"] = float("nan")          # no current-noise GT -> not reported/diagnosed
    if "iv" not in present:
        metrics["ivrms"] = float("nan")
    if "idcT" not in present:
        metrics["ptat_g"] = metrics["ptat_m"] = float("nan")
    if "psrr" not in present:
        metrics["prms"] = float("nan"); metrics["sign_ok"] = True

    # per-supply current-PSRR panels (model c0+jw.c1 per supply, == the fit_multiport score)
    psrr_panels = {}
    for s, arr in psrr_raw.items():
        f = arr[:, 0]; g = -(arr[:, 1] + 1j * arr[:, 2])     # dI/dVdd = -pi (matches emit + metrics)
        c0, c1, prms = FMP._fit_cpsrr(f, g)
        psrr_panels[s] = dict(f=f, Gg=g, Gm=c0 + 1j * 2 * np.pi * f * c1, rms_db=prms)

    return dict(kind="current", name=c, pin=pin, pol=pol, op_load=op_load,
                view=view, params=p, models=models, metrics=metrics,
                present=sorted(present), psrr_supplies=psrr_panels, notes=notes)


# --------------------------------------------------------------------- public: views
def port_views(result, npz_path, manifest):
    """Per-port overlay+score data, voltage rails first then current sinks.

    Voltage rail dict:
      {"kind":"voltage","name":o,"pin":pin,"loads":[il...],"cout":..,"esr":..,
       "supplies":[s...],
       "corners":{ il: {"fz","Zg","Zm","fp","Hg","Hm",
                        "psrr_supplies":{s:{"Hg","Hm"}}, "fn","Sg","Sm",
                        "scores":{"zrms","psrr":{s:(rms_db,phase_deg)},"nrms"}}},
       "worst":{"zrms":max,"prms":max,"nrms":max}}
      (Hg/Hm on the corner = primary supply, for the headline panels; Zg/Hg/Sg = GT,
       Zm/Hm/Sm = model. All arrays are numpy; Zm/Hm/Sm are finite.)

    Current sink dict (IN-SITU; one per manifest i_out):
      {"kind":"current","name":sink,"pin":pin,"pol":"sink"/"source","op_load":il,
       "view":<fit_isrc-schema dict assembled from the npz y_/pi_/iv_ keys>,
       "params":<fit_isrc params>, "models":{"iv","y","psrr","noise","idcT"},
       "metrics":<current_digest.diff_metrics dict; channels with no in-situ GT are NaN>,
       "present":[panels with REAL in-situ GT, subset of iv/y/psrr/idcT/noise],
       "psrr_supplies":{ s:{"f","Gg","Gm","rms_db"} },   # per-supply current-PSRR panels
       "notes":[absent-channel notes incl. "current-noise: not measured in-situ"]}
    The GUI draws exactly the panels named in `present` (+ one current-PSRR sub-panel per
    psrr_supplies entry); current-noise is drawn only when coverage.inoise measured it.
    """
    from insitu import importmp as IM
    ref = IM.load_multiport(npz_path)
    m = manifest
    views = IM.split_ports(ref, m)
    sups = list(m["supplies"])
    prim = sups[0] if sups else None

    out = []
    # voltage rails, in manifest v_out order (the result was built in that order)
    for o, fit in result["voltage"].items():
        view = views[o]
        out.append(_voltage_view(o, fit, view["npz"], view["supplies"], prim))
    # current sinks, IN-SITU: one per manifest i_out, built from the npz y_/pi_/iv_ keys via
    # importmp.current_ports (NOT the air-gap digest registry, which a real run never writes).
    cports = IM.current_ports(ref, m)
    rowmap = {}
    for r in result.get("current", []):
        rowmap.setdefault(r.get("sink"), r)              # first (large-signal) row per sink
    tnom_c = float(result.get("meta", {}).get("tnom_c", 55.0))
    for c in (m.get("i_out") or {}):
        if c in cports:
            out.append(_insitu_current_view(ref, c, cports[c], rowmap.get(c), m, tnom_c))
    for v in out:                                          # attach the usable/not modeling grade
        v["grade"] = grade_port(v)
    return out


# ============================================================ air-gap GT digest
# The multi-port twin of report.py's [7] / current_digest's [8d]: serialize the RAW ref GT
# arrays (log-resampled) into the report TEXT so a paste rebuilds an npz-equivalent ref and
# fit_multiport reproduces the WHOLE report locally -- no npz crosses the air gap. We emit the
# raw ref keys (not the port_views-derived arrays) so every downstream derivation/sign runs
# identically on rebuild -- the parser restores byte-for-byte the keys fit_multiport reads.
# Manifest-DRIVEN: a load label carries underscores (e.g. 'tt_25c'), so a raw key like
# 'z_pll_tt_25c' cannot be split unambiguously; instead the rail/supply/sink names + the loads
# array (all from the manifest) NAME each key, and the parser reconstructs it from `@`-tokens.
_MPD_BEGIN = "[MPD1] MULTIPORT GT DIGEST"
_MPD_END = "[MPD1-END]"


def _z_resample(f, Z, ppd=6):
    """log-resample a complex Zout, DENSIFYING around the |Z| resonance peak -- 5/dec would
    step over a sharp on-chip peak and the digest is all the modeler gets across the gap (same
    guard as report.py's [7])."""
    import current_digest as CD
    f = np.asarray(f, float)
    g, Zg = CD._logresample_complex(f, Z, ppd=ppd)
    zmag = np.abs(Z)
    ipk = int(np.argmax(zmag))
    if 0 < ipk < len(f) - 1 and zmag[ipk] > 2.0 * float(np.median(zmag)):
        fpk = f[ipk]
        extra = np.logspace(np.log10(max(fpk / 2.5, f[0])),
                            np.log10(min(fpk * 2.5, f[-1])), 16)
        # carry the ACTUAL sampled peak + its immediate neighbors VERBATIM (np.interp returns a
        # node's value exactly): a log-grid step that straddles a high-Q peak would undershoot
        # its magnitude by several dB, and a real on-chip pll/vco rail IS a high-Q peak -- the
        # one feature the Zout fit is judged on. The neighbors keep the peak's curvature.
        nbr = f[max(ipk - 2, 0):min(ipk + 3, len(f))]
        g = np.unique(np.concatenate([g, extra, nbr]))
        Zg = (np.interp(np.log(g), np.log(f), Z.real)
              + 1j * np.interp(np.log(g), np.log(f), Z.imag))
    return g, Zg


def _emit_c(pr, header, f, Z):
    pr(header + "   # f[Hz], re, im")
    for a, b in zip(f, Z):
        pr(f"  {a:.5e}, {b.real:.6e}, {b.imag:.6e}")


def _emit_r(pr, header, f, y):
    pr(header + "   # f[Hz], val")
    for a, b in zip(f, y):
        pr(f"  {a:.5e}, {b:.6e}")


def emit_multiport_digest(ref, manifest, views=None):
    """Serialize ref's GT arrays (log-resampled) -> the machine-readable [MPD1] digest lines
    that parse_multiport_digest rebuilds into an npz-equivalent ref. Manifest-driven; emits a
    block only for the keys actually present (a coverage-light run simply emits fewer).

    When `views` (port_views output) is given, ALSO carry the VOLTAGE rails' fitted MODEL curves
    (@zmodel/@psrrmodel/@noisemodel) at the same resampled freqs. Why: a near-capless silicon rail
    (resistive |Z| plateau, no output-cap signature) has an ill-conditioned Zout fit that a lossy
    log-resampled subsample does NOT refit to the same answer -- so a paste-only `digest_to_npz ->
    fit_multiport` reproduces the report's CURRENT ports exactly but mis-fits the VOLTAGE Zout
    (envelope-cap fallback -> wrong pole). Carrying the box's model makes the voltage GT-vs-model
    overlay faithful without trusting the refit. Current ports DO refit faithfully -> not carried."""
    import current_digest as CD
    m = manifest
    loads = [str(x) for x in ref["loads"]]
    L = []
    pr = L.append
    # voltage MODEL curves carried verbatim (keyed (kind,o[,s],il) -> (src_f, model_array))
    vm = {}
    for v in (views or []):
        if v.get("kind") != "voltage":
            continue
        for il, c in v["corners"].items():
            vm[("z", v["name"], il)] = (np.asarray(c["fz"], float), np.asarray(c["Zm"]))
            vm[("noise", v["name"], il)] = (np.asarray(c["fn"], float), np.asarray(c["Sm"]))
            for s, sp in c.get("psrr_supplies", {}).items():
                vm[("psrr", v["name"], s, il)] = (np.asarray(c["fp"], float), np.asarray(sp["Hm"]))

    def _on(fr, src_f, arr):
        """the carried model, resampled onto the SAME fr as its GT block (model is smooth)."""
        lf, ls = np.log(fr), np.log(np.asarray(src_f, float))
        if np.iscomplexobj(arr):
            return np.interp(lf, ls, arr.real) + 1j * np.interp(lf, ls, arr.imag)
        return np.interp(lf, ls, np.asarray(arr, float))
    pr(_MPD_BEGIN + "  (log-resampled ground truth; rebuilds an npz-equivalent ref so")
    pr("       fit_multiport reproduces this report from the PASTE alone -- no npz needed)")
    pr("-" * 78)
    # ---- meta: loads + per-label rail current / temp / Cout / ESR (verbatim; authoritative) --
    pr("@meta loads = " + " | ".join(loads))
    for o in m["v_out"]:
        k = f"meta_iload_{o}"
        if k in ref:
            pr(f"@meta {k} = " + " | ".join(f"{v:.6e}" for v in np.asarray(ref[k], float).ravel()))
    for k in ("meta_temp", "meta_cout", "meta_esr"):
        if k in ref:
            pr(f"@meta {k} = " + " | ".join(f"{v:.6g}" for v in np.asarray(ref[k], float).ravel()))
    # ---- voltage rails: z (peak-densified) / psrr per supply / noise, per corner ----
    for o in m["v_out"]:
        for il in loads:
            k = f"z_{o}_{il}"
            if k in ref:
                g = np.asarray(ref[k], float)
                fr, Zr = _z_resample(g[:, 0], g[:, 1] + 1j * g[:, 2])
                _emit_c(pr, f"@z {o} {il}", fr, Zr)
                if ("z", o, il) in vm:
                    _emit_c(pr, f"@zmodel {o} {il}", fr, _on(fr, *vm[("z", o, il)]))
        for s in m["supplies"]:
            for il in loads:
                k = f"p_{o}_{s}_{il}"
                if k in ref:
                    g = np.asarray(ref[k], float)
                    fr, Hr = CD._logresample_complex(g[:, 0], g[:, 1] + 1j * g[:, 2])
                    _emit_c(pr, f"@psrr {o} {s} {il}", fr, Hr)
                    if ("psrr", o, s, il) in vm:
                        _emit_c(pr, f"@psrrmodel {o} {s} {il}", fr, _on(fr, *vm[("psrr", o, s, il)]))
        for il in loads:
            k = f"noise_{o}_{il}"
            if k in ref:
                g = np.asarray(ref[k], float)
                fr, Sr = CD._logresample_real(g[:, 0], g[:, 1])
                _emit_r(pr, f"@noise {o} {il}", fr, Sr)
                if ("noise", o, il) in vm:
                    _emit_r(pr, f"@noisemodel {o} {il}", fr, _on(fr, *vm[("noise", o, il)]).real)
    # ---- current sinks: y / pi per supply, per corner + I-V sweeps (kept whole) ----
    for c in m["i_out"]:
        for il in loads:
            k = f"y_{c}_{il}"
            if k in ref:
                g = np.asarray(ref[k], float)
                fr, Yr = CD._logresample_complex(g[:, 0], g[:, 1] + 1j * g[:, 2])
                _emit_c(pr, f"@y {c} {il}", fr, Yr)
        for s in m["current_psrr_supplies"]:
            for il in loads:
                k = f"pi_{c}_{s}_{il}"
                if k in ref:
                    g = np.asarray(ref[k], float)
                    fr, Pr = CD._logresample_complex(g[:, 0], g[:, 1] + 1j * g[:, 2])
                    _emit_c(pr, f"@pi {c} {s} {il}", fr, Pr)
        for k in sorted(x for x in ref if str(x).startswith(f"iv_{c}_")):
            g = np.asarray(ref[k], float)
            Vo, I = CD._iv_subsample(g[:, 0], g[:, 1])
            pr(f"@iv {c} {k[len(f'iv_{c}_'):]}   # Vo[V], I[A]")
            for a, b in zip(Vo, I):
                pr(f"  {a:.6e}, {b:.6e}")
    pr(_MPD_END)
    return L


def parse_multiport_digest(text):
    """Rebuild an npz-equivalent ref dict from a pasted debug report's [MPD1] block (the inverse
    of emit_multiport_digest). Returns {key: np.ndarray} -- the exact keys fit_multiport reads
    (z_<o>_<il>, p_<o>_<s>_<il>, noise_<o>_<il>, y_<c>_<il>, pi_<c>_<s>_<il>, iv_<c>_<label>,
    loads, meta_*). Ignores everything outside the block."""
    ref = {}
    inblock = False
    kind = toks = None
    rows = []

    def _flush():
        nonlocal kind, toks, rows
        if kind is not None and rows:
            arr = np.array(rows, float)
            if kind == "z" and len(toks) >= 2:
                ref[f"z_{toks[0]}_{toks[1]}"] = arr
            elif kind == "psrr" and len(toks) >= 3:
                ref[f"p_{toks[0]}_{toks[1]}_{toks[2]}"] = arr
            elif kind == "noise" and len(toks) >= 2:
                ref[f"noise_{toks[0]}_{toks[1]}"] = arr
            elif kind == "y" and len(toks) >= 2:
                ref[f"y_{toks[0]}_{toks[1]}"] = arr
            elif kind == "pi" and len(toks) >= 3:
                ref[f"pi_{toks[0]}_{toks[1]}_{toks[2]}"] = arr
            elif kind == "iv" and len(toks) >= 2:
                ref[f"iv_{toks[0]}_{toks[1]}"] = arr
            # carried VOLTAGE MODEL curves -> m_* keys (fit_multiport never reads these; the
            # verification path uses them so the voltage GT-vs-model overlay is the BOX's actual
            # model, not a refit of the lossy digest). zmodel->m_z, psrrmodel->m_p, noisemodel->m_noise.
            elif kind == "zmodel" and len(toks) >= 2:
                ref[f"m_z_{toks[0]}_{toks[1]}"] = arr
            elif kind == "psrrmodel" and len(toks) >= 3:
                ref[f"m_p_{toks[0]}_{toks[1]}_{toks[2]}"] = arr
            elif kind == "noisemodel" and len(toks) >= 2:
                ref[f"m_noise_{toks[0]}_{toks[1]}"] = arr
        kind, toks, rows = None, None, []

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(_MPD_BEGIN):
            inblock = True
            continue
        if line.startswith(_MPD_END):
            _flush()
            inblock = False
            continue
        if not inblock:
            continue
        if line.startswith("@meta"):
            _flush()
            key, _, vals = line[len("@meta"):].strip().partition("=")
            key = key.strip()
            parts = [p.strip() for p in vals.split("|")] if vals.strip() else []
            if key == "loads":
                ref["loads"] = np.array([str(p) for p in parts])
            elif len(parts) == 1:
                ref[key] = np.array(float(parts[0]))          # scalar meta (Cout/ESR)
            elif parts:
                ref[key] = np.array([float(p) for p in parts], float)
            continue
        if line.startswith("@"):
            _flush()
            hdr = line[1:].split("#")[0].split()
            kind, toks, rows = (hdr[0] if hdr else None), hdr[1:], []
            continue
        if kind is not None and line and not line.startswith("#"):
            try:
                rows.append([float(t) for t in line.split(",")])
            except ValueError:                                # left the data block
                _flush()
    _flush()
    return ref


def parse_manifest(text):
    """Extract the inlined manifest JSON from a pasted debug report -> dict (the @meta-free
    companion to parse_multiport_digest, so a paste reproduces with NO attachments)."""
    import json
    i = text.find("manifest JSON (inlined")
    start = text.find("{", i if i >= 0 else 0)
    if start < 0:
        raise ValueError("no inlined manifest JSON found in the pasted report")
    depth = 0
    for j in range(start, len(text)):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:j + 1])
    raise ValueError("unterminated manifest JSON block in the pasted report")


def digest_to_npz(text, path):
    """Parse a pasted report's [MPD1] GT digest and write it as an npz that fit_multiport /
    debug_report consume UNCHANGED (fit_multiport takes a path; this rebuilds one). Returns the
    path as a str."""
    ref = parse_multiport_digest(text)
    if "loads" not in ref:
        raise ValueError("no [MPD1] MULTIPORT GT DIGEST block found in the pasted text")
    np.savez(path, **ref)
    return str(path)


def voltage_views_from_digest(text, manifest):
    """FAITHFUL voltage GT-vs-MODEL views from a pasted [MPD1] digest WITHOUT refitting -- uses the
    carried @zmodel/@psrrmodel/@noisemodel (the BOX's actual model). A refit of the lossy digest
    mis-fits near-capless Zout (see emit_multiport_digest), so this is the honest off-box voltage
    check. Returns {rail: {il: {fz,Zg,Zm, supplies:{s:{fp,Hg,Hm,rms_db}}, fn,Sg,Sm,
    scores:{zrms,prms,nrms}}}}; EMPTY if the report carried no model (pre-model-carry digest)."""
    ref = parse_multiport_digest(text)
    loads = [str(x) for x in ref.get("loads", [])]

    def _logrms(num, den):
        num, den = np.abs(num) + 1e-30, np.abs(den) + 1e-30
        return float(np.sqrt(np.mean((20 * np.log10(num / den)) ** 2)))

    out = {}
    for o in manifest.get("v_out", {}):
        rail = {}
        for il in loads:
            kz, kzm = f"z_{o}_{il}", f"m_z_{o}_{il}"
            if kz not in ref or kzm not in ref:
                continue
            gz, mz = ref[kz], ref[kzm]
            Zg, Zm = gz[:, 1] + 1j * gz[:, 2], mz[:, 1] + 1j * mz[:, 2]
            sel = gz[:, 0] >= 1e3                          # score band (== score.py zrms)
            zrms = _logrms(Zm[sel], Zg[sel]) if sel.any() else float("nan")
            sup, prms = {}, []
            for s in manifest.get("supplies", {}):
                kp, kpm = f"p_{o}_{s}_{il}", f"m_p_{o}_{s}_{il}"
                if kp in ref and kpm in ref:
                    gp, mp = ref[kp], ref[kpm]
                    Hg, Hm = gp[:, 1] + 1j * gp[:, 2], mp[:, 1] + 1j * mp[:, 2]
                    r = _logrms(Hm, Hg); prms.append(r)
                    sup[s] = dict(fp=gp[:, 0], Hg=Hg, Hm=Hm, rms_db=r)
            fn = Sg = Sm = None
            nrms = float("nan")
            kn, knm = f"noise_{o}_{il}", f"m_noise_{o}_{il}"
            if kn in ref and knm in ref:
                gn, mn = ref[kn], ref[knm]
                fn, Sg, Sm = gn[:, 0], gn[:, 1], mn[:, 1]
                nrms = _logrms(Sm, Sg)
            rail[il] = dict(fz=gz[:, 0], Zg=Zg, Zm=Zm, supplies=sup, fn=fn, Sg=Sg, Sm=Sm,
                            scores=dict(zrms=zrms, prms=(max(prms) if prms else float("nan")),
                                        nrms=nrms))
        if rail:
            out[o] = rail
    return out


# --------------------------------------------------------------- voltage diagnosis
def _rail_diagnosis(result, npz_path, manifest, o):
    """Plain-language findings for one voltage rail, REUSING report._diagnose. Computed IN
    MEMORY from the MULTI-PORT fit's OWN params (result['voltage'][o]['P']/['nfk']) through
    report._corner -- NO re-fit and NO disk write (report._diagnose only needs result.cout +
    the per-corner analysis), so the diagnosis matches the emitted model exactly. Returns
    (lines, note); on any failure returns ([], note) -- a diagnosis must never kill the report."""
    import types
    import report as RPT
    from insitu import importmp as IM
    try:
        fit = result["voltage"][o]
        P, nfk = fit["P"], fit["nfk"]
        loads = [e["il"] for e in fit["err"]]
        if not loads:
            return [], None
        ref = IM.load_multiport(npz_path)
        sp = IM.split_ports(ref, manifest)[o]["npz"]      # in-memory z_/p_/noise_ single-port view
        nom = loads[len(loads) // 2]
        # multi-port noise is fit Norton @vout (or the gated 'hybrid' series-voltage bank) with the
        # per-corner bank baked into P[il]; predict with the rail's own nmode/nfkv (no module trap).
        nmode, nfkv = fit.get("nmode", "norton"), (fit.get("nfkv") or None)
        cs = [RPT._corner(sp, P[il], nfk, il, nmode=nmode, nfkv=nfkv) for il in loads]
        cnom = next(c for c in cs if c["il"] == nom)
        agg = dict(zrms=np.mean([c["zrms"] for c in cs]),
                   zband=np.mean([c["zband"] for c in cs]),
                   zphase=np.mean([c["zphase"] for c in cs]),
                   pkdb=np.mean([abs(c["pkdb"]) for c in cs]),
                   pband=np.mean([c["pband"] for c in cs]),
                   pphase=np.mean([c["pphase"] for c in cs]),
                   noise=np.mean([c["npsd"] for c in cs]))
        terms = sorted(((k, RPT.W[k] * agg[k], agg[k], RPT.W[k]) for k in agg),
                       key=lambda x: -x[1])
        shim = types.SimpleNamespace(cout=float(fit["cout"]))   # _diagnose reads only result.cout
        dg = RPT._diagnose(cnom, cs, shim, sp, terms)
        return dg, None
    except Exception as e:                            # noqa: BLE001 -- never kill the report
        return [], f"diagnosis unavailable ({type(e).__name__}: {e})"


# --------------------------------------------------------------------- public: report
def debug_report(result, npz_path, manifest):
    """A single copy-pasteable text debug report for the whole multi-port model.

    Header (port roster + fit-param digest) -> per voltage rail (scores table + per-rail
    diagnosis) -> per current sink (scores + current_digest._diagnose) -> worst-case rollup
    (voltage vs current kept separate) -> TO REPRODUCE footer (the 3-line python to
    regenerate this report locally). Never raises on a degenerate/bad fit."""
    import current_digest as CD
    import fit_isrc

    meta = result.get("meta", {})
    stem = pathlib.Path(npz_path).stem
    mname = (manifest.get("name") if isinstance(manifest, dict) else None) or "(manifest)"
    sups = list(meta.get("supplies", manifest.get("supplies", []) if isinstance(manifest, dict) else []))
    loads = list(meta.get("loads", []))

    views = port_views(result, npz_path, manifest)
    vviews = [v for v in views if v["kind"] == "voltage"]
    cviews = [v for v in views if v["kind"] == "current"]

    L = []
    pr = L.append
    pr("=== PMU MULTI-PORT MODEL DEBUG REPORT ===")
    pr(f"npz       : {stem}")
    pr(f"manifest  : {mname}")
    pr(f"loads     : {loads}")
    pr(f"supplies  : {sups}")
    pr("ports     : (in overlay order; pin . kind)")
    for v in views:
        g = v.get("grade") or grade_port(v)
        pr(f"  - {v['pin']:<16} . {v['kind']:<7}  [{g['badge']:>2}] {g['verdict']}"
           + (f"  ({v['pol']})" if v["kind"] == "current" else f"  (rail '{v['name']}')"))
    pr("")
    og = overall_grade(views)
    pr(f"OVERALL MODELING GRADE : [{og['badge']}] {og['verdict']}"
       + (f"  -- driven by: {', '.join(og['offenders'])}" if og["offenders"] else ""))
    pr(f"  (bars: voltage Zout/PSRR<{GRADE_BARS['v_zrms'][0]:.0f}dB good /<"
       f"{GRADE_BARS['v_zrms'][1]:.0f} marginal, noise<{GRADE_BARS['v_nrms'][0]:.1f}; current "
       f"I-V<{GRADE_BARS['c_ivrms'][0]:.0f}% , |Y|/PSRR<{GRADE_BARS['c_yrms'][0]:.0f}dB; sign flip=REVIEW)")
    pr("")
    pr("fit-param digest:")
    for v in vviews:
        pr(f"  rail '{v['name']}' (pin {v['pin']}): Cout={v['cout']*1e12:.1f}pF "
           f"ESR={v['esr']:.3f}ohm  corners={v['loads']}")
    for v in cviews:
        mt = v["metrics"]
        pr(f"  sink '{v['name']}' (pin {v['pin']}): pol={v['pol']} "
           f"idc={mt['idc_ua']:.3f}uA rout={mt['rout_M']:.1f}Mohm Cp={mt['cp_fF']:.1f}fF "
           f"panels={','.join(v['present']) or '(none)'}")
    if not vviews and not cviews:
        pr("  (no ports)")

    # ---- per voltage rail ----
    for v in vviews:
        pr("")
        pr("-" * 78)
        pr(f"VOLTAGE RAIL '{v['name']}'   pin {v['pin']}   "
           f"Cout {v['cout']*1e12:.1f}pF / ESR {v['esr']:.3f}ohm")
        pr("-" * 78)
        g = v["grade"]
        pr(f"  GRADE: [{g['badge']}] {g['verdict']}"
           + (f"  ({'; '.join(g['reasons'])})" if g["reasons"] else ""))
        hdr = f"  {'load':>8} | {'Zrms[dB]':>9}"
        for s in v["supplies"]:
            hdr += f" | {('P_'+s+'[dB]'):>12} {(s+'[deg]'):>10}"
        hdr += f" | {'Nrms[dB]':>9}"
        pr(hdr)
        for il in v["loads"]:
            sc = v["corners"][il]["scores"]
            line = f"  {il:>8} | {sc['zrms']:>9.3f}"
            for s in v["supplies"]:
                rms_db, ph = sc["psrr"].get(s, (float("nan"), float("nan")))
                line += f" | {rms_db:>12.3f} {ph:>10.2f}"
            line += f" | {sc['nrms']:>9.3f}"
            pr(line)
        pr(f"  worst: Zout {v['worst']['zrms']:.2f}dB  PSRR {v['worst']['prms']:.2f}dB  "
           f"noise {v['worst']['nrms']:.2f}dB")
        dg, note = _rail_diagnosis(result, npz_path, manifest, v["name"])
        pr("  DIAGNOSIS:")
        if note:
            pr(f"    (note: {note})")
        if dg:
            for s in dg:
                pr("    - " + s)
        else:
            pr("    - no dominant analytic defect detected (or diagnosis unavailable).")

    # ---- per current sink ----
    for v in cviews:
        pr("")
        pr("-" * 78)
        pr(f"CURRENT SINK '{v['name']}'   pin {v['pin']}   pol {v['pol']}")
        pr("-" * 78)
        g = v["grade"]
        pr(f"  GRADE: [{g['badge']}] {g['verdict']}"
           + (f"  ({'; '.join(g['reasons'])})" if g["reasons"] else ""))
        m = v["metrics"]

        def _f(x, fmt):                                   # NaN-safe (in-situ omits channels)
            return (fmt % x) if isinstance(x, (int, float)) and np.isfinite(x) else "n/a"
        sign = m["gdd_sign"] if m["sign_ok"] else f"{m['gdd_sign']}!{m['gt_sign']}"
        pr(f"  Idc={_f(m['idc_ua'], '%.3f')}uA  IVrms={_f(m['ivrms'], '%.2f')}%  "
           f"rout={_f(m['rout_M'], '%.1f')}Mohm  Cp={_f(m['cp_fF'], '%.1f')}fF")
        prms_tag = "" if m.get("cpsrr_observable", True) else " (unobservable-not graded)"
        pr(f"  gdd={_f(m['gdd_nS'], '%+.3f')}nS (sign {sign})  Yrms={_f(m['yrms'], '%.2f')}dB  "
           f"Prms={_f(m['prms'], '%.2f')}dB{prms_tag}  "
           f"PTAT GT/model={_f(m['ptat_g'], '%.3f')}/{_f(m['ptat_m'], '%.3f')}")
        pr(f"  panels measured in-situ: {', '.join(v['present']) or '(none)'}")
        for n in v["notes"]:
            pr(f"  note: {n}")
        dg = CD._diagnose(m)
        pr("  DIAGNOSIS:")
        if dg:
            for s in dg:
                pr("    - " + s)
        else:
            pr("    - all current-port analytic metrics within tolerance.")

    # ---- worst-case rollup (voltage vs current SEPARATE, mirrors fit_multiport.report) ----
    pr("")
    pr("=" * 78)
    vz = [v["worst"]["zrms"] for v in vviews]
    vp = [v["worst"]["prms"] for v in vviews]
    vn = [v["worst"]["nrms"] for v in vviews]
    cy = [v["metrics"]["yrms"] for v in cviews if np.isfinite(v["metrics"].get("yrms", np.nan))]
    cp = [v["metrics"]["prms"] for v in cviews if np.isfinite(v["metrics"].get("prms", np.nan))]
    pr(f"worst VOLTAGE : Zout {max(vz, default=0.0):.2f}dB  PSRR {max(vp, default=0.0):.2f}dB"
       f"  noise {max(vn, default=0.0):.2f}dB")
    pr(f"worst CURRENT : Y {max(cy, default=0.0):.2f}dB  current-PSRR {max(cp, default=0.0):.2f}dB")

    # ---- reproduce footer: SELF-CONTAINED. The manifest is INLINED and the GT travels as the
    # log-resampled [MPD1] digest emitted right after this block, so a paste rebuilds the npz and
    # fit_multiport reproduces the whole report locally -- NO npz crosses the air gap (the
    # single-port [7]/[8d] design, now extended to multi-port). The npz path is named for
    # REFERENCE only (not needed to reproduce).
    pr("")
    pr("=" * 78)
    pr("TO REPRODUCE — paste this WHOLE report; NO attachment needed. The GT travels as the")
    pr("log-resampled [MPD1] digest below; rebuild + reproduce locally with:")
    mpath = manifest.get("_path") if isinstance(manifest, dict) else None
    if mpath:
        pr(f"  origin manifest (run box, reference only) : {mpath}")
    pr(f"  origin npz (run box, NOT needed to reproduce): {npz_path}")
    pr("  manifest JSON (inlined — copy into a .json, or report_multiport.parse_manifest(text)):")
    try:
        import json as _json
        mclean = ({k: v for k, v in manifest.items() if not str(k).startswith("_")}
                  if isinstance(manifest, dict) else manifest)
        for line in _json.dumps(mclean, indent=2, default=str).splitlines():
            pr("    " + line)
    except Exception:                                  # noqa: BLE001 -- footer must never raise
        pr(f"    (manifest not JSON-serializable; name={mname})")
    pr("  # from the repo root (harness/ + cadence/ on sys.path); text = this whole report:")
    pr("  import fit_multiport, report_multiport")
    pr("  ref = report_multiport.digest_to_npz(text, 'repro.npz')  # rebuild npz from [MPD1] below")
    pr("  m   = report_multiport.parse_manifest(text)              # the inlined manifest above")
    pr("  res = fit_multiport.fit_multiport(ref, m)")
    pr("  print(report_multiport.debug_report(res, ref, m))        # == this report, reproduced")
    pr("=" * 78)
    # ---- the GT itself: log-resampled, machine-readable; digest_to_npz rebuilds the npz from it.
    # Loaded fresh from the npz (raw arrays) so every downstream sign/derivation re-runs on the
    # rebuild exactly as here. Guarded: the digest must never kill the report.
    try:
        from insitu import importmp as IM
        ref_full = IM.load_multiport(npz_path)
        try:                                              # carry voltage MODEL curves (best-effort)
            _views = port_views(result, npz_path, manifest)
        except Exception:                                 # noqa: BLE001 -- model-carry is optional
            _views = None
        pr("")
        for line in emit_multiport_digest(ref_full, manifest, views=_views):
            pr(line)
    except Exception as e:                             # noqa: BLE001 -- digest is best-effort
        pr("")
        pr(f"{_MPD_BEGIN}  (UNAVAILABLE: {type(e).__name__}: {e})")
        pr(_MPD_END)
    return "\n".join(L)
