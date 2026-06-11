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
                       Covers the noise block in BOTH topologies (norton gn*,
                       hybrid snw/sn*), the shared noise POLE positions
                       (greedy-fit artifacts -> INVISIBLE), and the spur
                       injection amplitudes (SWITCH guard on interpolation).
  4. structloco()      STRUCTURE-stability LOCO: re-run the whole structure-
                       selection pipeline (C_ft gate, Cout/ghost-cap, Zout
                       branch-B, PSRR shelf/SK/complex selector, noise
                       topology + adaptive bank, spur table) on each N-1
                       corner subset. Every one of those selectors is a hard
                       threshold evaluated in-sample; a decision that FLIPS
                       when one corner is dropped was sitting on its
                       threshold -> data-noise, not architecture.

General by construction: everything derives from ref["loads"] and RELATIVE
thresholds -- no 304MHz / 8-24MHz / 121uA / "3 corners" hardcoded.

    python crossval.py --variant base          # one DUT
    python crossval.py --all                    # whole registry -> matrix.md
    python crossval.py --variant base --strict  # exit nonzero on any gate FAIL
"""
import argparse
import contextlib
import io
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

# Per-param interpolation spec MIRRORING fit_model.emit()'s `specs`, restricted to
# the keys predict() consumes. (key, logspace). There is no public accessor; keep in
# sync if emit's spec list ever changes -- INCLUDING its conditional tails (gn{k}
# Norton Lorentzian gains; snw/sn{k} when the gated hybrid noise mode engaged),
# which _specs() mirrors from the fit_model module state of the last fit.
_ZP_SPECS = [("R_a", True), ("L_a", True), ("R_pl", True), ("R_b", True), ("L_b", True),
             ("G0", False), ("G1", False), ("w1", True), ("G2", False), ("w2", True),
             ("G3", False), ("w3", True),
             ("pcb0", False), ("pcb1", False), ("pcw0", True), ("pcq", True), ("gnw", True)]


def _specs():
    sp = _ZP_SPECS + [(f"gn{k+1}", True) for k in range(fit_model.MNOISE)]
    if getattr(fit_model, "NOISE_MODE", "norton") == "hybrid":
        sp = sp + [("snw", True)] + [(f"sn{k+1}", True)
                                     for k in range(len(fit_model.NFKV))]
    return sp


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
    nmode = getattr(res, "nmode", None)              # pin the FitResult's noise mode
    nfkv = getattr(res, "nfkv", None)                # (predict must not read globals)
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

        zi = _zrms(fz, fit_model.predict(P[h], fz, nfk, nfkv=nfkv, nmode=nmode)["Zout"], Zg)
        zo = _zrms(fz, fit_model.predict(interp, fz, nfk, nfkv=nfkv, nmode=nmode)["Zout"], Zg)
        pi = _prms(fp, fit_model.predict(P[h], fp, nfk, nfkv=nfkv, nmode=nmode)["PSRR"], Hg)
        po = _prms(fp, fit_model.predict(interp, fp, nfk, nfkv=nfkv, nmode=nmode)["PSRR"], Hg)
        ni = scoremod._noise_metrics(fn, Sg, fn, fit_model.predict(
            P[h], fn, nfk, nfkv=nfkv, nmode=nmode)["noise"])["psd_rms"]
        no = scoremod._noise_metrics(fn, Sg, fn, fit_model.predict(
            interp, fn, nfk, nfkv=nfkv, nmode=nmode)["noise"])["psd_rms"]

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
        base_z = max(base_z, _zrms(fz, fit_model.predict(
            P[il], fz, res.nfk, nfkv=getattr(res, "nfkv", None),
            nmode=getattr(res, "nmode", None))["Zout"], Zg))
        base_p = max(base_p, _prms(fp, fit_model.predict(
            P[il], fp, res.nfk, nfkv=getattr(res, "nfkv", None),
            nmode=getattr(res, "nmode", None))["PSRR"], Hg))
        base_n = max(base_n, scoremod._noise_metrics(
            fn, Sg, fn, fit_model.predict(
                P[il], fn, res.nfk, nfkv=getattr(res, "nfkv", None),
                nmode=getattr(res, "nmode", None))["noise"])["psd_rms"])

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


# ------------------------------------------------------- structure-stability LOCO
# The parameter-level LOCO above re-INTERPOLATES within a FIXED structure. But every
# structural decision in fit_model (Zout 2nd branch, PSRR shelf/SK/complex selection,
# Norton-vs-hybrid noise, adaptive bank size, C_ft gate, ghost-cap adjudication) is a
# hard threshold evaluated on the SAME data it then fits -- the methodology review's
# residual exposure. This check re-runs the WHOLE structure-selection pipeline on each
# N-1 corner subset (what a user who characterized one corner fewer would get) and
# flags decisions that FLIP: a selection that changes when one corner is dropped was
# sitting on its threshold, i.e. it is data-noise, not architecture.

_FM_STATE = ["ref", "LOADS", "NOMINAL", "VREF", "C", "RC", "CFT",
             "NFK", "MNOISE", "NOISE_MODE", "NFKV", "NSPUR_F", "NSPUR_PH"]


def _fm_save():
    return {k: getattr(fit_model, k) for k in _FM_STATE}


def _fm_restore(s):
    for k, v in s.items():
        setattr(fit_model, k, v)


def _structure(P, loads):
    """Snapshot of every data-driven STRUCTURAL decision of the last fit (module
    state + per-corner param flags). R_b=1e9 / _cpx=0 are the documented 'off'
    encodings (fit_zout / fit_psrr)."""
    return dict(
        cft_on=bool(fit_model.CFT > 0.0),
        nmode=str(fit_model.NOISE_MODE),
        nsec_noise=int(len(fit_model.NFK) if fit_model.NOISE_MODE == "norton"
                       else len(fit_model.NFKV)),
        nspur=int(len(fit_model.NSPUR_F)),
        cout=float(fit_model.C),
        branchB={il: bool(P[il]["R_b"] < 1e8) for il in loads},
        psrr_cpx={il: bool(P[il].get("_cpx", 0)) for il in loads},
    )


def structloco(vkey, res, full):
    """Re-run structure selection per leave-one-corner-out fold; compare each fold's
    decisions to the full fit's. FLIP (gate-fail): C_ft gate, noise topology, per-
    retained-corner Zout branch-B / PSRR-complex selection. WARN (report-only):
    noise-bank section count, spur count, Cout extraction ratio >3x (the nominal-
    held-out fold legitimately loses the *_hf sweep -> extraction-corner effect)."""
    loads = list(res.loads)
    state = _fm_save()
    folds = []
    try:
        for h in loads:
            retained = [il for il in loads if il != h]
            # mirror load() on the subset: a user with N-1 corners runs exactly this
            fit_model.ref = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz",
                                    allow_pickle=True)
            fit_model.LOADS = list(retained)
            fit_model.NOMINAL = (res.nominal if res.nominal in retained
                                 else retained[len(retained) // 2])
            fit_model.CFT = 0.0
            with contextlib.redirect_stdout(io.StringIO()):   # fold fits are chatty
                fit_model.fit_cft()
                fit_model.C, fit_model.RC = fit_model.fit_cout_esr()
                P2 = fit_model.fit_all()
            snap = _structure(P2, retained)
            flips, warns = [], []
            if snap["cft_on"] != full["cft_on"]:
                flips.append(f"C_ft gate {full['cft_on']}->{snap['cft_on']}")
            if snap["nmode"] != full["nmode"]:
                flips.append(f"noise mode {full['nmode']}->{snap['nmode']}")
            for il in retained:
                if snap["branchB"][il] != full["branchB"][il]:
                    flips.append(f"Zout branch-B@{il} {full['branchB'][il]}->{snap['branchB'][il]}")
                if snap["psrr_cpx"][il] != full["psrr_cpx"][il]:
                    flips.append(f"PSRR complex@{il} {full['psrr_cpx'][il]}->{snap['psrr_cpx'][il]}")
            if snap["nsec_noise"] != full["nsec_noise"]:
                warns.append(f"noise sections {full['nsec_noise']}->{snap['nsec_noise']}")
            if snap["nspur"] != full["nspur"]:
                warns.append(f"spur count {full['nspur']}->{snap['nspur']}")
            r = snap["cout"] / (full["cout"] + 1e-300)
            if r > 3.0 or r < 1.0 / 3.0:
                warns.append(f"Cout extraction x{r:.2g} (held={h}"
                             + (", nominal -> *_hf sweep lost" if h == res.nominal else "") + ")")
            folds.append(dict(held=h, nominal=fit_model.NOMINAL,
                              flips=flips, warns=warns, snap=snap))
    finally:
        _fm_restore(state)
    ok = not any(f["flips"] for f in folds)
    return dict(full=full, folds=folds, pass_=ok,
                note="each fold re-runs the FULL structure-selection pipeline (C_ft gate, "
                     "Cout/ghost-cap, branch-B, PSRR selector, noise topology/adaptation, "
                     "spur table) on N-1 corners; a FLIP means that decision sat on its "
                     "in-sample threshold (data-noise, not architecture).")


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

    # ---- Noise: amplitude params, dispatched on the fitted topology ----
    # norton: gnw + gn1..gnM Lorentzian-current amplitudes (poles nfk fixed)
    # hybrid: gnw (Norton white floor) + snw/sn1..snK series voltage-bank amplitudes
    # (round-6 review deferred item: ident used to be BLIND to the hybrid sn-keys).
    nmode = getattr(res, "nmode", "norton") or "norton"
    nfkv = list(getattr(res, "nfkv", []) or [])
    nfk = list(res.nfk or [])
    K = fit_model.KT4 * fit_model.NRk
    if nmode == "hybrid":
        nkeys = ["gnw", "snw"] + [f"sn{k+1}" for k in range(len(nfkv))]
    else:
        nkeys = ["gnw"] + [f"gn{k+1}" for k in range(len(nfk))]
    nc, nvals = {}, {k: [] for k in nkeys}
    n_colnorms = []
    for h in loads:
        fn = refnpz[f"noise_{h}"][:, 0]
        npar = [P[h][k] for k in nkeys]
        for k in nkeys:
            nvals[k].append(P[h][k])
        if nmode == "hybrid":
            zf = [P[h][k] for k in zkeys]
            Zc = fit_model.zmodel(fn, *zf)
            T2 = np.abs(Zc / fit_model._za(fn, zf[0], zf[1], zf[2])) ** 2
            Z2 = np.abs(Zc) ** 2

            def gn(pv, fn=fn, T2=T2, Z2=Z2):
                Vn2 = pv[1] ** 2 * K * np.ones_like(fn)
                for k in range(len(nfkv)):
                    Vn2 = Vn2 + pv[2 + k] ** 2 * K / (1.0 + (fn / nfkv[k]) ** 2)
                return np.sqrt(Vn2 * T2 + pv[0] ** 2 * K * Z2) + 0j
        else:
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

    # ---- Noise POLE positions (shared, load-independent -- selected by the greedy
    # adaptive fit on the same data): Jacobian of the STACKED all-corner Sv wrt each
    # log corner-freq, amplitudes held at their fitted values. An INVISIBLE pole
    # (relative column norm < UNIDENT_REL) moves nothing -> an overfit artifact of
    # the greedy insertion, safe but flagged. ----
    poles = nfkv if nmode == "hybrid" else nfk
    if poles:
        def gpole(pv):
            out = []
            for h in loads:
                fn = refnpz[f"noise_{h}"][:, 0]
                Zc = fit_model.zmodel(fn, *(P[h][k] for k in zkeys))
                out.append(fit_model.noise_model_sv(
                    P[h], fn, Zc,
                    nfk=(pv if nmode == "norton" else nfk),
                    nfkv=(pv if nmode == "hybrid" else nfkv), nmode=nmode))
            return np.concatenate(out) + 0j
        cn, sv, cond = _jacobian(gpole, list(poles))
        rel = {f"f{k+1}({poles[k]:.3g}Hz)": float(cn[k] / (cn.max() + 1e-300))
               for k in range(len(poles))}
        blocks["noise_poles"] = dict(
            cond=float(cond), colnorm_rel=rel,
            invisible=[k for k, v in rel.items() if v < UNIDENT_REL])

    # ---- Spur injection amplitudes sa_k = vout_amp/|Zout(f_k)|: linear in the data
    # (identifiable by construction), but interpolated quad-in-ln(iload) through
    # _pexpr like everything else -> the SWITCH check guards that interpolation. ----
    spur_f = list(getattr(res, "spur_f", []) or [])
    if spur_f:
        scl = {}
        for k in range(len(spur_f)):
            key = f"sa{k+1}"
            vals = np.abs(np.array([P[h][key] for h in loads], float))
            ratio = float(vals.max() / (vals.min() + 1e-300))
            scl[key] = dict(cls=("SWITCH" if ratio > SWITCH_RATIO else "OK"),
                            ratio=ratio, vals=[float(x) for x in vals])
        blocks["spur"] = dict(classify=scl)

    switches = {b: [k for k, d in blocks[b].get("classify", {}).items() if d["cls"] == "SWITCH"]
                for b in blocks}
    # Round 2: fit_model._pexpr now clamps EVERY param to its corner envelope, so a switch
    # param can no longer overshoot off-corner -> it is CONTAINED (the harmful part -- nonsense
    # extrapolated values like R_pl->38.4 / pcw0->1.245e8 -- is gone). Whether the clamped
    # mid-value actually matches GT is the generalization question, measured by LOCO + off-grid.
    contained = bool(fit_model.CLAMP_M > 1.0 or fit_model.CLAMP_ADD > 0.0)
    ok = contained or not any(switches.values())
    return dict(blocks=blocks, switches=switches, contained=contained, pass_=ok)


# --------------------------------------------------------------------- runner
def run(vkey, do_offgrid=True, do_struct=True, _print=True):
    """Fit the variant ONCE, run all four guardrails, write the JSON report.
    Returns the report dict (with overall pass flags). Callers own strict/exit
    handling (see main() and score.py --strict). structloco runs LAST (its folds
    clobber+restore fit_model module state; offgrid's emit() must see the full fit)."""
    refnpz = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
    res = fit_model.fit_variant(vkey)        # sets module state; single-DUT-per-pass
    full_snap = _structure(res.P, res.loads)   # capture BEFORE anything re-fits
    L = loco(res, refnpz)
    I = identifiability(res, refnpz)
    O = offgrid(res, vkey) if do_offgrid else None
    S = structloco(vkey, res, full_snap) if do_struct else None

    rep = dict(variant=vkey, loads=list(res.loads), loco=L, identifiability=I, offgrid=O,
               structure=S,
               passes=dict(loco=L["pass_"], identifiability=I["pass_"],
                           offgrid=(O["pass_"] if O else None),
                           structure=(S["pass_"] if S else None)))
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

    # ---- noise poles / spur (informational sub-blocks) ----
    npb = rep["identifiability"]["blocks"].get("noise_poles")
    if npb:
        inv = npb["invisible"]
        print(f"  noise-pole ident: cond={_fmt_cond(npb['cond'])}"
              + (f"  INVISIBLE poles (greedy-fit artifacts): {inv}" if inv else "  all poles live"))
    spb = rep["identifiability"]["blocks"].get("spur")
    if spb:
        sflags = {k: d for k, d in spb["classify"].items() if d["cls"] != "OK"}
        for k, d in sflags.items():
            vs = " ".join(f"{x:.3g}" for x in d["vals"])
            print(f"   spur {d['cls']:>9} {k:<5} ratio={d['ratio']:.1e}  vals=[{vs}]")

    # ---- structure-stability LOCO ----
    S = rep.get("structure")
    if S:
        print("\n[4] STRUCTURE-STABILITY LOCO  (re-run structure selection on each N-1 subset)")
        for fo in S["folds"]:
            stat = ("FLIP: " + "; ".join(fo["flips"])) if fo["flips"] else "stable"
            wtxt = ("  warn: " + "; ".join(fo["warns"])) if fo["warns"] else ""
            print(f"  held {fo['held']:>6} (nom={fo['nominal']}): {stat}{wtxt}")
        print(f"  structure gate: {'PASS' if S['pass_'] else 'FAIL (a selection sits on its threshold)'}")

    p = rep["passes"]
    print(f"\n  >>> verdict: LOCO={'PASS' if p['loco'] else 'FAIL'}  "
          f"IDENTIFIABILITY={'PASS' if p['identifiability'] else 'FAIL'}  "
          f"OFFGRID={'PASS' if p['offgrid'] else ('FAIL' if p['offgrid'] is not None else 'skip')}  "
          f"STRUCTURE={'PASS' if p['structure'] else ('FAIL' if p['structure'] is not None else 'skip')}")


def _matrix(reps):
    """Compact per-variant matrix for --all."""
    # LOCO 'interp' (middle corner, bracketed) = the clean overfit signal (a few-corner limit,
    # ~unchanged by clamping). off-grid = deployment-faithful interior interpolation (the metric
    # the clamp improves). EXTRAP end-corners are clamped-to-boundary (deployment-honest) and
    # live in the per-variant detail, not here (they conflate corner-spacing with overfit).
    cols = ["variant", "loco", "ident", "offgrid", "struct", "Z_interp", "P_interp",
            "offgrid_Z", "offgrid_P", "Zout_cond_worst", "switches(contained)"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in reps:
        if r is None:
            continue
        L, I, O = r["loco"], r["identifiability"], r["offgrid"]
        S = r.get("structure")
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
                 ("PASS" if S["pass_"] else "FAIL") if S else "skip",
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
    ap.add_argument("--no-struct", action="store_true",
                    help="skip the structure-stability LOCO (N-1 re-fits; ~3x fit cost)")
    a = ap.parse_args()

    keys = list(variants.VARIANTS.keys()) if a.all else [a.variant]
    reps = []
    for k in keys:
        try:
            reps.append(run(k, do_offgrid=not a.no_offgrid, do_struct=not a.no_struct))
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
