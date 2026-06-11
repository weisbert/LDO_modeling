"""Text MODEL-vs-GT difference report -- analytic, NO simulator, NO plots.

Built for an airgapped red zone where you can't screenshot the overlays: it turns the
fit residuals into a plain-text diagnosis you can copy-paste. It compares the ANALYTIC
model (fit_model.predict -- the exact transfer functions the fitter optimizes, same as the
GUI Compare tab) against the imported GT npz, on the GT's own frequency grids. Pure
numpy/scipy; runs wherever the fit runs.

    python report.py --variant v3_miller            # results/ref/v3_miller.npz
    python report.py --ref results/ref/myldo.npz    # an imported real LDO
    python report.py --variant myldo --nominal 121u --vref 1.05

Writes results/score/report_<name>.txt and prints it. Covers Zout / PSRR / output-noise
(the analytic blocks). Transient & discrete-spur fidelity need the ngspice scorer
(score.py); this report says so rather than pretending to cover them.
"""
import argparse
import json
import pathlib
import numpy as np

import ng
import fit_model

SCOREDIR = ng.ROOT / "results" / "score"
BANDS = [8e6, 16e6, 24e6]
# same weights as score.py for the terms this analytic report can compute (no trms/spur here);
# zhf/phf mirror score.py's HF-extension terms and engage only when the ref has *_hf arrays
W = dict(zrms=1.0, zband=3.0, zphase=0.04, pkdb=1.0, pband=2.0, pphase=0.03, noise=0.5,
         zhf=0.5, phf=0.5)
TERM_DESC = dict(
    zrms="|Zout| magnitude RMS error (dB)",
    zband="|Zout| error in the 8/16/24 MHz spur bands (dB)",
    zphase="Zout phase RMS error (deg)",
    pkdb="Zout resonance-peak height error (dB)",
    pband="PSRR attenuation error in the spur bands (dB)",
    pphase="PSRR phase RMS error (deg)",
    noise="output-noise PSD log-RMS error (dB)",
    zhf="|Zout| RMS error in the *_hf extension band (dB)",
    phf="PSRR RMS error in the *_hf extension band (dB)",
)


def _wrapdeg(d):
    return (np.asarray(d) + 180.0) % 360.0 - 180.0


def _peak(f, mag, fmax=1e7):
    m = f < fmax
    i = int(np.argmax(mag * m))
    return float(f[i]), float(mag[i])


def _band_err(f, err):
    out = {}
    for fb in BANDS:
        m = (f >= 0.9 * fb) & (f <= 1.1 * fb)
        out[fb] = float(np.mean(np.abs(err[m]))) if m.any() else np.nan
    return out


def _decades(f, err):
    """Per-decade (mean|err|, signed_mean) so the report localizes WHERE error concentrates."""
    out = []
    lo = 10.0 ** np.floor(np.log10(f[0] + 1e-30))
    while lo < f[-1]:
        hi = lo * 10.0
        m = (f >= lo) & (f < hi)
        if m.any():
            out.append((lo, hi, float(np.mean(np.abs(err[m]))), float(np.mean(err[m]))))
        lo = hi
    return out


def _slope_db_dec(f, mag):
    """Local slope of |.| over the top decade, in dB/decade (sign tells roll-off direction)."""
    m = f >= f[-1] / 10.0
    if m.sum() < 2:
        return float("nan")
    lf, lm = np.log10(f[m]), 20 * np.log10(mag[m] + 1e-30)
    return float(np.polyfit(lf, lm, 1)[0])


def _corner(ref, P, nfk, il, nmode=None, nfkv=None):
    z, p, n = ref[f"z_{il}"], ref[f"p_{il}"], ref[f"noise_{il}"]
    fz, Zg = z[:, 0], z[:, 1] + 1j * z[:, 2]
    fp, Hg = p[:, 0], p[:, 1] + 1j * p[:, 2]
    fn, Sg = n[:, 0], n[:, 1]
    # nmode/nfkv from the FitResult (not module globals): the report may be built
    # from a HELD result after another fit ran in the same process (GUI).
    Zm = fit_model.predict(P, fz, nfk, nfkv=nfkv, nmode=nmode)["Zout"]
    Hm = fit_model.predict(P, fp, nfk, nfkv=nfkv, nmode=nmode)["PSRR"]
    Sm = fit_model.predict(P, fn, nfk, nfkv=nfkv, nmode=nmode)["noise"]
    az, am_ = np.abs(Zg), np.abs(Zm)
    ez = 20 * np.log10((am_ + 1e-30) / (az + 1e-30))
    ezph = _wrapdeg(np.degrees(np.angle(Zm) - np.angle(Zg)))
    ag, ap = -20 * np.log10(np.abs(Hg) + 1e-30), -20 * np.log10(np.abs(Hm) + 1e-30)
    ep = ap - ag
    epph = _wrapdeg(np.degrees(np.angle(Hm) - np.angle(Hg)))
    ndb = 20 * np.log10((Sm + 1e-30) / (Sg + 1e-30))
    hi = fz >= 1e3
    fpg, Zpg = _peak(fz, az)
    fpm, Zpm = _peak(fz, am_)
    nb = (fn >= 10) & (fn <= 100e6)
    resb = (fn > 0.5e6) & (fn < 3e6)
    iw = int(np.argmax(np.abs(ez)))
    iwp = int(np.argmax(np.abs(ezph)))
    iwpp = int(np.argmax(np.abs(epph)))
    iwn = nb & (np.abs(ndb) == np.max(np.abs(ndb[nb])))
    return dict(
        il=il, fz=fz, az=az, am=am_, ez=ez, ezph=ezph, fp=fp, ag=ag, ap=ap, ep=ep, epph=epph,
        fn=fn, Sg=Sg, Sm=Sm, ndb=ndb,
        zrms=float(np.sqrt(np.mean(ez[hi] ** 2))), zphase=float(np.sqrt(np.mean(ezph[hi] ** 2))),
        zb=_band_err(fz, ez), zband=float(np.nanmean(list(_band_err(fz, ez).values()))),
        zworst=(float(fz[iw]), float(ez[iw])), zphworst=(float(fz[iwp]), float(ezph[iwp])),
        zlf=(float(fz[0]), float(az[0]), float(am_[0]), float(ez[0])),
        zhf=(float(fz[-1]), float(az[-1]), float(am_[-1]), float(ez[-1]),
             _slope_db_dec(fz, az), _slope_db_dec(fz, am_)),
        pkf=fpm / fpg, pkdb=20 * np.log10(Zpm / Zpg), peak=(fpg, Zpg, fpm, Zpm),
        pb=_band_err(fp, ep), pband=float(np.nanmean(list(_band_err(fp, ep).values()))),
        pphase=float(np.sqrt(np.mean(epph[fp >= 1e3] ** 2))),
        plf=(float(fp[0]), float(ag[0]), float(ap[0]), float(ep[0])),
        pworst=(float(fp[np.argmax(np.abs(ep))]), float(ep[np.argmax(np.abs(ep))])),
        ppworst=(float(fp[iwpp]), float(epph[iwpp])),
        gt_notch=(float(fp[np.argmin(ag)]), float(ag.min())),
        npsd=float(np.sqrt(np.mean(ndb[nb] ** 2))),
        npk=float(20 * np.log10(Sm[resb].max() / Sg[resb].max())) if resb.any() else float("nan"),
        nlf=float(ndb[fn <= 1e3].mean()) if (fn <= 1e3).any() else float("nan"),
        nhf=float(ndb[(fn >= 1e6) & nb].mean()) if ((fn >= 1e6) & nb).any() else float("nan"),
        nworst=(float(fn[nb][np.argmax(np.abs(ndb[nb]))]), float(ndb[nb][np.argmax(np.abs(ndb[nb]))])),
    )


def _fmt_hz(f):
    for div, u in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= div:
            return f"{f/div:.3g}{u}"
    return f"{f:.3g}Hz"


def _hi_lo(v, unit="dB"):
    return f"{v:+.2f}{unit} ({'model HIGH' if v > 0 else 'model LOW'})"


def build_report(ref, result, name, refpath="", with_sim_note=True):
    """Assemble the full text report from the GT npz dict + a fitted FitResult. Returns a str."""
    P, nfk = result.P, result.nfk
    loads = [str(x) for x in result.loads]
    nom = str(result.nominal)
    cs = [_corner(ref, P[il], nfk, il,
                  nmode=getattr(result, "nmode", None),
                  nfkv=getattr(result, "nfkv", None)) for il in loads]
    cnom = next(c for c in cs if c["il"] == nom)
    L = []
    pr = L.append

    # ---- header ----
    pr("=" * 84)
    pr(f"LDO MODEL DIFFERENCE REPORT  --  '{name}'   (analytic predict vs GT, no simulator)")
    pr("=" * 84)
    pr(f"ref={refpath or '(npz)'}   corners={loads}  nominal={nom}  vref={result.vref:.4g}V")
    try:    # build fingerprint: stamped into app/ by deploy/package.py (air-gap forensics --
            # identical reports across rounds usually mean a stale bundle was re-applied)
        bi = json.loads((ng.ROOT / "BUILD_INFO.json").read_text())
        pr(f"tool build: {bi.get('git_sha', '?')[:9]}  ({bi.get('built_utc', '?')})")
    except (OSError, ValueError):
        pr("tool build: dev (no BUILD_INFO.json -- running from the repo)")
    dc, de = float(ref.get("meta_cout", np.nan)), float(ref.get("meta_esr", np.nan))
    pr(f"Cout/ESR: extracted {result.cout*1e12:.1f}pF / {result.esr:.3f}ohm"
       + (f"   design {dc*1e12:.1f}pF / {de:.3f}ohm" if np.isfinite(dc) else "   design n/a"))
    cft = getattr(result, "cft", 0.0)
    if cft > 0:
        pr(f"C_ft: {cft*1e15:.1f}fF (vin->vout feedthrough)")
    if getattr(result, "nmode", "norton") == "hybrid":   # getattr: old pickled results
        nfkv = getattr(result, "nfkv", [])
        pr(f"noise: HYBRID series bank {len(nfkv)} sections @ fvk[Hz] = "
           + " ".join(f"{x:.3g}" for x in nfkv)
           + "   (+ Norton white floor @vout)")
    else:
        pr(f"noise bank: {len(nfk)} sections @ fk[Hz] = "
           + " ".join(f"{x:.3g}" for x in nfk))
    sfx = ref.get("z_signfix")
    if sfx is not None and np.size(sfx):
        pr("NOTE: Zout sign was auto-corrected at import (inverted V/I in the export) for: "
           + ", ".join(str(x) for x in np.ravel(sfx)))

    # ---- [1] where the error is (weighted composite breakdown) ----
    agg = dict(zrms=np.mean([c["zrms"] for c in cs]), zband=np.mean([c["zband"] for c in cs]),
               zphase=np.mean([c["zphase"] for c in cs]), pkdb=np.mean([abs(c["pkdb"]) for c in cs]),
               pband=np.mean([c["pband"] for c in cs]), pphase=np.mean([c["pphase"] for c in cs]),
               noise=np.mean([c["npsd"] for c in cs]))
    # HF-extension terms (mirror score.py's zhf/phf): analytic predict on the *_hf grid
    # above the in-band top. Gated on the ref actually carrying *_hf arrays, so a digest
    # ref (whose in-band z already reaches the ceiling) keeps its composite UNCHANGED.
    kzhf = next((k for k in (f"z_{nom}_hf", "z_121u_hf") if k in ref), None)
    if kzhf is not None:
        gzh = ref[kzhf]
        fh, Zgh = gzh[:, 0], gzh[:, 1] + 1j * gzh[:, 2]
        mh = fh > float(ref[f"z_{nom}"][:, 0].max())
        if mh.sum() >= 3:
            predh = fit_model.predict(P[nom], fh[mh], nfk,
                                      nmode=getattr(result, "nmode", None),
                                      nfkv=getattr(result, "nfkv", None))
            ezh = 20 * np.log10((np.abs(predh["Zout"]) + 1e-30) / (np.abs(Zgh[mh]) + 1e-30))
            agg["zhf"] = float(np.sqrt(np.mean(ezh ** 2)))
            kphf = next((k for k in (f"p_{nom}_hf", "p_121u_hf") if k in ref), None)
            if kphf is not None:
                gph = ref[kphf]
                fhp, Hgh = gph[:, 0], gph[:, 1] + 1j * gph[:, 2]
                mp = fhp > float(ref[f"p_{nom}"][:, 0].max())
                if mp.sum() >= 3:
                    predp = fit_model.predict(P[nom], fhp[mp], nfk,
                                              nmode=getattr(result, "nmode", None),
                                              nfkv=getattr(result, "nfkv", None))
                    eph = (np.clip(-20 * np.log10(np.abs(predp["PSRR"]) + 1e-30), None, 200)
                           - (-20 * np.log10(np.abs(Hgh[mp]) + 1e-30)))
                    agg["phf"] = float(np.sqrt(np.mean(eph ** 2)))
    terms = sorted(((k, W[k] * agg[k], agg[k], W[k]) for k in agg), key=lambda x: -x[1])
    comp = sum(t[1] for t in terms)
    pr("\n[1] WHERE THE ERROR IS  (composite split into weighted terms, worst first)")
    pr("-" * 84)
    pr(f"  {'term':<8} {'weighted':>9}  = {'raw':>7} x {'wt':>4}   {'what it measures'}")
    for k, wv, raw, wt in terms:
        pr(f"  {k:<8} {wv:>9.2f}  = {raw:>7.2f} x {wt:>4.2g}   {TERM_DESC[k]}")
    pr(f"  {'-'*8} {'-'*9}")
    pr(f"  {'TOTAL':<8} {comp:>9.2f}   (analytic composite; excludes transient+spur -> run score.py for those)")
    pr(f"  => dominant: '{terms[0][0]}' ({TERM_DESC[terms[0][0]]}). Fix this first.")

    # ---- [2] scorecard ----
    pr("\n[2] SCORECARD  (per load corner; errors in dB / deg)")
    pr("-" * 84)
    pr(f"  {'load':>5} | {'Zrms':>5} {'Zband':>6} {'Zdeg':>5} {'pk_df':>6} {'pk_dB':>6} |"
       f" {'Pband':>6} {'Pdeg':>5} | {'Npsd':>5} {'Npk':>5}")
    for c in cs:
        pr(f"  {c['il']:>5} | {c['zrms']:5.2f} {c['zband']:6.2f} {c['zphase']:5.1f}"
           f" {c['pkf']:6.2f} {c['pkdb']:+6.1f} | {c['pband']:6.2f} {c['pphase']:5.1f} |"
           f" {c['npsd']:5.1f} {c['npk']:+5.1f}")
    pr("  (pk_df = model/GT resonance-freq ratio; 1.00 = co-located)")

    # ---- [3] Zout ----
    pr("\n[3] Zout  -- magnitude & phase")
    pr("-" * 84)
    for c in cs:
        fpg, Zpg, fpm, Zpm = c["peak"]
        f0, z0g, z0m, e0 = c["zlf"]
        fH, zHg, zHm, eH, sg, sm = c["zhf"]
        wf, wv = c["zworst"]
        pr(f"  corner {c['il']}:")
        pr(f"    |Z| err : RMS {c['zrms']:.2f}dB, worst {_hi_lo(wv)} @ {_fmt_hz(wf)}")
        pr(f"    LF floor: GT {z0g:.3g}ohm  model {z0m:.3g}ohm  ({_hi_lo(e0)}) @ {_fmt_hz(f0)}")
        pr(f"    resonance: GT {Zpg:.3g}ohm @ {_fmt_hz(fpg)} | model {Zpm:.3g}ohm @ {_fmt_hz(fpm)}"
           f"  -> peak {_hi_lo(c['pkdb'])}, freq x{c['pkf']:.2f}")
        pr(f"    HF tail : GT {zHg:.3g}ohm  model {zHm:.3g}ohm @ {_fmt_hz(fH)}"
           f"  (slope GT {sg:.0f} / model {sm:.0f} dB/dec)")
        pr(f"    by decade |err|dB: " + "  ".join(
            f"{_fmt_hz(lo)}:{ma:.1f}" for lo, hi, ma, sgn in _decades(c["fz"], c["ez"])))
        pr(f"    phase err: RMS {c['zphase']:.1f}deg, worst {c['zphworst'][1]:+.0f}deg @ "
           f"{_fmt_hz(c['zphworst'][0])}")

    # ---- [4] PSRR ----
    pr("\n[4] PSRR  -- attenuation & phase  (atten = -20log10|H|; higher = better rejection)")
    pr("-" * 84)
    for c in cs:
        f0, a0g, a0m, e0 = c["plf"]
        pr(f"  corner {c['il']}:")
        pr(f"    DC PSRR : GT {a0g:.1f}dB  model {a0m:.1f}dB  ({e0:+.1f}dB)")
        pr(f"    band err: 8/16/24MHz " + " ".join(f"{fb/1e6:.0f}M:{c['pb'][fb]:+.2f}" for fb in BANDS)
           + f"  (mean {c['pband']:.2f}dB)")
        pr(f"    GT worst-notch: {c['gt_notch'][1]:.1f}dB @ {_fmt_hz(c['gt_notch'][0])}"
           f"  | worst atten err {_hi_lo(c['pworst'][1])} @ {_fmt_hz(c['pworst'][0])}")
        pr(f"    by decade |err|dB: " + "  ".join(
            f"{_fmt_hz(lo)}:{ma:.1f}" for lo, hi, ma, sgn in _decades(c["fp"], c["ep"])))
        pr(f"    phase err: RMS {c['pphase']:.1f}deg, worst {c['ppworst'][1]:+.0f}deg @ "
           f"{_fmt_hz(c['ppworst'][0])}")

    # ---- [5] noise ----
    pr("\n[5] OUTPUT NOISE  -- PSD shape (Sv = In*|Zout|, so Zout errors leak in here too)")
    pr("-" * 84)
    for c in cs:
        pr(f"  corner {c['il']}: PSD log-RMS {c['npsd']:.2f}dB | LF(flicker) {c['nlf']:+.1f}dB"
           f"  HF(white) {c['nhf']:+.1f}dB  res-peak {c['npk']:+.1f}dB"
           f"  | worst {_hi_lo(c['nworst'][1])} @ {_fmt_hz(c['nworst'][0])}")
        pr(f"    by decade |err|dB: " + "  ".join(
            f"{_fmt_hz(lo)}:{ma:.1f}" for lo, hi, ma, sgn in _decades(c["fn"], c["ndb"])))

    # ---- [6] plain-language diagnosis ----
    pr("\n[6] PLAIN-LANGUAGE DIAGNOSIS  (paste this whole file to the modeler)")
    pr("-" * 84)
    dg = _diagnose(cnom, cs, result, ref, terms)
    if dg:
        for s in dg:
            pr("  - " + s)
    else:
        pr("  - All analytic metrics within tolerance; no dominant defect detected.")
    if with_sim_note:
        pr("\n  NOTE: transient (load-step droop/ring) and discrete-spur fidelity are NOT in this")
        pr("        analytic report -- they need the ngspice scorer:  python score.py --variant "
           f"{name}")

    # ---- [7] GT digest: log-resampled ground truth, machine-readable. This is the
    #      AIR-GAP DATA CHANNEL: no npz can cross the gap, but this text can -- pasting
    #      the report hands the modeler the actual curves to fit against locally.
    pr("\n[7] GT DIGEST  (log-resampled ground truth, ~5 pts/decade; machine-readable)")
    pr("    columns: f[Hz], |Z|[ohm], Zph[deg], PSRRatt[dB], Hph[deg], Sv[V/rtHz]")
    pr("-" * 84)
    for il in loads:
        gz = ref[f"z_{il}"]
        fz = gz[:, 0]
        grid = np.logspace(np.log10(fz[0]), np.log10(fz[-1]),
                           max(int(5 * np.log10(fz[-1] / fz[0])) + 1, 8))
        # densify around the |Z| resonance: 5/dec under-samples a sharp peak and the
        # digest is all the modeler gets across the air gap (digest_import's
        # sufficiency check WARNs on an under-resolved peak; this prevents it).
        zmag = np.abs(gz[:, 1] + 1j * gz[:, 2])
        ipk = int(np.argmax(zmag))
        if 0 < ipk < len(fz) - 1 and zmag[ipk] > 2.0 * float(np.median(zmag)):
            fpk = fz[ipk]
            extra = np.logspace(np.log10(max(fpk / 2.2, fz[0])),
                                np.log10(min(fpk * 2.2, fz[-1])), 12)
            grid = np.unique(np.concatenate([grid, extra]))
        Z = _cint(grid, fz, gz[:, 1] + 1j * gz[:, 2])
        cols = [np.abs(Z), np.degrees(np.angle(Z))]
        gp = ref.get(f"p_{il}")
        if gp is not None:
            H = _cint(grid, gp[:, 0], gp[:, 1] + 1j * gp[:, 2])
            cols += [-20 * np.log10(np.abs(H) + 1e-30), np.degrees(np.angle(H))]
        else:
            cols += [np.full(grid.size, np.nan)] * 2
        gn = ref.get(f"noise_{il}")
        if gn is not None:
            Sv = np.exp(np.interp(np.log(grid), np.log(gn[:, 0]),
                                  np.log(gn[:, 1] + 1e-300), left=np.nan, right=np.nan))
        else:
            Sv = np.full(grid.size, np.nan)
        cols.append(Sv)
        pr(f"  # corner {il}")
        for i, fv in enumerate(grid):
            pr(f"  {fv:.4e}, " + ", ".join(
                "-" if not np.isfinite(c[i]) else f"{c[i]:.4g}" for c in cols))
    # DC curves (corner-independent), so the yellow-zone replica gets the REAL
    # load-reg / line-reg / dropout instead of synthesized placeholders -- makes the
    # replica's slew_en=1 dropout table and the vdd line-reg tracking real too.
    for key, desc in (("dc_loadreg", "iload[A], vout[V]"),
                      ("dc_linereg", "vin[V], vout[V]"),
                      ("dc_dropout", "iload[A], vout[V]")):
        d = ref.get(key)
        if d is None or len(d) < 2:
            continue
        idx = np.unique(np.linspace(0, len(d) - 1, min(len(d), 64)).astype(int))
        pr(f"  # dcblock {key}  (columns: {desc})")
        for i in idx:
            pr(f"  {d[i, 0]:.6e}, {d[i, 1]:.6e}")
    pr("=" * 84)
    return "\n".join(L)


def _cint(fq, f, z):
    """Complex interp in log-f; NaN outside the data range."""
    re = np.interp(np.log(fq), np.log(f), z.real, left=np.nan, right=np.nan)
    im = np.interp(np.log(fq), np.log(f), z.imag, left=np.nan, right=np.nan)
    return re + 1j * im


def _diagnose(cnom, cs, result, ref, terms):
    """Threshold-fired, plain-language findings, ordered by composite impact. Each string names
    the symptom, the numbers, and the likely physical cause -- the vocabulary the user asked for."""
    d = []
    # resonance mislocation (top of the list if it's the dominant term)
    if abs(cnom["pkf"] - 1.0) > 0.12:
        sign = "HIGH" if cnom["pkf"] > 1 else "LOW"
        d.append(f"Zout RESONANCE MISLOCATED: model peak at {_fmt_hz(cnom['peak'][2])} vs GT "
                 f"{_fmt_hz(cnom['peak'][0])} (x{cnom['pkf']:.2f}, too {sign}). The output pole "
                 f"(L_a x Cout) is off -- check the fitted L_a and Cout.")
    if abs(cnom["pkdb"]) > 2.0:
        d.append(f"Zout resonance HEIGHT off by {cnom['pkdb']:+.1f}dB at nominal (and similar at "
                 f"other corners) -- damping/Q wrong: the R_pl across L_a, or a missing 2nd RLC "
                 f"branch if the GT has >1 resonance.")
    # Cout/ESR design mismatch (explains HF tail + resonance freq)
    dc = float(ref.get("meta_cout", np.nan))
    if np.isfinite(dc) and dc > 0 and abs(result.cout / dc - 1.0) > 0.2:
        d.append(f"Cout extracted {result.cout*1e12:.0f}pF vs design {dc*1e12:.0f}pF "
                 f"({(result.cout/dc-1)*100:+.0f}%) -- this sets the HF Zout tail AND the resonance "
                 f"frequency; a bad cap/ESR extraction propagates to both. Provide the *_hf 500MHz "
                 f"Zout sweep for a robust extraction.")
    # LF floor / load-reg
    if abs(cnom["zlf"][3]) > 1.0:
        d.append(f"Zout LF floor off by {cnom['zlf'][3]:+.1f}dB -- DC output resistance / load-"
                 f"regulation mismatch (R_a or the loop DC gain).")
    # Zout in spur bands dominating
    if cnom["zband"] > 2.0:
        d.append(f"Zout error is concentrated in the 8/16/24MHz spur bands ({cnom['zband']:.1f}dB "
                 f"mean) -- this term is x3 in the composite, so it dominates the score even when "
                 f"the broadband RMS looks ok.")
    # PSRR phase / non-min-phase
    if cnom["pphase"] > 10.0:
        d.append(f"PSRR PHASE diverges (RMS {cnom['pphase']:.0f}deg, worst {cnom['ppworst'][1]:+.0f}"
                 f"deg @ {_fmt_hz(cnom['ppworst'][0])}) -- classic NON-MINIMUM-PHASE / transport-"
                 f"delay behavior the real-pole PSRR bank can't capture. Needs a complex-conjugate "
                 f"2nd-order section (or delay all-pass); see analyze_psrr_phase.py.")
    if cnom["pband"] > 2.0:
        d.append(f"PSRR attenuation off in the spur bands ({cnom['pband']:.1f}dB mean) -- the "
                 f"PSRR=i_c x Zout shelf shares Zout's resonance; if Zout is wrong here, PSRR follows.")
    if abs(cnom["plf"][3]) > 3.0:
        d.append(f"DC PSRR off by {cnom['plf'][3]:+.1f}dB -- loop-gain / Vref scaling at low freq.")
    # noise shape
    if cnom["npsd"] > 3.0:
        where = ("LF/flicker" if abs(cnom["nlf"]) >= max(abs(cnom["nhf"]), abs(cnom["npk"]))
                 else "resonance-band" if abs(cnom["npk"]) >= abs(cnom["nhf"]) else "HF/white")
        d.append(f"Noise PSD shape off (log-RMS {cnom['npsd']:.1f}dB), worst in the {where} region. "
                 + ("Add/retune a flicker Lorentzian corner." if where == "LF/flicker"
                    else "Since Sv=In x |Zout|, this likely just mirrors the Zout resonance error above."
                    if where == "resonance-band" else "Raise/lower the white In floor (gnw)."))
    # cross-corner consistency: does the defect track across corners or only one?
    zb = [c["zband"] for c in cs]
    if max(zb) > 2 * (min(zb) + 1e-6) and max(zb) > 2.0:
        wc = cs[int(np.argmax(zb))]["il"]
        d.append(f"The Zout-band error is far worse at corner {wc} than the others -- a per-corner "
                 f"(load-dependent) effect the ln(iload) interpolation isn't capturing; check that "
                 f"corner's extraction.")
    return d


def write_report(name, txt):
    SCOREDIR.mkdir(parents=True, exist_ok=True)
    out = SCOREDIR / f"report_{name}.txt"
    out.write_text(txt, encoding="utf-8")
    return out


def main(variant=None, refpath=None, nominal=None, vref=None):
    if refpath:
        refpath = pathlib.Path(refpath)
        name = refpath.stem
    else:
        name = variant
        refpath = ng.ROOT / "results" / "ref" / f"{name}.npz"
    ref = {k: v for k, v in np.load(refpath, allow_pickle=True).items()}
    result = fit_model.fit_variant(name, nominal=nominal, vref=vref)
    txt = build_report(ref, result, name, refpath=str(refpath))
    out = write_report(name, txt)
    print(txt)
    print(f"\nwrote {out}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Text model-vs-GT difference report (analytic, no sim)")
    ap.add_argument("--variant", default=None, help="reference stem in results/ref/<variant>.npz")
    ap.add_argument("--ref", default=None, help="explicit results/ref/<name>.npz path")
    ap.add_argument("--nominal", default=None)
    ap.add_argument("--vref", type=float, default=None)
    a = ap.parse_args()
    if not a.variant and not a.ref:
        ap.error("give --variant or --ref")
    main(variant=a.variant, refpath=a.ref, nominal=a.nominal, vref=a.vref)
