"""P5 -- multi-port fit + report for the in-situ extraction (Mechanism A).

A real PMU exposes SEVERAL modeled ports (two voltage-output LDOs that share a VREF/bias,
plus current-sink outputs). fit_model.py models ONE voltage output (Zout + one PSRR +
noise). This module GENERALIZES to multi-port by REUSING fit_model's proven per-output
fitters -- it does NOT reimplement them:

  voltage output  o : fit_model.fit_cout_esr / fit_zout / fit_psrr (x each supply) /
                      fit_noise_bank   -- the identical building blocks, in a loop.
  current sink    c : a small NEW fit -- admittance Y(s)=g0+sC and current-PSRR pi(s)
                      (low order; a sink is a near-ideal conductance + parasitic cap).

Why the building blocks and not fit_variant(): fit_variant -> fit_all needs a DC
load-regulation sweep + current-labeled load corners (ng.amps), which an in-situ
small-signal extraction does not carry. The building blocks are pure (arrays in, params
out), so we drive them directly over an npz-like per-output VIEW, saving/restoring
fit_model's module globals around each output so outputs never cross-contaminate
(PLL Cout=1n vs VCO Cout=4.7n live in C/RC -> set per output).

The report breaks out CURRENT-port error SEPARATELY from voltage-port error (a debug
requirement: a current-sink model that is off must be visible, not averaged away).

    python -m harness.fit_multiport --variant pmu_standin_ade --manifest pmu_top
    # or:  python harness/fit_multiport.py --variant pmu_standin --manifest pmu_top
"""
import contextlib
import io
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_model as FM          # the per-output fitters we reuse  # noqa: E402


class _NpzLike(dict):
    """A dict that also answers `k in obj.files` (fit_model probes ref.files)."""
    @property
    def files(self):
        return list(self.keys())


@contextlib.contextmanager
def _fm_globals():
    """Save/restore the fit_model module globals we mutate, so each output (and the
    caller's process) is isolated."""
    keys = ("ref", "LOADS", "NOMINAL", "C", "RC", "CFT", "VREF", "NFK", "MNOISE",
            "NOISE_MODE", "NFKV", "NSPUR_F", "NSPUR_PH")
    saved = {k: getattr(FM, k) for k in keys}
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(FM, k, v)


# ----------------------------------------------------------------- voltage outputs
def _iload_map(ref, o, loads):
    """Per-LABEL REAL rail current for v_out o, read off the multi-port ref's
    meta_iload_<o> array (positionally aligned with ref['loads']). Returns
    {label: amps} for every label whose meta current is FINITE. Absent meta key /
    NaN entry -> the label is simply not in the map (caller falls back to a numeric
    label parse, then 0.0). Single-OP / legacy npz (no meta_iload_<o>) -> {}."""
    key = f"meta_iload_{o}"
    if ref is None or key not in (ref if isinstance(ref, dict) else getattr(ref, "files", [])):
        return {}
    cur = np.asarray(ref[key], dtype=float).ravel()
    reflabels = [str(x) for x in np.asarray(ref["loads"]).ravel()]
    out = {}
    for lbl, a in zip(reflabels, cur):
        if np.isfinite(a):
            out[str(lbl)] = float(a)
    # keep only labels this view actually carries (defensive; loads is the view set)
    return {k: v for k, v in out.items() if k in set(str(x) for x in loads)} or out


def _fit_voltage_output(o, view, supplies, vout_dc=0.8, iload_map=None):
    """Fit one voltage output from its single-port view (split_ports output).
    view = {"npz": {z_<il>,p_<il>,noise_<il>,loads,meta_*}, "supplies": {s:{il:arr}}, ...}.
    Returns dict(P={il:params}, nfk, cout, esr, err=[per-corner per-metric], supplies=[...],
    schedule_loads=[labels carrying a real, finite iv], cft=<gated feedthrough cap, 0.0 off>,
    vreg_sched=<{currents,vregs,i_nom} DC load-reg from transient, or None>).

    `iload_map` {label: amps} carries the rail's REAL per-label current (from the npz
    meta_iload_<o>); when given it is the AUTHORITATIVE abscissa for the emit-side
    ln(iload) schedule. When absent (single-OP / legacy numeric npz) the iv falls back
    to a numeric label parse, then 0.0 -- byte-identical to the pre-stage-2b behavior."""
    sp = view["npz"]
    iload_map = iload_map or {}
    # the voltage fit operates ONLY on labels that carry a real AC Zout array (z_<il>).
    # The once-per-temp cells (DC/iv/tran at the OP) appear in ref['loads'] but carry no
    # z/p/noise for this rail -> they are NOT voltage-fit corners. Single-OP/legacy npz:
    # every load carries z_<il> -> loads == view['loads'] (unchanged).
    loads = [il for il in view["loads"] if f"z_{il}" in sp]
    nom = loads[len(loads) // 2]
    with _fm_globals():
        FM.ref = _NpzLike(sp)
        FM.LOADS = list(loads)
        FM.NOMINAL = nom
        FM.CFT = 0.0
        FM.fit_cft()                       # gate (stays silent on the stand-in)
        # CAPTURE the gated vin->vout feedthrough cap BEFORE _fm_globals() restores the
        # module global on block exit. fit_cft() writes FM.CFT as a side effect (0.0 when its
        # 5 gates fail); the single-port emit_va consumed it, but the PMU emit path used to
        # DROP it. Thread it out so emit_pmu_va can re-emit the I(vin,vout) feedthrough.
        # CFT==0 (gate off / single-OP stand-in) -> emit stays byte-identical.
        cft = float(FM.CFT)
        FM.C, FM.RC = FM.fit_cout_esr()    # this output's physical Cout/ESR
        cout, esr = FM.C, FM.RC
        zfits, P, err = {}, {}, []
        for il in loads:
            gz = sp[f"z_{il}"]; fz = gz[:, 0]; Z = gz[:, 1] + 1j * gz[:, 2]
            R_a, L_a, R_pl, R_b, L_b = FM.fit_zout(fz, Z)
            zfits[il] = (R_a, L_a, R_pl, R_b, L_b)
            # iv = the rail's REAL current at this label. Priority: meta_iload_<o>[label]
            # (the in-situ truth, set by the sweep) -> a parseable numeric label (legacy
            # ng.amps npz) -> 0.0 (open / unparseable -> no real load, no scheduling).
            if il in iload_map:
                iv = float(iload_map[il])
            elif _amps_ok(il):
                iv = FM._amps(il)
            else:
                iv = 0.0
            P[il] = dict(iv=iv, R_a=R_a, L_a=L_a, R_pl=R_pl, R_b=R_b, L_b=L_b,
                         vreg=vout_dc + R_a * iv)
            # PSRR per supply -- the primary supply's params live on P[il]; all supplies'
            # fits are kept in per-supply dicts for the report.
            psrr_params = {}
            for s in supplies:
                gp = view["supplies"][s][il]
                fp = gp[:, 0]; H = gp[:, 1] + 1j * gp[:, 2]
                G, Q = FM.fit_psrr(fp, H, R_a, L_a, R_pl, R_b, L_b)
                psrr_params[s] = (G, Q)
            prim = view.get("primary_supply") or supplies[0]
            G, Q = psrr_params[prim]
            P[il].update(G0=G[0], G1=G[1], w1=G[2], G2=G[3], w2=G[4], G3=G[5], w3=G[6],
                         pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3], _psrr=psrr_params)
        # joint Norton-@vout noise bank over corners (reads FM.ref noise_<il>, FM.C/RC)
        NB = FM.fit_noise_bank(zfits)
        # GATED HYBRID (series voltage-noise) keep-best -- mirror fit_all's gated attempt so the
        # multi-port SCORE/REPORT matches the emitted single-port VA (which engages hybrid via
        # fit_all). A real LOOP-SHAPED rail has In=Sv/|Zout| falling steeper than the Norton bank
        # can hold (the WuR pll/vco: Norton ~11dB, hybrid ~0.5dB). Engage ONLY when Norton STALLED
        # above the trigger AND the hybrid is a clear (>0.5dB) win (keep-best -> zero regression;
        # every synthetic ref fits <=3.7dB Norton -> never fires there -> byte-identical).
        nmode, nfkv, NH = "norton", [], None
        if NB["worst"] > FM.NOISE_ADAPT_TRIG:
            NH = FM.fit_noise_hybrid(zfits)
            if NH["worst"] < NB["worst"] - 0.5:
                nmode, nfkv = "hybrid", list(NH["fkv"])
        nfk = list(NB["fk"]) if nmode == "norton" else []
        for il in loads:
            if nmode == "hybrid":
                P[il]["gnw"] = NH["gw"][il]
                P[il]["snw"] = NH["snw"][il]
                for k in range(len(nfkv)):
                    P[il][f"sn{k+1}"] = NH["snk"][il][k]
            else:
                P[il]["gnw"] = NB["gw"][il]
                for k in range(len(nfk)):
                    P[il][f"gn{k+1}"] = NB["gk"][il][k]
        # per-corner per-metric error (model vs GT), using the SAME transfer fns as the fit
        for il in loads:
            e = dict(il=il)
            gz = sp[f"z_{il}"]; fz = gz[:, 0]; Zg = gz[:, 1] + 1j * gz[:, 2]
            Zm = FM.zmodel(fz, P[il]["R_a"], P[il]["L_a"], P[il]["R_pl"],
                           P[il]["R_b"], P[il]["L_b"])
            e["zrms"] = _rms_db(Zm, Zg)
            e["psrr"] = {}
            for s in supplies:
                gp = view["supplies"][s][il]; fp = gp[:, 0]; Hg = gp[:, 1] + 1j * gp[:, 2]
                G, Q = P[il]["_psrr"][s]
                Hm = FM.psrr_model(fp, P[il]["R_a"], P[il]["L_a"], P[il]["R_pl"],
                                   P[il]["R_b"], P[il]["L_b"], G, Q)
                sel = fp >= 1e3
                e["psrr"][s] = (_rms_db(Hm, Hg),
                                float(np.degrees(np.sqrt(np.mean(
                                    np.angle(Hm[sel] / Hg[sel]) ** 2)))))
            gn = sp[f"noise_{il}"]; fn = gn[:, 0]; Sg = gn[:, 1]
            Sm = FM.noise_model_sv(P[il], fn, FM.zmodel(fn, *zfits[il]),
                                   nfk=nfk, nfkv=nfkv, nmode=nmode)
            e["nrms"] = float(np.sqrt(np.mean(
                (20 * np.log10((Sm + 1e-30) / (Sg + 1e-30))) ** 2)))
            err.append(e)
    # schedule_loads: the labels whose iv is REAL + finite + nonzero -- the abscissa set
    # the emit-side ln(iload) parameter schedule fits over (NaN/0 labels are NOT scheduled,
    # per the contract). Single-OP -> one label or empty (baked literal downstream).
    schedule_loads = [il for il in loads
                      if np.isfinite(P[il]["iv"]) and P[il]["iv"] != 0.0]
    # DC load-regulation schedule derived from the rail's TRANSIENT load steps (the AC sweep
    # never carries DC load-reg). view['tr_steps'] = {tr_<label>: (i_from, i_to)} from the
    # manifest step decl (split_ports), so the load currents come from the SOURCE OF TRUTH --
    # not string-parsed from the opaque key. None when no transient -> emit byte-identical.
    tr_steps = view.get("tr_steps") or {}
    n_tr_npz = sum(1 for k in sp if isinstance(k, str) and k.startswith("tr_"))  # waveforms present
    vreg_sched = _build_vreg_schedule(sp, tr_steps, P, nom)
    # branch-A regulation slew-rate limit SRa (the LARGE-SIGNAL load-transient undershoot the AC
    # Zout cannot carry) -- AUTO-FIT from the SAME coverage.transient steps the vreg schedule uses,
    # so SRa is a fit product like Zout/PSRR/noise, not a hand-filled constant. `_fit_slew_a` is
    # hardened to REJECT switching-rippled / under-sampled steps (returns None) rather than emit a
    # noise-located SRa; on a clean characterization step it recovers dI/t_bottom. None (no clean
    # undershoot, or no transient) -> the rail emits byte-identical. The manifest slew_a, when set,
    # OVERRIDES this at the fit_multiport call site -- the escape hatch for DUTs whose transient is
    # GHz-contaminated (the real WuR system TB) where the auto-fit must not be trusted.
    slew_a = _fit_slew_a(sp, tr_steps)
    # when transients ARE mapped but the schedule still didn't build, capture WHY (per-waveform
    # settled-extraction dump) so the fit log explains it without another box round-trip.
    loadreg_diag = (_loadreg_diag(sp, tr_steps)
                    if (vreg_sched is None and len(tr_steps) >= 2) else None)
    return dict(P=P, nfk=nfk, nmode=nmode, nfkv=nfkv, cout=cout, esr=esr, err=err,
                supplies=list(supplies), schedule_loads=schedule_loads, cft=cft,
                vreg_sched=vreg_sched, slew_a=slew_a, n_tr_npz=n_tr_npz, n_tr_mapped=len(tr_steps),
                loadreg_diag=loadreg_diag)


def _settled_step(w):
    """(pre-step, post-step) settled output means from a load-step [t,V] waveform, or None
    when the step can't be located cleanly. The step edge = the largest |dV| sample searched
    ONLY in [15%,98%] of the time span, so a t~0 TURN-ON transient (the startup drop the user
    sees -- its dV can DWARF the load step and hijack a whole-waveform argmax, collapsing the pre
    window to nothing) does not steal the edge. The pre window is a settled slab just BEFORE the
    edge AND clear of the turn-on; the post window the settled tail. Robust to step direction."""
    w = np.asarray(w, dtype=float)
    if w.ndim != 2 or w.shape[0] < 8 or w.shape[1] < 2:
        return None
    t, V = w[:, 0], w[:, 1]
    if not (np.all(np.isfinite(t)) and np.all(np.isfinite(V))):
        return None
    t0, tend = t[0], t[-1]
    span = tend - t0
    if span <= 0:
        return None
    dV = np.abs(np.diff(V))                                   # edge between sample i and i+1
    win = (t[:-1] >= t0 + 0.15 * span) & (t[:-1] <= t0 + 0.98 * span)  # skip turn-on + far tail
    idx = int(np.argmax(np.where(win, dV, -1.0))) if win.any() else int(np.argmax(dV))
    te = t[idx]
    if not (tend > te > t0):
        return None
    pre = (t >= max(te - 0.40 * (te - t0), t0 + 0.15 * span)) & (t < te - 0.02 * (te - t0))
    post = t >= tend - 0.20 * (tend - te)
    if pre.sum() < 3 or post.sum() < 3:
        return None
    return float(V[pre].mean()), float(V[post].mean())


def _loadreg_diag(sp, tr_steps):
    """Per-transient shape dump for the FAILURE case (transients mapped but the vreg schedule
    still did not build): for each waveform, the located settled pre/post pair, or -- when it
    couldn't -- the edge position + V at start/mid/end, so a startup-dominated or step-at-t0
    waveform is visible WITHOUT a box round-trip. Returns a list of one-line strings."""
    out = []
    for k, fromto in sorted((tr_steps or {}).items()):
        if k not in sp:
            out.append(f"{k}: key absent in view"); continue
        w = np.asarray(sp[k], float)
        if w.ndim != 2 or w.shape[0] < 8 or w.shape[1] < 2:
            out.append(f"{k}: not a [t,V] array (shape {w.shape})"); continue
        t, V = w[:, 0], w[:, 1]
        span = t[-1] - t[0]
        ei = int(np.argmax(np.abs(np.diff(V)))) if V.size > 1 else 0
        ef = ((t[ei] - t[0]) / span) if span > 0 else float("nan")
        s = _settled_step(sp[k])
        try:
            frm, to = float(fromto[0]), float(fromto[1])
        except Exception:                                   # noqa: BLE001
            frm = to = float("nan")
        if s is None:
            # dump the SHAPE: V at 9 evenly-spaced time fractions + the min and where it sits, so a
            # load STEP (settles at a new level) vs a load PULSE (dips then RETURNS to the start)
            # vs a dropout collapse is distinguishable from the paste alone.
            ix = np.linspace(0, len(V) - 1, 9).round().astype(int)
            ser = " ".join(f"{V[i]:.3f}" for i in ix)
            imn = int(np.argmin(V)); imx = int(np.argmax(V))
            extreme = imn if abs(V[imn] - V[0]) >= abs(V[imx] - V[0]) else imx
            ef2 = ((t[extreme] - t[0]) / span) if span > 0 else float("nan")
            out.append(f"{k} ({frm:g}->{to:g}A): NO settled pair -- V@[0..100%]= {ser} ; "
                       f"extreme {V[extreme]:.4f}V @{ef2:.0%} (start {V[0]:.4f}, end {V[-1]:.4f})")
        else:
            out.append(f"{k} ({frm:g}->{to:g}A): pre={s[0]:.5f}V post={s[1]:.5f}V")
    return out


def _loadreg_from_transient(sp, tr_steps):
    """Recover {iload: settled Vout} DC load-regulation points from a rail's transient load
    steps. `tr_steps` maps each transient view-key -> its (i_from, i_to) load currents, taken
    from the manifest's coverage.transient step DECLARATION (the source of truth). The currents
    are NOT parsed out of the key string: the key/label is opaque (the real manifest uses custom
    labels like "2m"/"3m" -> key tr_2m, which carries NO numeric currents and NO load suffix),
    so string-parsing silently dropped every real step. Each step yields TWO settled-DC points:
    pre-step tail = Vout@from, post-step tail = Vout@to. Duplicate iloads averaged. Empty when
    no transient is present/declared."""
    by_i = {}
    for k, fromto in (tr_steps or {}).items():
        if k not in sp:
            continue
        try:
            i_from, i_to = float(fromto[0]), float(fromto[1])
        except (TypeError, ValueError, IndexError):           # malformed step decl
            continue
        s = _settled_step(sp[k])
        if s is None:
            continue
        for iv, vo in ((i_from, s[0]), (i_to, s[1])):
            if np.isfinite(iv) and iv > 0 and np.isfinite(vo):
                by_i.setdefault(iv, []).append(vo)
    return {iv: float(np.mean(vs)) for iv, vs in by_i.items()}


def _build_vreg_schedule(sp, tr_steps, P, nom):
    """Derive a vreg(iload) DC load-regulation schedule from the rail's transient steps.

    The model's DC output is Vout = vreg - R_a*iload (branch-A R_a is the ONLY DC path; B is
    open, C is a cap). To reproduce the MEASURED settled Vout(iload) we set
        vreg(iload) = Vout_settled(iload) + R_a*iload
    and schedule vreg vs ln(iload). This corrects BOTH error sources in one shot: the
    load-DEPENDENT droop (multiple loads) AND the fixed-target steady-state offset -- the AC
    fit bakes vreg from the 0.8 TARGET (fit_multiport vout_dc default), not the real output.

    Returns dict(currents, vregs, i_nom) or None (no transient / <2 distinct loads). i_nom =
    the AC operating load so the default instance sits at the characterized OP."""
    pts = _loadreg_from_transient(sp, tr_steps)
    if len(pts) < 2:
        return None
    R_a = float(P[nom]["R_a"])
    currents = sorted(pts.keys())
    if len(currents) < 2:
        return None
    vregs = [pts[i] + R_a * i for i in currents]
    iv_nom = float(P[nom]["iv"])
    i_nom = iv_nom if (np.isfinite(iv_nom) and iv_nom > 0) else currents[len(currents) // 2]
    return dict(currents=currents, vregs=vregs, i_nom=float(i_nom))


def _movavg(y, w):
    """Edge-padded box smoother (replicates the end values so there is no zero-pull artifact at the
    boundaries), length-preserving. Used to extract the dip ENVELOPE from a switching-rippled
    transient so the bottom can be timed robustly."""
    y = np.asarray(y, float)
    w = max(1, int(w))
    if w <= 1 or y.size < w:
        return y
    pad = w // 2
    yp = np.pad(y, pad, mode="edge")
    return np.convolve(yp, np.ones(w) / w, mode="valid")[: y.size]


# physical branch-A slew-rate band [A/s]: a real LDO pass device ramps its current at ~1e3..1e8 A/s.
# A "fit" outside this band is a sampling / aliasing artifact, not a slew measurement -> reject.
_SRA_LO, _SRA_HI = 1.0e3, 1.0e8


_RECOV_KEYS = ("Lreg", "Rreg", "Cs", "Rs")
_RECOV_OPT_KEYS = ("Imax", "Vcl", "Gcl")     # optional anti-windup overrides (emit has defaults)


def _fit_recovery(vmf):
    """Recovery (overdamped 2nd-order Zout) param dict for ONE rail from its manifest entry, or
    None when not opted in. Opt-in + manifest-driven (NO auto-fit yet -- see below): the rail's
    `recovery` field must carry ALL of Lreg, Rreg, Cs, Rs as finite, strictly-positive numbers.
    Any missing / non-positive / non-numeric value -> None -> the rail emits byte-identical.

    The recovery network reshapes the post-dip climb the in-situ LTI Zout + slew front-end gets
    wrong: a slow recovery inductor (Lreg||Rreg, lossless at DC -> DC setpoint unmoved) stretches
    the Cout-recharge into a monotonic ~100ns overdamped climb, and a DC-blocked Rs-Cs snubber
    damps the slew-induced overshoot. The winning topology + validated PLL-rail params are in
    cadence/wur_real_tb/ldo_pll_compensated.va (RMS 2.47mV vs real silicon).

    WHY STILL MANIFEST-DRIVEN (not yet auto-fit): the four params are an overdamped-shape fit that
    needs a CLEAN single load-step characterization waveform. Until that is isolated this stays a
    hand-tuned knob (same escape-hatch discipline as slew_a).

    B4 RECIPE (proven, ready to productionize -- HANDOFF_RECOVERY_STDFLOW.md, scratchpad/
    autofit_acconsistent.py): fit {SRa, Lreg, Rreg, Cs, Rs} by replaying the DECAP'D coverage.transient
    step (the coverage.cdecap feature) through the model and minimizing vs the box target, with TWO
    GUARDS that keep the fit PHYSICAL and make it GENERALIZE to real_V (held-out):
      (1) La PINNED to the AC-Zout value (NOT free) -- the deep dip is large-signal (SS!=LS); letting
          La float makes it AC-inconsistent (La120 -> Zout peak 558 vs measured ~160).
      (2) Cs BOUNDED <= the external decap scale (a few pF) -- else the snubber Cs floats to ~1nF as a
          FAKE OUTPUT DECAP (the 35.5mV-held-out failure); bounded, Cs converges to ~1pF and real_V
          lands ~5.79mV with physical params. fit_multiport is Pure-Python today -> this needs a
          Spectre-in-loop replay (or an analytic dip-depth->SRa / recovery-tau->Lreg fit).

    Optional anti-windup overrides (Imax/Vcl/Gcl) pass through when present + valid; absent ->
    the emit uses its built-in defaults (the clamps bound the loop's large-signal response without
    touching the in-envelope replay/DC/AC -- see emit_pmu_model._recov_param)."""
    recov = vmf.get("recovery") if isinstance(vmf, dict) else None
    if not isinstance(recov, dict):
        return None
    out = {}
    for k in _RECOV_KEYS:
        try:
            v = float(recov.get(k))
        except (TypeError, ValueError):
            return None
        if not (v > 0 and v < 1e30):
            return None
        out[k] = v
    for k in _RECOV_OPT_KEYS:        # optional; only carried when finite + strictly positive
        if k in recov:
            try:
                v = float(recov.get(k))
            except (TypeError, ValueError):
                continue
            if v > 0 and v < 1e30:
                out[k] = v
    return out


def _fit_slew_a(sp, tr_steps):
    """Branch-A regulation SLEW-RATE limit SRa [A/s] from the rail's transient load steps -- the
    LARGE-SIGNAL dynamic the AC Zout/PSRR fundamentally cannot carry (proven: same load + same cap
    + same small-signal Zout, but the real LDO undershoots ~3x deeper than a pure-LTI model; a
    finite slew reproduces it, SR->inf collapses it). On a load step of dI, the pass-device
    regulation current can only ramp at SRa, so the output UNDERSHOOTS -- dips BELOW its settled
    post-step level -- until the current catches the load. The dip BOTTOM sits ~dI/SRa after the
    edge, CAP-INDEPENDENTLY (the test cap sets the dip DEPTH, not its TIME), so
        SRa = dI / t_bottom ,  t_bottom = t(dip extremum) - t(step edge)
    Multi-step (the manifest declares 2m/3m/4m): each qualifying step gives one SRa estimate and
    the MEDIAN is returned, so one bad step cannot swing it (and the median assumes SRa is ~load-
    independent across the steps -- the branch-A single-rate model; wide scatter would show as a
    skewed median, not a crash).

    Gated like Cft/d2/vreg_sched: a step contributes ONLY when it shows a CLEAN undershoot, and the
    whole rail emits byte-identical when none do. CLEAN here is hardened against the real-TB failure
    mode (memory real-tb-model-vs-real): an under-sampled / switching-rippled transient has a dip
    region that is OSCILLATORY, so the raw argmin lands on a noise trough and t_bottom (hence SRa)
    reads ~10x wrong. A step is therefore rejected unless
      (1) the dip is clearly past the settled-tail ripple (>=2mV and >4x tail std),
      (2) the high-frequency content not captured by the smoothed envelope is a SMALL fraction of
          the dip depth (switching ripple ~ dip depth -> the bottom is noise-located -> reject),
      (3) the raw bottom and the smoothed-envelope bottom agree in time (bottom robust to smoothing),
      (4) the resulting SRa lands in the physical band [1e3,1e8] A/s.
    What this CANNOT catch is a SLOW alias that mimics real envelope structure -- that is a Nyquist
    limit of the data, not fixable in the fit; for such DUTs the manifest slew_a override is the
    escape hatch. Returns the median SRa over the qualifying steps, or None."""
    srs = []
    for k, fromto in (tr_steps or {}).items():
        if k not in sp:
            continue
        w = np.asarray(sp[k], float)
        if w.ndim != 2 or w.shape[0] < 12 or w.shape[1] < 2:
            continue
        t, V = w[:, 0], w[:, 1]
        if not (np.all(np.isfinite(t)) and np.all(np.isfinite(V))):
            continue
        try:
            i_from, i_to = float(fromto[0]), float(fromto[1])
        except (TypeError, ValueError, IndexError):
            continue
        dI = abs(i_to - i_from)
        t0, tend = t[0], t[-1]
        span = tend - t0
        if not (np.isfinite(dI) and dI > 0 and span > 0):
            continue
        # step edge = largest |dV|, skipping the t~0 turn-on and the far settled tail
        dV = np.abs(np.diff(V))
        win = (t[:-1] >= t0 + 0.15 * span) & (t[:-1] <= t0 + 0.85 * span)
        if not win.any():
            continue
        te = t[int(np.argmax(np.where(win, dV, -1.0)))]
        post = t >= tend - 0.20 * span                       # settled tail (dip recovered)
        seg = (t > te) & (t < tend - 0.20 * span)            # where the undershoot lives
        if post.sum() < 3 or seg.sum() < 5:                  # >=5 dip samples to judge smoothness
            continue
        v_settled = float(V[post].mean())
        ripple = float(np.std(V[post]))
        load_up = i_to > i_from                              # a load INCREASE dips the output DOWN
        Vseg, tseg = V[seg], t[seg]
        j = int(np.argmin(Vseg)) if load_up else int(np.argmax(Vseg))
        v_ext, t_ext = float(Vseg[j]), float(tseg[j])
        depth = (v_settled - v_ext) if load_up else (v_ext - v_settled)
        # (1) a REAL undershoot (dip clearly past tail ripple, >=2mV) resolved in time after the edge
        if not (depth > max(2e-3, 4.0 * ripple) and t_ext > te):
            continue
        # (2)+(3) anti-aliasing: extract the dip ENVELOPE and reject when the bottom is noise-located
        Vsm = _movavg(Vseg, max(3, Vseg.size // 8))
        if float(np.std(Vseg - Vsm)) > 0.30 * depth:         # high-freq ripple ~ dip depth -> reject
            continue
        js = int(np.argmin(Vsm)) if load_up else int(np.argmax(Vsm))
        if abs(float(tseg[js]) - t_ext) > 0.25 * (tseg[-1] - tseg[0]):   # bottom not robust -> reject
            continue
        t_bottom = t_ext - te
        if t_bottom <= 0:
            continue
        sr = dI / t_bottom
        if np.isfinite(sr) and _SRA_LO <= sr <= _SRA_HI:     # (4) physical slew band only
            srs.append(sr)
    return float(np.median(srs)) if srs else None


def _amps_ok(il):
    try:
        FM._amps(il); return True
    except Exception:
        return False


def _rms_db(model, gt):
    return float(np.sqrt(np.mean((20 * np.log10(np.abs(model) / np.abs(gt))) ** 2)))


# ----------------------------------------------------------------- current sinks
def _fit_admittance(f, Y):
    """Y(s) ~ g0 + s*Cp  (sink output conductance + parasitic cap), complex LS in
    [g0, Cp]. Degenerate-safe: <2 points -> constant g0 only; a non-physical negative
    parasitic cap is clamped to 0 (a sink cap cannot be negative). Returns (g0, Cp, rms_db)."""
    w = 2 * np.pi * f
    if f.size < 2:                                    # rank-deficient -> constant model
        g0 = float(np.mean(Y).real)
        return g0, 0.0, _rms_db(np.full_like(Y, g0), Y)
    A = np.c_[np.ones_like(w), 1j * w]                # [1, jw]
    x, *_ = np.linalg.lstsq(A, Y, rcond=None)
    g0, Cp = float(x[0].real), max(float(x[1].real), 0.0)   # clamp: parasitic cap >= 0
    Ym = g0 + 1j * w * Cp
    return g0, Cp, _rms_db(Ym, Y)


def _fit_cpsrr(f, PI):
    """current-PSRR pi(s) ~ c0 + s*c1 (low order; near-flat for a behavioral sink).
    Degenerate-safe: <2 points -> complex constant c0 only. Returns (c0, c1, rms_db)."""
    w = 2 * np.pi * f
    if f.size < 2:
        c0 = complex(np.mean(PI))
        return c0, 0j, _rms_db(np.full_like(PI, c0), PI)
    A = np.c_[np.ones_like(w), 1j * w]
    x, *_ = np.linalg.lstsq(A, PI, rcond=None)
    c0, c1 = complex(x[0]), complex(x[1])
    PIm = c0 + 1j * w * c1
    return c0, c1, _rms_db(PIm, PI)


def _iv_for_sink(ref, c):
    """Collect every I-V sweep present for sink c, keyed by its load LABEL:
    {label: [Vsweep, I]} from ref['iv_<c>_<label>']. {} when no I-V was run for c
    (legacy / T0 npz -> the legacy AC-only row fires)."""
    pre = f"iv_{c}_"
    files = ref if isinstance(ref, dict) else getattr(ref, "files", [])
    out = {}
    for k in files:
        if k.startswith(pre):
            arr = np.asarray(ref[k], float)
            if arr.ndim == 2 and arr.shape[0] >= 2 and arr.shape[1] >= 2:
                out[k[len(pre):]] = arr
    return out


def _noise_i_for_sink(ref, c):
    """Every in-situ current-noise PSD present for sink c, keyed by load LABEL:
    {label: [f, In]} from ref['noise_i_<c>_<label>'] (A/rtHz). {} when none measured
    (coverage.inoise off / legacy npz) -> the fit keeps in_white=in_kf=0 (the honest stub)."""
    pre = f"noise_i_{c}_"
    files = ref if isinstance(ref, dict) else getattr(ref, "files", [])
    out = {}
    for k in files:
        if str(k).startswith(pre):
            arr = np.asarray(ref[k], float)
            if arr.ndim == 2 and arr.shape[0] >= 2 and arr.shape[1] >= 2:
                out[str(k)[len(pre):]] = arr
    return out


def _temp_of_label(ref, label):
    """The temperature (degC) stamped on a load LABEL via meta_temp (positionally aligned
    with ref['loads']); NaN when absent / not stamped. The DC/iv once-cells are labeled
    'Lnom_T<temp>' so this recovers the I-V curve's temperature for the didt fit."""
    key = "meta_temp"
    files = ref if isinstance(ref, dict) else getattr(ref, "files", [])
    if key not in files:
        return float("nan")
    reflabels = [str(x) for x in np.asarray(ref["loads"]).ravel()]
    cur = np.asarray(ref[key], float).ravel()
    for lbl, t in zip(reflabels, cur):
        if str(lbl) == str(label):
            return float(t)
    return float("nan")


def _fit_current_largesignal(c, cp, ivmap, sink_dc, pol, tnom_c, ref):
    """The P0 fix: a LARGE-SIGNAL current-bias row, REUSING fit_isrc's internal fitters on
    the in-memory arrays (NO temp npz round-trip). Produced ONLY when sink c carries a real
    I-V sweep (iv_<c>_<label>); it then carries idc55/didt/g0/vc/gdd/vknee/knee_p/Cp/
    in_white/in_kf/pol/tnom_c so emit_pmu_model._current_block dispatches to the validated
    large-signal VA block. Falls back to None (caller keeps the legacy AC-only row) on a
    degenerate I-V.

      idc/g0/vknee/knee_p : fit_isrc._fit_iv(Vo, I, vc=sink_dc, pol, rout)  -- rout from a
                            first-pass output-conductance estimate (1/g0 from the I-V slope),
                            consistent with the AC admittance g0.
      idc55/didt          : fit_isrc._fit_temp(temps, idcT) over the per-temp I-V curves'
                            OP value (Idc at each temp). Single temp -> didt=0, idc55=idc.
      gdd (SIGNED PSRR)   : fit_isrc._fit_psrr(f, g), g=dI/dVsup = -pi (pi already carries the
                            SIGN from importmp's PI=-I/Vsup). The sign is KEPT (guardrail 2).
      Cp                  : the AC admittance imag (_fit_admittance). in_white/in_kf default 0
                            (sink noise is not in the in-situ matrix -- documented; the
                            large-signal block tolerates 0 noise)."""
    import fit_isrc as ISR
    # pick the OP-temperature I-V curve (the one whose label temp is nearest tnom_c, else any).
    labels = sorted(ivmap)
    tmap = {lbl: _temp_of_label(ref, lbl) for lbl in labels}
    def _near_nom(lbl):
        t = tmap[lbl]
        return abs(t - tnom_c) if np.isfinite(t) else 1e30
    op_label = min(labels, key=_near_nom)
    Vo, I = ivmap[op_label][:, 0], ivmap[op_label][:, 1]
    if np.ptp(Vo) <= 0 or Vo.size < 2:
        return None
    # output conductance g0 (-> rout, the _fit_iv I-V anchor). MIRROR the REPORT GRADE
    # (report_multiport: rout = 1/|ac_y[0].real|): derive g0 from the AC-admittance DC real
    # part so the emitted g0 EQUALS the graded g0 (emit==grade). The OLD code used a FULL-SWEEP
    # I-V chord (Is[-1]-Is[0])/(Vs[-1]-Vs[0]) -- despite the comment, that CROSSES the compliance
    # turn-off knee (~1.7V) -> ~225x too steep -> 29-37% IVrms baked into the .va while the report
    # grade reads 0.3-1.2%. Fall back to a POST-KNEE saturation-region chord (the upper half of the
    # sweep, away from the knee), NOT the full sweep, when no AC admittance is present.
    order = np.argsort(Vo)
    Vs, Is = Vo[order], I[order]
    g0_ac = None
    if cp.get("y"):                                # lowest-freq admittance real part = DC conductance
        ylbl = op_label if op_label in cp["y"] else next(iter(cp["y"]))
        yg = np.asarray(cp["y"][ylbl])
        if yg.ndim == 2 and yg.shape[0] >= 1 and np.isfinite(yg[0, 1]) and yg[0, 1] != 0:
            g0_ac = abs(float(yg[0, 1]))
    if g0_ac is not None and g0_ac > 0:
        rout = 1.0 / g0_ac
    else:
        # no AC admittance -> KNEE-AGNOSTIC chord: fit the slope only over the conducting
        # SATURATION region (|I| >= 0.5*Iplat), which excludes the collapsed knee tail on
        # EITHER side (high-side ceiling or low-side NMOS knee), so the slope is the true
        # output conductance, not the steep knee crossing of the full sweep.
        Iplat = float(np.median(np.sort(np.abs(Is))[-8:]))
        sat = np.abs(Is) >= 0.5 * Iplat
        if int(sat.sum()) >= 2 and np.ptp(Vs[sat]) > 0:
            g0_iv = float(np.polyfit(Vs[sat], Is[sat], 1)[0])
        else:
            dVk = Vs[-1] - Vs[0]
            g0_iv = float((Is[-1] - Is[0]) / dVk) if dVk != 0 else 0.0
        rout = 1.0 / max(abs(g0_iv), 1e-12)
    iv = ISR._fit_iv(Vs, Is, vc=float(sink_dc), pol=pol, rout=rout)   # idc/g0/vknee/knee_p

    # temp law: Idc at each temp = each curve's fitted OP current (interp at sink_dc).
    Tlist, idcT = [], []
    for lbl in labels:
        a = ivmap[lbl]
        t = tmap[lbl]
        if not np.isfinite(t):
            continue
        o2 = np.argsort(a[:, 0])
        idc_l = float(np.interp(float(sink_dc), a[o2, 0], a[o2, 1]))
        Tlist.append(t); idcT.append(idc_l)
    if len(Tlist) >= 2:
        tp = ISR._fit_temp(np.asarray(Tlist), np.asarray(idcT))
        idc55, didt, d2 = float(tp["idc55"]), float(tp["didt"]), float(tp["d2"])
    else:                                              # single temp -> flat in T
        idc55, didt, d2 = float(iv["idc"]), 0.0, 0.0

    # gdd (SIGNED current-PSRR): g = dI/dVsup = -pi (pi = -I/Vsup from importmp). Use the
    # model's primary current-PSRR supply (first pi key present); 0 when no pi was measured.
    gdd = 0.0
    pis = cp.get("pi", {})
    pi_keys = sorted({s for (s, il) in pis})
    if pi_keys:
        s0 = pi_keys[0]
        # the pi array at the OP label if present, else the first available load.
        cand = [il for (s, il) in pis if s == s0]
        il0 = op_label if op_label in cand else (cand[0] if cand else None)
        if il0 is not None:
            arr = pis[(s0, il0)]
            f = arr[:, 0]; PI = arr[:, 1] + 1j * arr[:, 2]
            g = -PI                                    # dI/dVsup, SIGN preserved
            ps = ISR._fit_psrr(f, g.real if np.allclose(g.imag, 0) else g)
            gdd = float(ps["gdd"])

    # Cp + the 2nd-order cascode/Wilson zero (y_wz/y_wp) from the AC admittance, via the SAME
    # fit_isrc._fit_admittance the report path uses (report<->emit consistency), anchored to the
    # I-V conductance g0. y_wz/y_wp stay None on a flat g0+sCp admittance -> emit is byte-identical.
    Cp, y_wz, y_wp, yrms = 0.0, None, None, float("nan")
    if cp.get("y"):
        il_y = op_label if op_label in cp["y"] else next(iter(cp["y"]))
        g = cp["y"][il_y]; f = g[:, 0]; Y = g[:, 1] + 1j * g[:, 2]
        af = ISR._fit_admittance(f, Y, float(iv["g0"]))
        Cp, y_wz, y_wp = float(af["cp"]), af["y_wz"], af["y_wp"]
        # grade the ADOPTED admittance model vs GT (dB-mag RMS, == report's diff_metrics yrms) so
        # the summary table + fit-log carry a REAL |Y| residual. The large-signal row used to omit
        # yrms entirely -> the table fell back to NaN (the box's `Yrms = nan` on every sink).
        try:
            yp = dict(g0=float(iv["g0"]), cp=Cp, y_wz=y_wz, y_wp=y_wp)
            yrms = float(ISR._y_rms_db(ISR.predict_y(yp, f), Y))
        except Exception:                              # noqa: BLE001 -- never break the fit on grading
            yrms = float("nan")

    # current-output noise (in_white/in_kf): fit the in-situ noise_i_<c>_<load> (A/rtHz, the probe
    # current noise) via the VALIDATED fit_isrc._fit_noise (white + 1/f). Absent (coverage.inoise
    # off / legacy npz) -> 0.0, the honest stub (req 1: current modeling now CARRIES noise when
    # measured). Prefer the OP-label curve, else any.
    in_white, in_kf = 0.0, 0.0
    nik = _noise_i_for_sink(ref, c) if ref is not None else {}
    if nik:
        nlbl = op_label if op_label in nik else next(iter(nik))
        narr = nik[nlbl]
        nz = ISR._fit_noise(narr[:, 0], narr[:, 1])
        in_white, in_kf = float(nz["in_white"]), float(nz["in_kf"])
    # I-V fit quality as a %-of-plateau current RMS (== report's diff_metrics ivrms) -- the
    # MEANINGFUL absolute metric. R^2 (iv_r2) is DEGENERATE for a near-flat current-source I-V:
    # a good sink holds I~const across V, so SS_tot (the data variance) ~ 0 and R^2 goes hugely
    # negative even when the current error is sub-%. The box's `IVr2 = -1.8..-7.6 [BAD!!]` on all
    # three sinks is exactly this artifact -- so the fit-log/anomaly now grade on ivrms, not R^2.
    try:
        Iplat = float(np.median(np.sort(Is)[-8:]))
        ivp = dict(idc=float(iv["idc"]), g0=float(iv["g0"]), vc=float(sink_dc),
                   vknee=float(iv["vknee"]), knee_p=float(iv["knee_p"]),
                   pol=pol, knee_side=iv["knee_side"], vhi=float(iv["vhi"]))
        Im = ISR.predict_iv(ivp, Vs)
        ivrms = float(np.sqrt(np.mean(((Im - Is) / (Iplat + 1e-30)) ** 2)) * 100.0)
    except Exception:                                  # noqa: BLE001
        ivrms = float("nan")
    return dict(sink=c, il=op_label, pol=pol, vc=float(sink_dc),
                idc55=idc55, didt=didt, d2=d2, g0=float(iv["g0"]),
                gdd=gdd, vknee=float(iv["vknee"]), knee_p=float(iv["knee_p"]),
                knee_side=iv["knee_side"], vhi=float(iv["vhi"]),
                Cp=float(Cp), y_wz=y_wz, y_wp=y_wp, yrms=yrms, ivrms=ivrms,
                iv_vmin=float(Vs[0]), iv_vmax=float(Vs[-1]),   # swept I-V range (for OP-in-range check)
                in_white=in_white, in_kf=in_kf, tnom_c=float(tnom_c),
                iv_r2=float(iv["iv_r2"]))


def _fit_current_ports(cports, supplies, ref=None, manifest=None, tnom_c=55.0):
    """Fit each current sink's behavioral model. Returns a list of report rows.

    LARGE-SIGNAL core (the P0 fix): when sink c carries a real I-V sweep (iv_<c>_<label> in
    `ref`) it gets ONE large-signal row (idc55/didt/g0/vc/gdd/vknee/knee_p/Cp/pol/tnom_c) so
    emit_pmu_model._current_block dispatches to the validated large-signal VA block. When NO
    I-V sweep is present (legacy / T0 npz), it keeps producing TODAY's legacy AC-only rows
    (g0/Cp/pi magnitude) so the legacy block fires -- byte-identical to the pre-stage-2b path.
    `ref`/`manifest` are optional: absent -> the legacy-only behavior (single-OP tests)."""
    m = manifest or {}
    rows = []
    for c, cp in cports.items():
        ivmap = _iv_for_sink(ref, c) if ref is not None else {}
        if ivmap:                                       # T2+ : the large-signal core
            sink_dc = float((m.get("i_out") or {}).get(c, {}).get("dc", 0.0))
            pol = str((m.get("i_out") or {}).get(c, {}).get("pol", "sink"))
            lrow = _fit_current_largesignal(c, cp, ivmap, sink_dc, pol, tnom_c, ref)
            if lrow is not None:
                # carry the AC current-PSRR report fields too (so the report table still
                # shows pi_<s>), additive to the large-signal params.
                lrow["pi"] = {}
                for (s, il2), arr in cp["pi"].items():
                    f = arr[:, 0]; PI = arr[:, 1] + 1j * arr[:, 2]
                    _, _, prms = _fit_cpsrr(f, PI)
                    lrow["pi"].setdefault(s, dict(rms=prms, dc=float(np.abs(PI[0]))))
                rows.append(lrow)
                continue                                # one large-signal row per sink
        # legacy AC-only rows (one per load) -- unchanged
        for il in cp["loads"]:
            row = dict(sink=c, il=il)
            if il in cp["y"]:
                g = cp["y"][il]; f = g[:, 0]; Y = g[:, 1] + 1j * g[:, 2]
                g0, Cp, yrms = _fit_admittance(f, Y)
                row.update(g0=g0, Cp=Cp, yrms=yrms, ydc=float(np.abs(Y[0])))
            row["pi"] = {}
            for (s, il2), arr in cp["pi"].items():
                if il2 != il:
                    continue
                f = arr[:, 0]; PI = arr[:, 1] + 1j * arr[:, 2]
                c0, c1, prms = _fit_cpsrr(f, PI)
                row["pi"][s] = dict(rms=prms, dc=float(np.abs(PI[0])))
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- driver
def _log_voltage_summary(o, vf):
    """One-line per-rail fit summary captured into result['fit_log'] -- the HIGH-VALUE decisions
    a reviewer needs at a glance: vreg SOURCE (a silently-inert `baked` here when a load-reg
    schedule was expected is the exact bug class we hit), Cft gate, noise mode, Cout/ESR.
    Defensive: a missing field degrades to a fallback, never breaks the fit."""
    try:
        vs = vf.get("vreg_sched")
        sl = vf.get("schedule_loads", []) or []
        if vs:
            cur, vr = list(vs.get("currents", [])), list(vs.get("vregs", []))
            vreg = (f"load-reg schedule {len(cur)}pts [{min(cur):g}..{max(cur):g}]A"
                    + (f" -> vreg [{min(vr):.4f}..{max(vr):.4f}]V" if vr else ""))
        elif len(sl) > 1:
            vreg = f"multi-load AC schedule ({len(sl)} loads)"
        else:
            P = vf.get("P") or {}
            labs = list(P.keys())
            vreg = (f"baked {float(P[labs[len(labs) // 2]]['vreg']):.4f}V (single-OP)"
                    if labs else "baked (single-OP)")
            # spell out WHY the load-reg schedule did not engage -- the exact data/manifest gap:
            #   0 wfm in npz  -> transient steps were never simulated/imported into THIS npz
            #   N wfm, 0 mapped -> npz HAS the waveforms but the fit's manifest has no
            #                      coverage.transient steps to map them to
            vreg += (f" [load-reg OFF: {vf.get('n_tr_npz', 0)} transient wfm in npz, "
                     f"{vf.get('n_tr_mapped', 0)} mapped to manifest steps -> need >=2 load pts]")
        cft = float(vf.get("cft", 0.0) or 0.0)
        sa = vf.get("slew_a")
        slew = (f"branch-A slew {float(sa):.3g} A/s (large-signal undershoot)" if sa and sa > 0
                else f"OFF (no transient undershoot; {vf.get('n_tr_mapped', 0)} steps mapped)")
        print(f"[fit]  V-rail {o} (pin {vf.get('pin', o)}): vreg={vreg}; "
              f"Cft={('%.1ffF' % (cft * 1e15)) if cft > 0 else 'off'}; slew={slew}; "
              f"noise={vf.get('nmode', '?')}/{len(vf.get('nfk', []))}fk; "
              f"Cout={float(vf.get('cout', float('nan'))):.3g}F "
              f"ESR={float(vf.get('esr', float('nan'))):.3g}ohm")
        # mapped-but-no-schedule: dump each waveform's settled extraction so the gap is visible
        for d in (vf.get("loadreg_diag") or []):
            print(f"[fit]      load-reg wfm  {d}")
        # fit QUALITY (the 'specific process'): worst-over-corner residual + verdict per transfer.
        errs = vf.get("err") or []
        if errs:
            zr = max(float(e["zrms"]) for e in errs)
            nr = max(float(e["nrms"]) for e in errs)
            prs = [rms for e in errs for rms, _ in (e.get("psrr") or {}).values()]
            pr = max(prs) if prs else float("nan")
            print(f"[fit]    quality {o}: Zout {zr:.2f}dB[{_verdict(zr, 1.0, 3.0)}] "
                  f"PSRR {pr:.2f}dB[{_verdict(pr, 1.0, 3.0)}] "
                  f"noise {nr:.2f}dB[{_verdict(nr, 1.5, 3.0)}]")
    except Exception as e:                             # noqa: BLE001 -- a log line must never break the fit
        print(f"[fit]  V-rail {o}: <summary unavailable: {e}>")


def _log_current_summary(r):
    """One-line per-sink fit summary: idc(@tnom), temp slope, I-V knee, admittance zero, white
    noise, I-V fit R^2. Defensive -- missing fields degrade gracefully (a small-signal-only sink
    carries fewer keys)."""
    try:
        bits = [f"idc={float(r.get('idc55', float('nan'))) * 1e6:.3g}uA@{r.get('tnom_c', '?')}C"]
        didt = float(r.get("didt", 0.0) or 0.0)
        if didt:
            bits.append(f"didt={didt * 1e9:.3g}nA/K")
        vk = r.get("vknee")
        if vk is not None and r.get("knee_side"):
            bits.append(f"knee@{float(vk):.3g}V({r.get('knee_side')})")
        if r.get("y_wz") and r.get("y_wp"):
            bits.append(f"Yzero(wz={float(r['y_wz']):.3g},wp={float(r['y_wp']):.3g})")
        inw = float(r.get("in_white", 0.0) or 0.0)
        if inw:
            bits.append(f"in_wht={inw:.2e}A2/Hz")
        ivr = r.get("ivrms")
        if ivr is not None and np.isfinite(ivr):       # %-of-plateau RMS = the MEANINGFUL I-V grade
            bits.append(f"IVrms={float(ivr):.2f}%[{_verdict(ivr, 2.0, 5.0)}]")
        yr = r.get("yrms")
        if yr is not None and np.isfinite(yr):
            bits.append(f"Yrms={float(yr):.2f}dB[{_verdict(yr, 3.0, 8.0)}]")
        print(f"[fit]  I-sink {r.get('sink', '?')} (pin {r.get('pin', r.get('sink', '?'))}, "
              f"{r.get('pol', 'sink')}): " + ", ".join(bits))
    except Exception as e:                             # noqa: BLE001
        print(f"[fit]  I-sink {r.get('sink', '?')}: <summary unavailable: {e}>")


def _verdict(x, good, marg):
    """OK / ~ / BAD!! verdict for a residual against (good, marginal) bars. NaN -> NaN!!."""
    try:
        x = float(x)
        if not np.isfinite(x):
            return "NaN!!"
        return "OK" if x <= good else ("~" if x <= marg else "BAD!!")
    except Exception:                                  # noqa: BLE001
        return "?"


def _log_provenance(npz_path, m):
    """SECTION 0 -- provenance: the running CODE version (so the log itself proves a `bash apply`
    landed), the npz path/size/mtime, and the manifest coverage fingerprint. Answers 'did my fix
    deploy?' and 'which inputs?' without a round-trip. Defensive."""
    import os
    import datetime

    def _mtime(p):
        try:
            return datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
        except Exception:                              # noqa: BLE001
            return "?"
    sha = "?"
    try:
        import subprocess
        sha = (subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                               "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=3).stdout.strip() or "?")
    except Exception:                                  # noqa: BLE001
        pass
    # the module mtime moves on every bash-apply even when the box is NOT a git checkout -> the
    # reliable 'did the new code land' signal.
    print("[fit] ===== [0] PROVENANCE (did my fix deploy? which inputs?) =====")
    print(f"[fit] code    : git {sha} | fit_multiport.py mtime {_mtime(__file__)}")
    try:
        sz = f"{os.path.getsize(npz_path) / 1024:.0f} KB"
    except Exception:                                  # noqa: BLE001
        sz = "? KB"
    print(f"[fit] npz     : {npz_path}  ({sz}, mtime {_mtime(npz_path)})")
    cov = m.get("coverage") or {}
    tr = cov.get("transient") or {}
    nsteps = sum(len((tr.get(o) or {}).get("steps") or []) for o in tr)
    print(f"[fit] manifest: name={m.get('name')} tier={cov.get('tier')} "
          f"enable={cov.get('enable')} temps={cov.get('temps')} "
          f"transient_steps={nsteps} temp_sweep={cov.get('temp_sweep')}")


def _ref_keys(ref):
    """The npz key list whether ref is a plain dict (load_multiport) or an .files-bearing
    NpzFile/_NpzLike. Defensive -> [] on anything odd."""
    try:
        return [k for k in (getattr(ref, "files", None) or list(ref)) if isinstance(k, str)]
    except Exception:                                  # noqa: BLE001
        return []


def _log_npz_inventory(ref, m):
    """SECTION 1 -- list EVERY array in the npz grouped by measurement kind (+ labels + sweep
    range), so the log shows exactly WHAT simulation results are present. Defensive."""
    files = _ref_keys(ref)
    if not files:
        return
    cats = [("z_", "Zout AC"), ("noise_i_", "current noise"), ("noise_", "output noise"),
            ("p_", "PSRR"), ("pi_", "current-PSRR"), ("y_", "admittance"),
            ("tr_", "transient step"), ("iv_", "I-V sweep"), ("dc_", "dropout/DC"),
            ("m_", "carried model"), ("meta_", "meta")]
    try:
        _loads = [str(x) for x in ref["loads"]] if "loads" in files else "?"
    except Exception:                                  # noqa: BLE001
        _loads = "?"
    print(f"[fit] ===== [1] SIMULATIONS PRESENT (which results exist): "
          f"{len(files)} arrays (loads={_loads}) =====")
    seen = set()
    for pre, lab in cats:
        ks = sorted(k for k in files if k.startswith(pre) and k not in seen)
        if pre == "noise_":
            ks = [k for k in ks if not k.startswith("noise_i_")]
        seen.update(ks)
        if not ks:
            continue
        rng = ""
        try:
            a = np.asarray(ref[ks[0]])
            if a.ndim == 2 and a.shape[0] > 1:
                rng = f"  [axis0 {a[0, 0]:.3g}..{a[-1, 0]:.3g}, {a.shape[0]}pts]"
        except Exception:                              # noqa: BLE001
            pass
        shown = ", ".join(ks) if len(ks) <= 10 else ", ".join(ks[:10]) + f", +{len(ks) - 10} more"
        print(f"[fit]   {lab:16s}: {len(ks):2d}  {shown}{rng}")
    other = sorted(k for k in files if k not in seen and k != "loads")
    if other:
        print(f"[fit]   {'other':16s}: {len(other)}  {', '.join(other)}")


def _unused_reason(k):
    """Why a present npz array went UNUSED -- the actionable hint. Distinguishes a BUG (should be
    consumed but a mapping failed) from a BY-DESIGN gap (no model path for it yet)."""
    if k.startswith("tr_"):
        return ("transient present but NOT mapped to a manifest step (label mismatch / no "
                "coverage.transient) -> vreg stays BAKED (the load-reg/20mV gap)")
    if k.startswith("couple_"):
        return ("BY DESIGN: output<->output coupling (transfer-Z V_other/I_this, auto-emitted per "
                "rail pair) -- the PMU has NO cross-rail coupling model today, so it is measured but "
                "not fit. OK if the rails are isolated; if |couple| is large vs each rail's own Zout "
                "it is a real modeling gap. Not a bug.")
    if k.startswith("dc_"):
        return "dropout/DC sweep present but DC-dropout/slew emission is stage-2b (not emitted yet)"
    if k.startswith("iv_"):
        return "I-V present but the sink took NO large-signal row (no matching i_out dc/pol?) -- check"
    if k.startswith(("z_", "p_", "noise_")):
        return "AC array for a load that carries no z_ (orphan corner) -> not a voltage-fit corner"
    return "present but not consumed by any fit path"


def _log_consumption(ref, m, views, volt, curr):
    """SECTION 2 -- which simulation arrays the fit CONSUMED vs which were present but UNUSED
    (with a reason). The round-trip killer: a present-but-unused transient/dropout names itself."""
    files = set(_ref_keys(ref))
    if not files:
        return
    used = set()
    for o in m.get("v_out", {}):
        view = views.get(o, {}) or {}
        sp = view.get("npz", {}) or {}
        loads_fit = [il for il in view.get("loads", []) if f"z_{il}" in sp]
        for il in loads_fit:
            used |= {f"z_{o}_{il}", f"noise_{o}_{il}"} & files
            for s in (m.get("supplies", {}) or {}):
                if f"p_{o}_{s}_{il}" in files:
                    used.add(f"p_{o}_{s}_{il}")
        for vk in (view.get("tr_steps") or {}):        # tr_<label> view-key -> tr_<o>_<label> npz key
            ok = f"tr_{o}_{vk[3:]}"
            if ok in files:
                used.add(ok)
    for r in (curr or []):
        c = r.get("sink")
        used |= {k for k in files
                 if any(k.startswith(f"{p}_{c}_") or k == f"{p}_{c}"
                        for p in ("iv", "y", "pi", "noise_i"))}
    data = sorted(k for k in files
                  if k != "loads" and not k.startswith(("meta_", "m_")))  # m_* = carried model, not GT
    unused = [k for k in data if k not in used]
    print(f"[fit] ===== [2] WHICH RESULTS WERE USED IN THE FIT: {len(data)} data arrays -> "
          f"{len(data) - len(unused)} USED, {len(unused)} UNUSED =====")
    for k in unused:
        print(f"[fit]   UNUSED {k}  -> {_unused_reason(k)}")
    if not unused:
        print("[fit]   (every simulation array was consumed)")


def _badnum(x):
    try:
        return not np.isfinite(float(x))
    except Exception:                                  # noqa: BLE001
        return True


def _log_anomalies(volt, curr):
    """SECTION 4 -- auto-flag the red numbers a reviewer would otherwise have to spot by eye: a
    bad-fit R^2, a NaN metric, a marginal/BAD residual. Empty -> 'none flagged'. Defensive."""
    flags = []
    for o, vf in (volt or {}).items():
        for e in vf.get("err", []):
            zr = e.get("zrms")
            if _badnum(zr):
                flags.append(f"V-rail {o}: Zout RMS is NaN/inf (degenerate Zout fit)")
            elif float(zr) > 3.0:
                flags.append(f"V-rail {o}: Zout RMS {float(zr):.1f}dB > 3 (marginal/BAD)")
            if _badnum(e.get("nrms")):
                flags.append(f"V-rail {o}: noise RMS NaN/inf")
        if _badnum(vf.get("cout")) or _badnum(vf.get("esr")):
            flags.append(f"V-rail {o}: Cout/ESR NaN")
    for r in (curr or []):
        c = r.get("sink")
        # grade I-V on the %-of-plateau RMS (the meaningful absolute error), NOT R^2: a near-flat
        # current-source I-V drives SS_tot->0 so R^2 is hugely negative even at sub-% error -- the
        # box's IVr2=-1.8..-7.6 was a metric artifact, not a bad fit. Flag only a real >5% miss.
        ivr = r.get("ivrms")
        if ivr is not None and not _badnum(ivr) and float(ivr) > 5.0:
            vc, vlo, vhi = r.get("vc"), r.get("iv_vmin"), r.get("iv_vmax")
            # the #1 cause of a big I-V miss: the sink's OPERATING point (i_out.dc = vc) is OUTSIDE
            # the swept I-V range -> the Idc anchor + knee are EXTRAPOLATED. Name it precisely.
            if (vc is not None and vlo is not None and vhi is not None
                    and not (_badnum(vc) or _badnum(vlo) or _badnum(vhi))
                    and not (float(vlo) <= float(vc) <= float(vhi))):
                flags.append(f"I-sink {c}: I-V fit RMS={float(ivr):.1f}% > 5% -> OP vc={float(vc):g}V "
                             f"is OUTSIDE the swept I-V [{float(vlo):g}..{float(vhi):g}]V -> the fit "
                             f"EXTRAPOLATES. Re-sweep I-V to cover vc, or fix i_out.dc.")
            else:
                flags.append(f"I-sink {c}: I-V fit RMS={float(ivr):.1f}% > 5% -> BAD I-V fit "
                             f"(check the sink dc/pol, or the knee side)")
        if _badnum(r.get("yrms")):
            flags.append(f"I-sink {c}: |Y| RMS NaN/inf (admittance not graded)")
        if _badnum(r.get("g0")):
            flags.append(f"I-sink {c}: g0 NaN")
    if flags:
        print(f"[fit] ===== [!] ANOMALIES (fix these first): {len(flags)} flagged =====")
        for f in flags:
            print(f"[fit]   !! {f}")
    else:
        print("[fit] --- anomalies: none flagged ---")


class _Tee:
    """Write to several streams at once: live passthrough (terminal/CLI unchanged) + a capture
    buffer. A dead/closed stream is skipped, never raised -- the fit must not die on logging."""
    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:                          # noqa: BLE001
                pass
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:                          # noqa: BLE001
                pass


def fit_multiport(npz_path, manifest, vout_dc=None):
    """Fit every modeled port of a multi-port npz (PUBLIC entry).

    Captures the WHOLE fit's diagnostic output into result['fit_log'] -- a copy-pasteable
    record of the fitting process (per-rail vreg source / Cft gate / noise mode / Cout-ESR,
    per-sink knee, plus every fitter's residual/adapt/fallback print) for offline debugging --
    while still streaming it live to stdout so the CLI/terminal behaviour is unchanged. The
    per-rail summary line in particular makes a silently-inert decision (e.g. `vreg=baked 0.8V`
    when a load-reg schedule was expected) obvious at a glance. See _fit_multiport_impl."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = _Tee(old, buf)
    try:
        result = _fit_multiport_impl(npz_path, manifest, vout_dc)
    finally:
        sys.stdout = old
    if isinstance(result, dict):
        result["fit_log"] = buf.getvalue()
    return result


def _fit_multiport_impl(npz_path, manifest, vout_dc=None):
    """Fit every modeled port of a multi-port npz. Returns a structured result dict:
    {voltage: {o: <fit>}, current: [rows], meta, fit_log}. Pure-Python; no simulator."""
    from insitu import importmp as IM
    ref = IM.load_multiport(npz_path)
    m = manifest
    vmap = vout_dc or {}
    views = IM.split_ports(ref, m)
    cports = IM.current_ports(ref, m)
    supplies = list(m["supplies"])
    print(f"[fit] === {pathlib.Path(npz_path).stem}: {len(m.get('v_out', {}))} V-rail(s), "
          f"{len(cports)} current sink(s); tier={(m.get('coverage') or {}).get('tier')}, "
          f"supplies={supplies} ===")
    _log_provenance(npz_path, m)                        # SECTION 0: code version + inputs identity
    _log_npz_inventory(ref, m)                         # SECTION 1: what simulation results exist
    volt = {}
    sched_meta = {}                                # per-rail {labels, currents} for emit
    for o in m["v_out"]:
        vdc = vmap.get(o, 0.8)
        # the rail's REAL per-label current (in-situ truth) -> the emit-side ln(iload)
        # schedule abscissa. {} on a single-OP / legacy npz -> iv falls back to the
        # numeric-label parse then 0.0, byte-identical to the pre-stage-2b path.
        ilmap = _iload_map(ref, o, [str(x) for x in views[o]["loads"]])
        volt[o] = _fit_voltage_output(o, views[o], supplies, vout_dc=vdc, iload_map=ilmap)
        # branch-A slew SRa [A/s] is AUTO-FIT from coverage.transient inside _fit_voltage_output.
        # The manifest m['v_out'][rail]['slew_a'], when present and >0, is an OVERRIDE (the user-
        # tuned SRa / the escape hatch for GHz-contaminated DUTs) -- it wins over the auto-fit.
        # Absent/<=0 -> keep the auto-fitted value (None when no clean undershoot -> byte-identical).
        _sa = (m["v_out"][o] or {}).get("slew_a")
        if _sa and float(_sa) > 0:
            volt[o]["slew_a"] = float(_sa)
        # recovery (overdamped 2nd-order Zout) is an OPT-IN, manifest-driven OVERRIDE: when the
        # rail carries a valid m['v_out'][rail]['recovery'] = {Lreg,Rreg,Cs,Rs} (all >0) it is
        # threaded to the emit, which reshapes the post-dip climb into a monotonic ~100ns
        # overdamped recovery + a DC-blocked snubber that kills the slew-induced overshoot
        # (the in-situ LTI Zout + slew front-end otherwise rings/over-recovers under a fast
        # load step). Absent/invalid -> not attached -> the rail emits byte-identical. There is
        # NO auto-fit yet: the 4-param overdamped shape needs a clean characterization step the
        # in-situ pipeline does not currently isolate (see _fit_recovery flag below), so this is
        # a hand-tuned designer knob (mirrors the slew_a manifest escape hatch).
        _rc = _fit_recovery(m["v_out"][o] or {})
        if _rc is not None:
            volt[o]["recovery"] = _rc
        # OPT-IN La (dip-inductor / low-freq Zout) OVERRIDE. The small-signal AC-Zout fit
        # under-estimates the effective branch-A inductance ~5x (gives ~24uH; the real load-
        # transient recovery -- the higher low-freq Zout -- needs ~120uH; proven on the local
        # replay loop: La=24uH+recovery -> 11mV, La=120uH+recovery -> 2.47mV vs real silicon).
        # Until the La fit is improved to read the recovery time-constant, m['v_out'][rail]
        # ['la_override'] (a finite, positive [H]) corrects it. Applied AFTER all per-load
        # fitting (incl. PSRR, which keeps its already-fit coefficients), so it only reshapes
        # the emitted branch-A inductor / Zout; absent -> the fitted La is kept (byte-identical).
        # Mirrors the slew_a / recovery escape-hatch discipline; pairs with the recovery dict.
        _laov = (m["v_out"][o] or {}).get("la_override")
        if _laov is not None:
            try:
                _laov = float(_laov)
            except (TypeError, ValueError):
                _laov = None
            if _laov is not None and 0 < _laov < 1e30:
                for _p in volt[o]["P"].values():
                    _p["L_a"] = _laov
        # carry the designer's GUI symbol pin name (set by build_manifest) so the model
        # cell's PORT is the pin, not our internal role key. Default: the role key itself
        # (the stand-in manifest carries no 'pin', so it stays 'pll'/'vco' etc.).
        volt[o]["pin"] = m["v_out"][o].get("pin", o)
        sl = volt[o].get("schedule_loads", [])
        sched_meta[o] = dict(labels=list(sl),
                             currents=[float(volt[o]["P"][il]["iv"]) for il in sl])
        _log_voltage_summary(o, volt[o])
    # nominal temp the Idc(T) fit references: middle of the manifest temps, else 55.
    _tnom = 55.0
    try:
        from insitu import manifest as _Mt
        _tps = _Mt.temps(m)
        if _tps:
            _tnom = float(_tps[len(_tps) // 2])
    except Exception:                              # noqa: BLE001
        pass
    curr = _fit_current_ports(cports, m["current_psrr_supplies"],
                              ref=ref, manifest=m, tnom_c=_tnom)
    for r in curr:
        r["pin"] = m["i_out"].get(r["sink"], {}).get("pin", r["sink"])
        _log_current_summary(r)
    _log_consumption(ref, m, views, volt, curr)        # SECTION 2: used vs present-but-unused
    _log_anomalies(volt, curr)                          # SECTION 4: auto-flag red numbers
    # provenance for the emit banner (emit_pmu_va reads these off meta by default, so
    # step_emit needs no new args). All optional / defensive -- a coverage-free or
    # hand-built manifest leaves them None and the banner falls back to 'unspecified'.
    try:
        from insitu import manifest as _Mp
        cov = m.get("coverage") or {}
        coverage_tier = cov.get("tier")
        temps = list(cov.get("temps") or [])
        op_temp = temps[len(temps) // 2] if temps else None
        # union load envelope over every v_out's declared load_points (None when none declared)
        all_loads = []
        for o in m.get("v_out", {}):
            all_loads += [float(x) for x in _Mp.load_points(m, o)]
        valid_load = (min(all_loads), max(all_loads)) if all_loads else None
        op_iload = all_loads[0] if all_loads else None
    except Exception:                              # noqa: BLE001 -- provenance is best-effort
        coverage_tier = valid_load = op_iload = op_temp = None
    return dict(voltage=volt, current=curr,
                meta=dict(name=pathlib.Path(npz_path).stem,
                          loads=[str(x) for x in ref["loads"]],
                          supplies=supplies,
                          coverage_tier=coverage_tier, valid_load=valid_load,
                          op_iload=op_iload, op_temp=op_temp,
                          tnom_c=_tnom, schedule_loads=sched_meta))


def export_single_port_refs(npz_path, manifest, vout_dc=None, outdir=None):
    """Write each voltage output as a SINGLE-port npz (results/ref/<variant>_<o>.npz) that
    the EXISTING ModelerCore / fit_model.fit_variant / emit consume UNCHANGED -- so the GUI
    Fit/Compare tabs and the Verilog-A emit work per output with ZERO new fit/emit code.

    The in-situ OP (one iload, set by the designer's TB) maps to fit_model's iload axis:
    the corner key is the manifest iload (e.g. '500u').

    ANTI-FOOTGUN (stage 2a): we DO NOT fabricate DC. When the SOURCE multi-port npz carries
    a REAL dropout sweep for output o (key 'dc_<o>' or 'dc_<o>_<load>', shape [Iload, Vout]
    from importmp's 'dropout' derive), we carry it through as fit_model's dc_loadreg AND
    dc_dropout (the same real load sweep of the regulated output -- the in-situ sweep does
    not distinguish the two, so both read the one real curve). When the npz has NO real dc
    array for o (a small-signal-only T0 export), we OMIT dc_loadreg/dc_dropout ENTIRELY ->
    the single-port emit emits NO dropout/load-reg/current-limit term (honest scope), rather
    than a flat fabricated stand-in. dc_linereg has no in-situ line-reg sweep yet -> always
    omitted (never fabricated) unless a real one is present. Returns {output: path}.

    NOTE on axes: multi-PVT-corner single-port modeling (PVT != iload) is handled by the
    multiport report's own per-load loop; this single-port export targets the GUI's
    one-DUT-at-a-time path and uses the nominal corner."""
    from insitu import importmp as IM
    ref = IM.load_multiport(npz_path)
    m = manifest
    vmap = vout_dc or {}
    views = IM.split_ports(ref, m)
    loads = [str(x) for x in ref["loads"]]
    nom = loads[len(loads) // 2]
    outdir = pathlib.Path(outdir) if outdir else (ROOT / "results" / "ref")
    outdir.mkdir(parents=True, exist_ok=True)
    stem = pathlib.Path(npz_path).stem
    out_paths = {}
    for o, v in views.items():
        sp = v["npz"]
        meta = m["v_out"][o]
        iload = float(meta.get("iload", 500e-6))
        ilkey = _amps_to_key(iload)
        vdc = vmap.get(o, meta.get("vout_dc", 0.8))
        rec = {"loads": np.array([ilkey]),
               f"z_{ilkey}": sp[f"z_{nom}"],
               f"p_{ilkey}": sp[f"p_{nom}"],
               f"noise_{ilkey}": sp[f"noise_{nom}"],
               "meta_cout": sp.get("meta_cout", np.array(np.nan)),
               "meta_esr": sp.get("meta_esr", np.array(np.nan)),
               "meta_port": np.array(o), "meta_vout_dc": np.array(vdc)}
        # REAL DC only -- no fabrication. The dropout sweep lands in the FULL multi-port ref
        # (split_ports does not carry it into the per-output view), keyed 'dc_<o>' or
        # 'dc_<o>_<load>', shape [Iload, Vout]. When present, feed it to fit_model as BOTH
        # dc_loadreg and dc_dropout (the one real load sweep of the regulated output). When
        # absent -> emit NOTHING for the DC term (small-signal-only scope; the consumers in
        # fit_model gracefully skip the dropout/load-reg branch). dc_linereg: no in-situ
        # line-reg sweep -> omitted unless a real one is present.
        dckey = next((k for k in ref if k == f"dc_{o}" or k.startswith(f"dc_{o}_")), None)
        if dckey is not None:
            dc_real = np.asarray(ref[dckey])
            rec["dc_loadreg"] = dc_real
            rec["dc_dropout"] = dc_real
        lrkey = next((k for k in ref
                      if k == f"linereg_{o}" or k.startswith(f"linereg_{o}_")), None)
        if lrkey is not None:
            rec["dc_linereg"] = np.asarray(ref[lrkey])
        p = outdir / f"{stem}_{o}.npz"
        np.savez(p, **rec)
        out_paths[o] = p
    return out_paths


def _amps_to_key(a):
    """amps -> a corner key fit_model.ng.amps round-trips ('500u','1m',...)."""
    for suf, sc in (("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if a >= sc:
            v = a / sc
            return (f"{v:g}{suf}")
    return f"{a:g}"


def emit_models(npz_path, manifest, vout_dc=None, modeldir=None):
    """Best-effort: export per-output single-port refs, then fit+emit each via the EXISTING
    fit_model path -> model/<variant>_<o>.va (+ .lib + dropout .tbl). Returns
    {output: {"va","lib"} | {"error"}}. Never raises: a per-output emit failure is reported,
    not fatal (the report is the always-on deliverable)."""
    refs = export_single_port_refs(npz_path, manifest, vout_dc=vout_dc)
    modeldir = pathlib.Path(modeldir) if modeldir else (ROOT / "model")
    modeldir.mkdir(parents=True, exist_ok=True)
    out = {}
    for o, refp in refs.items():
        try:
            with _fm_globals():
                fr = FM.fit_variant(refp.stem, nominal=None, vref=1.05)
                lib = modeldir / f"{refp.stem}.lib"
                va = modeldir / f"{refp.stem}.va"
                tbl = modeldir / f"{refp.stem}_dropout.tbl"
                FM.emit(fr.P, lib)
                FM.emit_va(fr.P, va, tbl)
            out[o] = {"va": va, "lib": lib, "ref": refp}
        except Exception as e:        # noqa: BLE001 -- emit is best-effort by design
            out[o] = {"error": f"{type(e).__name__}: {e}", "ref": refp}
    return out


def report(res):
    """Human report: voltage-port table, then a SEPARATE current-port table."""
    L = []
    L.append(f"=== Multi-port fit report: {res['meta']['name']} ===")
    L.append(f"loads={res['meta']['loads']}  supplies={res['meta']['supplies']}")
    L.append("")
    L.append("--- VOLTAGE OUTPUTS (Zout / PSRR per supply / noise) ---")
    sups = res["meta"]["supplies"]
    hdr = f"{'out':>5} {'load':>6} {'Cout[pF]':>9} {'ESR':>6} {'Zrms[dB]':>9}"
    for s in sups:
        hdr += f" {'P_'+s+'[dB]':>10} {'P_'+s+'[deg]':>10}"
    hdr += f" {'Nrms[dB]':>9}"
    L.append(hdr)
    for o, fit in res["voltage"].items():
        for e in fit["err"]:
            line = (f"{o:>5} {e['il']:>6} {fit['cout']*1e12:9.1f} {fit['esr']:6.3f} "
                    f"{e['zrms']:9.3f}")
            for s in sups:
                pr, pd = e["psrr"][s]
                line += f" {pr:10.3f} {pd:10.2f}"
            line += f" {e['nrms']:9.3f}"
            L.append(line)
    L.append("")
    L.append("--- CURRENT SINKS (admittance / current-PSRR) -- reported SEPARATELY ---")
    chdr = f"{'sink':>7} {'load':>6} {'g0[S]':>11} {'Cp[F]':>11} {'Yrms[dB]':>9}"
    pis = sorted({s for r in res["current"] for s in r.get("pi", {})})
    for s in pis:
        chdr += f" {'pi_'+s+'[dB]':>11}"
    L.append(chdr)
    for r in res["current"]:
        line = (f"{r['sink']:>7} {r['il']:>6} {r.get('g0', float('nan')):11.3e} "
                f"{r.get('Cp', float('nan')):11.3e} {r.get('yrms', float('nan')):9.3f}")
        for s in pis:
            line += f" {r['pi'].get(s, {}).get('rms', float('nan')):11.3f}"
        L.append(line)
    # worst-case rollup (voltage vs current kept separate)
    vz = [e["zrms"] for fit in res["voltage"].values() for e in fit["err"]]
    vp = [pr for fit in res["voltage"].values() for e in fit["err"]
          for pr, _ in e["psrr"].values()]
    vn = [e["nrms"] for fit in res["voltage"].values() for e in fit["err"]]
    cy = [r["yrms"] for r in res["current"] if "yrms" in r]
    cp = [d["rms"] for r in res["current"] for d in r.get("pi", {}).values()]
    L.append("")
    L.append(f"worst VOLTAGE  : Zout {max(vz, default=0):.2f}dB  PSRR {max(vp, default=0):.2f}dB"
             f"  noise {max(vn, default=0):.2f}dB")
    L.append(f"worst CURRENT  : Y {max(cy, default=0):.2f}dB  current-PSRR {max(cp, default=0):.2f}dB")
    return "\n".join(L)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Multi-port in-situ fit + report")
    ap.add_argument("--variant", required=True, help="results/ref/<variant>.npz stem")
    ap.add_argument("--manifest", required=True, help="pin-role manifest name/path")
    ap.add_argument("--report-out", default=None, help="write the text report here")
    ap.add_argument("--export-refs", action="store_true",
                    help="also write per-output single-port refs (results/ref/<v>_<o>.npz)")
    ap.add_argument("--emit", action="store_true",
                    help="also emit per-output Verilog-A via the existing fit_model path")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "cadence"))
    from insitu import manifest as _M
    m = _M.load(a.manifest)
    npz = ROOT / "results" / "ref" / f"{a.variant}.npz"
    res = fit_multiport(npz, m)
    txt = report(res)
    print(txt)
    if a.report_out:
        pathlib.Path(a.report_out).write_text(txt + "\n")
        print(f"\nwrote {a.report_out}")
    if a.export_refs or a.emit:
        refs = export_single_port_refs(npz, m)
        print("\nper-output single-port refs:")
        for o, p in refs.items():
            print(f"  {o}: {p}")
    if a.emit:
        em = emit_models(npz, m)
        print("\nper-output Verilog-A emit:")
        for o, r in em.items():
            print(f"  {o}: " + (str(r["va"]) if "va" in r else f"FAILED -- {r['error']}"))
