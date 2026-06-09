"""OUT-OF-SAMPLE validation guardrails for the LDO behavioral model.

The score.py feedback loop is IN-SAMPLE by construction: it re-simulates the
emitted model on the SAME load corners / frequencies the fitter already saw
(bench.LOADS, bench.AC). The methodology audit (REVIEW_methodology_audit.md)
reproduced overfitting that this can never catch -- the per-param quadratic in
ln(iload) is forced through exactly 3 corners (0 residual DOF) and generalizes
badly between/around them. This module adds the missing held-out checks, as a
PURE INCREMENT (it does not touch fit_model's fit/emit nor score's composite):

  1. loco()            leave-one-load-corner-out cross-validation. Build the
                       ln(iload) interpolant from the OTHER N-1 corners (linear
                       when 2 retained), predict the held-out corner's
                       Zout/PSRR/noise, score vs GT. In-sample vs held-out.
                       Gate: held-out RMS <= ~2x in-sample.
  2. offgrid()         a held-out load BETWEEN corners (geometric midpoints),
                       generated from the GT and scored through the *emitted*
                       .lib in ngspice -- a true off-grid load that also
                       exercises the _pexpr clamp / interpolation overshoot.
  3. identifiability()  per block per corner: cond(J) / singular values of the
                       fit Jacobian. Params with sigma < ~1e-3*sigma_max are
                       UNIDENTIFIABLE; a param that is identifiable at some
                       corner but swings >1e3x across corners is a HARMFUL
                       SWITCH being interpolated as continuous (e.g. R_pl
                       ON/OFF); the rest are low-harm INVISIBLE (dead R_b=1e9,
                       a high-ESR invisible cap). base/v1/v2 Zout -> cond=inf.

General by construction: everything derives from ref["loads"] and RELATIVE
thresholds -- no 304MHz / 8-24MHz / 121uA / "3 corners" hardcoded.

    python crossval.py --variant base          # one DUT
    python crossval.py --all                    # whole registry -> matrix.md
    python crossval.py --variant base --strict  # exit nonzero on any gate FAIL
"""
import argparse
import json
import numpy as np

import ng
import bench
import variants
import fit_model
import score as scoremod

OUT = ng.ROOT / "results" / "crossval"

# Gate knobs (relative -> general). Held-out RMS may exceed in-sample by this
# factor (plus a small absolute floor so a ~0 in-sample value is not a div trap).
LOCO_FACTOR = 2.0
LOCO_FLOOR_DB = 0.5
# identifiability thresholds (relative -> general)
UNIDENT_REL = 1e-3      # column-norm / max column-norm below this => unidentifiable
RANKDEF_REL = 1e-9      # sigma_min / sigma_max below this => rank-deficient (cond~inf)
SWITCH_RATIO = 1e3      # max/min of a param across corners above this => switch-like

# Per-param interpolation spec MIRRORING fit_model.emit()'s `specs`
# (fit_model.py:708-713), restricted to the keys predict() consumes. (key, logspace).
# There is no public accessor; keep in sync if emit's spec list ever changes.
_ZP_SPECS = [("R_a", True), ("L_a", True), ("R_pl", True), ("R_b", True), ("L_b", True),
             ("G0", False), ("G1", False), ("w1", True), ("G2", False), ("w2", True),
             ("G3", False), ("w3", True),
             ("pcb0", False), ("pcb1", False), ("pcw0", True), ("pcq", True), ("gnw", True)]


def _specs():
    return _ZP_SPECS + [(f"gn{k+1}", True) for k in range(fit_model.MNOISE)]


# ---------------------------------------------------------------- interpolation
def _interp_params(P, retained, target_iv, specs):
    """Interpolate each fitted param at load=target_iv from the RETAINED corners,
    in u=ln(iload), CLAMPED to the retained-corner envelope exactly as the emitted
    model does (fit_model._pexpr). Degree = min(len(retained)-1, 2): 3 retained ->
    quadratic, 2 -> linear (the audit's defensible 2-point interpolant), 1 ->
    constant. Clamping mirrors deployment, so a held-out END corner (extrapolation)
    is bounded to the measured envelope instead of blowing up; the held-out MIDDLE
    corner (interpolation) is inside the envelope so the clamp is inactive and the
    residual gap there is the true few-corner overfit."""
    u = np.array([np.log(P[il]["iv"]) for il in retained])
    # mirror the emitted model's LOAD clamp ic=min(max(iload,LOADS[0]),LOADS[-1]) (fit_model
    # emit): a held-out load outside the retained range uses the boundary corner's params,
    # exactly as a deployed (N-1)-corner model would (no silent extrapolation).
    ut = float(np.clip(np.log(target_iv), u.min(), u.max()))
    deg = max(0, min(len(retained) - 1, 2))
    M, ADD = fit_model.CLAMP_M, fit_model.CLAMP_ADD   # mirror the emit-time envelope clamp
    out = {}
    for key, logspace in specs:
        y = np.array([float(P[il][key]) for il in retained])
        if logspace:
            c = np.polyfit(u, np.log(np.abs(y) + 1e-300), deg)
            v = float(np.exp(np.polyval(c, ut)))
            out[key] = float(np.clip(v, y.min() / M, y.max() * M))
        else:
            c = np.polyfit(u, y, deg)
            v = float(np.polyval(c, ut))
            pad = ADD * (y.max() - y.min())
            out[key] = float(np.clip(v, y.min() - pad, y.max() + pad))
    return out


# --------------------------------------------------------------------- metrics
def _zrms(f, Zm, Zg, fmin=1e3):
    """Zout magnitude error RMS in dB over the score band (mirrors score.py:143)."""
    sel = f >= fmin
    return float(np.sqrt(np.mean((20 * np.log10(np.abs(Zm[sel]) / np.abs(Zg[sel]))) ** 2)))


def _prms(f, Hm, Hg, fmin=1e3):
    """PSRR attenuation error RMS in dB over the score band. Magnitude only
    (atten = -20log10|H|, like score.py:121-122) -> matches the audit's PSRR
    LOCO dB; a complex-log RMS would report a different (larger) number."""
    sel = f >= fmin
    e = scoremod._atten(Hm[sel]) - scoremod._atten(Hg[sel])
    return float(np.sqrt(np.mean(e ** 2)))


def _gate(insample, heldout):
    return bool(heldout <= max(LOCO_FACTOR * insample, insample + LOCO_FLOOR_DB))


# ------------------------------------------------------------------------ LOCO
def loco(res, refnpz):
    """Leave-one-load-corner-out cross-validation (analytic, no ngspice)."""
    P, nfk, loads = res.P, res.nfk, res.loads
    specs = _specs()
    amps = {il: ng.amps(il) for il in loads}
    corners = []
    for h in loads:
        retained = [il for il in loads if il != h]
        rmin, rmax = min(amps[r] for r in retained), max(amps[r] for r in retained)
        kind = "interp" if rmin <= amps[h] <= rmax else "extrap"
        interp = _interp_params(P, retained, amps[h], specs)

        gz = refnpz[f"z_{h}"]; fz, Zg = gz[:, 0], gz[:, 1] + 1j * gz[:, 2]
        gp = refnpz[f"p_{h}"]; fp, Hg = gp[:, 0], gp[:, 1] + 1j * gp[:, 2]
        gn = refnpz[f"noise_{h}"]; fn, Sg = gn[:, 0], gn[:, 1]

        zi = _zrms(fz, fit_model.predict(P[h], fz, nfk)["Zout"], Zg)
        zo = _zrms(fz, fit_model.predict(interp, fz, nfk)["Zout"], Zg)
        pi = _prms(fp, fit_model.predict(P[h], fp, nfk)["PSRR"], Hg)
        po = _prms(fp, fit_model.predict(interp, fp, nfk)["PSRR"], Hg)
        ni = scoremod._noise_metrics(fn, Sg, fn, fit_model.predict(P[h], fn, nfk)["noise"])["psd_rms"]
        no = scoremod._noise_metrics(fn, Sg, fn, fit_model.predict(interp, fn, nfk)["noise"])["psd_rms"]

        corners.append(dict(
            corner=h, kind=kind,
            zout=dict(insample=zi, heldout=zo, ok=_gate(zi, zo)),
            psrr=dict(insample=pi, heldout=po, ok=_gate(pi, po)),
            noise=dict(insample=ni, heldout=no, ok=_gate(ni, no)),
        ))
    ok = all(c[m]["ok"] for c in corners for m in ("zout", "psrr", "noise"))
    return dict(corners=corners, pass_=ok,
                note="held-out mirrors the emitted model (interpolant CLAMPED to the "
                     "retained-corner envelope); 'interp' (middle corner held out) is the "
                     "bracketed, clamp-inactive case showing the true few-corner gap, "
                     "'extrap' end-corners are bounded by the clamp. Noise LOCO is "
                     "amplitude-OOS / corner-freq-in-sample (NFK Lorentzian poles are "
                     "load-independent, fit jointly).")


# --------------------------------------------------------------------- offgrid
def _key(a):
    """amps -> ngspice-safe literal (bare scientific, no SI suffix needed)."""
    return f"{a:.6e}"


def offgrid(res, vkey):
    """Held-out load BETWEEN corners: GT vs the EMITTED .lib (which interpolates
    internally through the _pexpr clamp) in ngspice. Geometric midpoints of
    consecutive corners -> N-1 off-grid loads."""
    P, loads = res.P, res.loads
    name = "ldo_model" if vkey == "base" else f"ldo_{vkey}"
    OUT.mkdir(parents=True, exist_ok=True)
    crosslib = OUT / f"{name}.lib"
    fit_model.emit(P, crosslib)                      # non-invasive: under results/crossval

    v = variants.get(vkey)
    gtlibs, gtsub, gtxp = v["libs"], v["subckt"], v["xparams"]
    cs = sorted(ng.amps(il) for il in loads)
    mids = [float(np.sqrt(cs[i] * cs[i + 1])) for i in range(len(cs) - 1)]

    # in-sample worst-corner error (analytic) as the off-grid comparison baseline
    base_z = base_p = base_n = 0.0
    for il in loads:
        gz = res_ref(vkey, "z", il); gp = res_ref(vkey, "p", il); gn = res_ref(vkey, "noise", il)
        fz, Zg = gz[:, 0], gz[:, 1] + 1j * gz[:, 2]
        fp, Hg = gp[:, 0], gp[:, 1] + 1j * gp[:, 2]
        fn, Sg = gn[:, 0], gn[:, 1]
        base_z = max(base_z, _zrms(fz, fit_model.predict(P[il], fz, res.nfk)["Zout"], Zg))
        base_p = max(base_p, _prms(fp, fit_model.predict(P[il], fp, res.nfk)["PSRR"], Hg))
        base_n = max(base_n, scoremod._noise_metrics(
            fn, Sg, fn, fit_model.predict(P[il], fn, res.nfk)["noise"])["psd_rms"])

    rows = []
    for m in mids:
        mk = _key(m)
        fzg, Zg = bench.measure_zout(gtlibs, gtsub, mk, xparams=gtxp)
        fpg, Hg = bench.measure_psrr(gtlibs, gtsub, mk, xparams=gtxp)
        fng, Sg = bench.measure_noise(gtlibs, gtsub, mk, xparams=gtxp)
        xpm = f"iload={mk}"
        fzm, Zm = bench.measure_zout([crosslib], "ldo_model", mk, xparams=xpm)
        fpm, Hm = bench.measure_psrr([crosslib], "ldo_model", mk, xparams=xpm)
        fnm, Sm = bench.measure_noise([crosslib], "ldo_model", mk, xparams=xpm)
        zr = _zrms(fzg, scoremod._interp_cplx(fzg, fzm, Zm), Zg)
        pr = _prms(fpg, scoremod._interp_cplx(fpg, fpm, Hm), Hg)
        nr = scoremod._noise_metrics(fng, Sg, fnm, Sm)["psd_rms"]
        rows.append(dict(iload=m, iload_uA=m * 1e6,
                         zout=dict(rms=zr, ok=_gate(base_z, zr)),
                         psrr=dict(rms=pr, ok=_gate(base_p, pr)),
                         noise=dict(rms=nr, ok=_gate(base_n, nr))))
    ok = all(r[k]["ok"] for r in rows for k in ("zout", "psrr", "noise"))
    return dict(mids=rows, insample_worst=dict(zout=base_z, psrr=base_p, noise=base_n),
                pass_=ok, lib=str(crosslib))


_REFCACHE = {}


def res_ref(vkey, q, il):
    """Cached GT array accessor (avoids re-loading the npz per corner)."""
    if vkey not in _REFCACHE:
        _REFCACHE[vkey] = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
    return _REFCACHE[vkey][f"{q}_{il}"]


# ------------------------------------------------------------- identifiability
def _jacobian(g, params, delta=1e-4):
    """Relative (log-sensitivity) Jacobian of complex transfer g(params): a RELATIVE
    perturbation p -> p*(1+delta) on every param via ln-ratio finite differences, so
    column k ~ d ln g / d ln|p_k| -- unit-free and comparable across params of any
    sign/scale (gains ~1e-3, freqs ~1e6, the b1*s coeff ~1e-9 all on one footing).
    Returns (colnorm, singular_values, cond).

    A param with ~zero sensitivity (R_pl=1e9 in zmodel -> changing it barely moves Z)
    OR sitting at exactly 0 (an inert PSRR section coeff -> p*(1+delta) stays 0 -> zero
    column) yields a ~zero column => sigma_min -> 0 => cond -> inf. This reproduces the
    audit's base/v1/v2 Zout cond=inf AND correctly flags off sections as unidentifiable
    (an absolute step on a 0-valued b1 would instead inject a huge fake HF column)."""
    params = np.asarray(params, float)
    g0 = g(params)
    cols = []
    for k in range(len(params)):
        pp = params.copy()
        pp[k] = params[k] * (1.0 + delta)
        dl = np.log(g(pp) / g0) / delta
        cols.append(np.concatenate([dl.real, dl.imag]))
    J = np.column_stack(cols)
    colnorm = np.linalg.norm(J, axis=0)
    sv = np.linalg.svd(J, compute_uv=False)
    smin, smax = float(sv[-1]), float(sv[0])
    cond = (smax / smin) if smin > 0 else float("inf")
    return colnorm, sv, cond


def _classify(keys, per_corner_colnorm, value_across_corners):
    """OK / SWITCH(harmful) / INVISIBLE(low-harm) per param, from per-corner
    identifiability + cross-corner value range."""
    out = {}
    for j, key in enumerate(keys):
        ident_any = any(cn[j] / (cn.max() + 1e-300) >= UNIDENT_REL for cn in per_corner_colnorm)
        vals = np.abs(np.array(value_across_corners[key], float))
        ratio = float(vals.max() / (vals.min() + 1e-300))
        if ident_any and ratio > SWITCH_RATIO:
            cls = "SWITCH"          # influential somewhere but ON/OFF across corners
        elif not ident_any:
            cls = "INVISIBLE"       # unidentifiable everywhere -> low-harm to interpolate
        else:
            cls = "OK"
        out[key] = dict(cls=cls, ratio=ratio,
                        vals=[float(x) for x in value_across_corners[key]])
    return out


def identifiability(res, refnpz):
    """cond(J)/singular-values per block per corner + harmful-switch detection."""
    P, loads = res.P, res.loads
    blocks = {}

    # ---- Zout: 5 params (audit method, ablnZ/dlnp) ----
    zkeys = ["R_a", "L_a", "R_pl", "R_b", "L_b"]
    zc, zvals = {}, {k: [] for k in zkeys}
    z_colnorms = []
    for h in loads:
        fz = refnpz[f"z_{h}"][:, 0]
        zp = [P[h][k] for k in zkeys]
        for k in zkeys:
            zvals[k].append(P[h][k])
        cn, sv, cond = _jacobian(lambda p, fz=fz: fit_model.zmodel(fz, *p), zp)
        z_colnorms.append(cn)
        zc[h] = dict(cond=cond, rank_deficient=bool(sv[-1] < RANKDEF_REL * sv[0]),
                     colnorm_rel={zkeys[j]: float(cn[j] / (cn.max() + 1e-300)) for j in range(len(zkeys))})
    blocks["zout"] = dict(per_corner=zc, classify=_classify(zkeys, z_colnorms, zvals))

    # ---- PSRR: real bank (G,w) + complex section (pcb0,pcb1,pcw0,pcq) ----
    pkeys = ["G0", "G1", "w1", "G2", "w2", "G3", "w3", "pcb0", "pcb1", "pcw0", "pcq"]
    pc, pvals = {}, {k: [] for k in pkeys}
    p_colnorms = []
    for h in loads:
        fp = refnpz[f"p_{h}"][:, 0]
        zf = [P[h][k] for k in zkeys]
        pp = [P[h][k] for k in pkeys]
        for k in pkeys:
            pvals[k].append(P[h][k])

        def gp(pv, fp=fp, zf=zf):
            return fit_model.psrr_model(fp, *zf, list(pv[:7]), tuple(pv[7:11]))
        cn, sv, cond = _jacobian(gp, pp)
        p_colnorms.append(cn)
        pc[h] = dict(cond=cond, rank_deficient=bool(sv[-1] < RANKDEF_REL * sv[0]),
                     colnorm_rel={pkeys[j]: float(cn[j] / (cn.max() + 1e-300)) for j in range(len(pkeys))})
    blocks["psrr"] = dict(per_corner=pc, classify=_classify(pkeys, p_colnorms, pvals))

    # ---- Noise: white + MNOISE Lorentzian amplitudes (fixed poles) ----
    M = fit_model.MNOISE
    nkeys = ["gnw"] + [f"gn{k+1}" for k in range(M)]
    K = fit_model.KT4 * fit_model.NRk
    nfk = res.nfk
    nc, nvals = {}, {k: [] for k in nkeys}
    n_colnorms = []
    for h in loads:
        fn = refnpz[f"noise_{h}"][:, 0]
        npar = [P[h][k] for k in nkeys]
        for k in nkeys:
            nvals[k].append(P[h][k])

        def gn(pv, fn=fn):
            In2 = pv[0] ** 2 * K * np.ones_like(fn)
            for k in range(len(nfk)):
                In2 = In2 + pv[1 + k] ** 2 * K / (1.0 + (fn / nfk[k]) ** 2)
            return np.sqrt(In2) + 0j
        cn, sv, cond = _jacobian(gn, npar)
        n_colnorms.append(cn)
        nc[h] = dict(cond=cond, rank_deficient=bool(sv[-1] < RANKDEF_REL * sv[0]),
                     colnorm_rel={nkeys[j]: float(cn[j] / (cn.max() + 1e-300)) for j in range(len(nkeys))})
    blocks["noise"] = dict(per_corner=nc, classify=_classify(nkeys, n_colnorms, nvals))

    switches = {b: [k for k, d in blocks[b]["classify"].items() if d["cls"] == "SWITCH"]
                for b in blocks}
    # Round 2: fit_model._pexpr now clamps EVERY param to its corner envelope, so a switch
    # param can no longer overshoot off-corner -> it is CONTAINED (the harmful part -- nonsense
    # extrapolated values like R_pl->38.4 / pcw0->1.245e8 -- is gone). Whether the clamped
    # mid-value actually matches GT is the generalization question, measured by LOCO + off-grid.
    contained = bool(fit_model.CLAMP_M > 1.0 or fit_model.CLAMP_ADD > 0.0)
    ok = contained or not any(switches.values())
    return dict(blocks=blocks, switches=switches, contained=contained, pass_=ok)


# --------------------------------------------------------------------- runner
def run(vkey, do_offgrid=True, _print=True):
    """Fit the variant ONCE, run all three guardrails, write the JSON report.
    Returns the report dict (with overall pass flags). Callers own strict/exit
    handling (see main() and score.py --strict)."""
    refnpz = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
    res = fit_model.fit_variant(vkey)        # sets module state; single-DUT-per-pass
    L = loco(res, refnpz)
    I = identifiability(res, refnpz)
    O = offgrid(res, vkey) if do_offgrid else None

    rep = dict(variant=vkey, loads=list(res.loads), loco=L, identifiability=I, offgrid=O,
               passes=dict(loco=L["pass_"], identifiability=I["pass_"],
                           offgrid=(O["pass_"] if O else None)))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{vkey}.json").write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    if _print:
        _report(rep)
    return rep


def _fmt_cond(c):
    return "inf" if not np.isfinite(c) else (f"{c:.2e}" if c >= 1e4 else f"{c:.1f}")


def _report(rep):
    v = rep["variant"]
    print(f"\n{'='*92}\nCROSS-VALIDATION (out-of-sample guardrails)  variant='{v}'\n{'='*92}")

    # ---- LOCO ----
    L = rep["loco"]
    print("\n[1] LOCO  leave-one-load-corner-out  (in-sample -> held-out, dB; gate held<=2x in)")
    print(f"  {'held':>6} {'kind':>6} | {'Zout in':>8} {'Zout out':>9} {'':>3}"
          f" | {'PSRR in':>8} {'PSRR out':>9} {'':>3} | {'Nz in':>6} {'Nz out':>7} {'':>3}")
    for c in L["corners"]:
        def cell(d):
            return f"{d['insample']:8.2f} {d['heldout']:9.2f} {'ok' if d['ok'] else '**':>3}"
        print(f"  {c['corner']:>6} {c['kind']:>6} | {cell(c['zout'])}"
              f" | {cell(c['psrr'])} | {c['noise']['insample']:6.1f} {c['noise']['heldout']:7.1f}"
              f" {'ok' if c['noise']['ok'] else '**':>3}")
    print(f"  LOCO gate: {'PASS' if L['pass_'] else 'FAIL (overfit -> held-out > 2x in-sample)'}")
    print(f"  note: {L['note']}")

    # ---- identifiability ----
    I = rep["identifiability"]
    print("\n[2] IDENTIFIABILITY  cond(J) per block per corner + switch detection")
    for b in ("zout", "psrr", "noise"):
        blk = I["blocks"][b]
        conds = "  ".join(f"{h}:{_fmt_cond(d['cond'])}{'!' if d['rank_deficient'] else ''}"
                          for h, d in blk["per_corner"].items())
        print(f"  {b:>5}  cond(J): {conds}")
        flags = {k: d for k, d in blk["classify"].items() if d["cls"] != "OK"}
        for k, d in flags.items():
            vs = " ".join(f"{x:.3g}" for x in d["vals"])
            print(f"         {d['cls']:>9} {k:<5} ratio={d['ratio']:.1e}  vals=[{vs}]")
    sw = {b: s for b, s in I["switches"].items() if s}
    cont = I.get("contained")
    if sw:
        tag = "CONTAINED by envelope clamp" if cont else "UNCONTAINED (interpolated continuously)"
        print(f"  switch params ({tag}): {sw}")
    print(f"  identifiability gate: {'PASS' if I['pass_'] else 'FAIL: '+str(sw)}"
          + "   (switches clamped to envelope; generalization -> LOCO/off-grid;"
          + " ! = rank-deficient, cond~inf)")

    # ---- offgrid ----
    O = rep["offgrid"]
    if O:
        print("\n[3] OFF-GRID LOAD  GT vs emitted .lib (interpolated) in ngspice  (dB)")
        bw = O["insample_worst"]
        print(f"  in-sample worst corner: Zout={bw['zout']:.2f} PSRR={bw['psrr']:.2f} Nz={bw['noise']:.1f} dB")
        print(f"  {'iload':>9} | {'Zout':>6} {'':>3} | {'PSRR':>6} {'':>3} | {'Nz':>6} {'':>3}")
        for r in O["mids"]:
            print(f"  {r['iload_uA']:8.1f}u | {r['zout']['rms']:6.2f} {'ok' if r['zout']['ok'] else '**':>3}"
                  f" | {r['psrr']['rms']:6.2f} {'ok' if r['psrr']['ok'] else '**':>3}"
                  f" | {r['noise']['rms']:6.1f} {'ok' if r['noise']['ok'] else '**':>3}")
        print(f"  off-grid gate: {'PASS' if O['pass_'] else 'FAIL (off-grid > 2x in-sample)'}")

    p = rep["passes"]
    print(f"\n  >>> verdict: LOCO={'PASS' if p['loco'] else 'FAIL'}  "
          f"IDENTIFIABILITY={'PASS' if p['identifiability'] else 'FAIL'}  "
          f"OFFGRID={'PASS' if p['offgrid'] else ('FAIL' if p['offgrid'] is not None else 'skip')}")


def _matrix(reps):
    """Compact per-variant matrix for --all."""
    # LOCO 'interp' (middle corner, bracketed) = the clean overfit signal (a few-corner limit,
    # ~unchanged by clamping). off-grid = deployment-faithful interior interpolation (the metric
    # the clamp improves). EXTRAP end-corners are clamped-to-boundary (deployment-honest) and
    # live in the per-variant detail, not here (they conflate corner-spacing with overfit).
    cols = ["variant", "loco", "ident", "offgrid", "Z_interp", "P_interp",
            "offgrid_Z", "offgrid_P", "Zout_cond_worst", "switches(contained)"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in reps:
        if r is None:
            continue
        L, I, O = r["loco"], r["identifiability"], r["offgrid"]
        interp = [c for c in L["corners"] if c["kind"] == "interp"] or L["corners"]
        zi = max(c["zout"]["heldout"] for c in interp)
        pi = max(c["psrr"]["heldout"] for c in interp)
        zcond = max((d["cond"] for d in I["blocks"]["zout"]["per_corner"].values()), default=0)
        sw = [f"{b}:{','.join(s)}" for b, s in I["switches"].items() if s]
        ogz = max((m["zout"]["rms"] for m in O["mids"]), default=float("nan")) if O else float("nan")
        ogp = max((m["psrr"]["rms"] for m in O["mids"]), default=float("nan")) if O else float("nan")
        cells = [r["variant"],
                 "PASS" if L["pass_"] else "FAIL",
                 "PASS" if I["pass_"] else "FAIL",
                 ("PASS" if O["pass_"] else "FAIL") if O else "skip",
                 f"{zi:.2f}", f"{pi:.2f}", f"{ogz:.2f}", f"{ogp:.2f}",
                 _fmt_cond(zcond), (";".join(sw) if sw else "-")]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    ap.add_argument("--all", action="store_true", help="run the whole variant registry")
    ap.add_argument("--strict", action="store_true", help="exit nonzero if any gate FAILs")
    ap.add_argument("--no-offgrid", action="store_true", help="skip the ngspice off-grid sims")
    a = ap.parse_args()

    keys = list(variants.VARIANTS.keys()) if a.all else [a.variant]
    reps = []
    for k in keys:
        try:
            reps.append(run(k, do_offgrid=not a.no_offgrid))
        except Exception as e:                  # one bad variant must not kill the matrix
            print(f"!!! {k}: {type(e).__name__}: {e}")
            reps.append(None)

    if a.all:
        OUT.mkdir(parents=True, exist_ok=True)
        md = "# Cross-validation matrix (out-of-sample guardrails)\n\n" + _matrix(reps) + "\n"
        (OUT / "crossval_matrix.md").write_text(md, encoding="utf-8")
        print("\n" + md)
        print(f"wrote {OUT/'crossval_matrix.md'} + per-variant JSON")

    if a.strict:
        bad = any(r is None or not all(v for v in r["passes"].values() if v is not None)
                  for r in reps)
        raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
