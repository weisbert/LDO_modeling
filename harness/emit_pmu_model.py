"""Component D -- emit the FINAL combined PMU behavioral model cell: ONE Verilog-A
module with the single input supply AVDD1P0 (LEFT), the N voltage rails + M current
biases (RIGHT), and the VSS ground (BOTTOM), compiled on the company box.

This consumes `fit_result` EXACTLY as returned by
harness.fit_multiport.fit_multiport(npz, manifest):
    {voltage: {o: {P, nfk, cout, esr, err, supplies}}, current: [rows], meta}

For EACH voltage output we REUSE the validated per-output transfer-function topology
of fit_model.emit_va (NO new transfer functions):
    Zout = (R_a + sL_a||R_pl) || (R_b + sL_b) || (ESR + 1/sCout)   [branches A/B/C]
    PSRR = i_c * Zout, i_c = G0 + sum_i Gi/(1+s/wi) [real bank]
           + one signed complex 2nd-order section (b0+b1 s)/(1+s/(Qw0)+(s/w0)^2)
    Noise= decoupled Norton @vout: white-R floor + len(nfk) R||C Lorentzians,
           transconducted into vout (gm sets amplitude) -> In*|Zout| = Sv.
Difference vs the single-LDO emit_va: the in-situ PMU model is captured at ONE
operating point, so the fitted params are baked in as CONSTANTS (no ln(iload)
interpolation, no DC-dropout PWL -- neither exists for a small-signal in-situ run).
Every output gets its OWN node namespace (`<o>_*`) so the N+M ports live in one module.

For EACH current bias we emit a behavioral current source with the fitted admittance
Y(s)=g0+sCp and a current-PSRR injection pi*(vin-vin_dc) (low order, from
fit_multiport._fit_admittance / _fit_cpsrr).

  from harness.emit_pmu_model import emit_pmu_va
  emit_pmu_va(fit_result, "PMU_model", "model/PMU_model.va",
              supply="AVDD1P0", ground="VSS")
"""
import pathlib

import numpy as np

TWO_PI = 2 * np.pi
NRk = 1e6  # fixed noise-section resistor (matches fit_model.NRk; gm sets amplitude)


# --------------------------------------------------------------------- helpers
def _nom_corner(P):
    """The corner whose params we bake into the single-OP combined model. The in-situ
    extraction carries ONE operating point; if several corner keys are present (a
    multi-corner report fed in) pick the middle one, matching fit_multiport's `nom`."""
    keys = list(P.keys())
    return keys[len(keys) // 2]


def _primary_supply_psrr(vfit, P_il, supply):
    """Return (G, Q) for the supply this combined model's single input drives.
    Prefer an exact name match in P_il['_psrr']; else fall back to the primary supply
    fit already promoted onto P_il (G0..w3 + pcb0..pcq)."""
    psrr = P_il.get("_psrr", {})
    if supply in psrr:
        return psrr[supply]
    # case-insensitive / role-key match (manifest supplies are role keys like '1p0',
    # the model port is the net name AVDD1P0 -- accept either)
    for k, v in psrr.items():
        if k.lower() in supply.lower() or supply.lower().endswith(k.lower()):
            return v
    if psrr:                                   # first fitted supply
        return next(iter(psrr.values()))
    # last resort: the params promoted onto P_il by fit_multiport
    G = [P_il["G0"], P_il["G1"], P_il["w1"], P_il["G2"], P_il["w2"],
         P_il["G3"], P_il["w3"]]
    Q = (P_il["pcb0"], P_il["pcb1"], P_il["pcw0"], P_il["pcq"])
    return G, Q


# --------------------------------------------------------- ln(iload) scheduling (T3)
# Mirror fit_model._poly/_body/_pexpr EXACTLY so an at-corner scheduled expr evaluates
# to the baked literal value (corner-exact) and off-corner loads stay inside the measured
# envelope (the same CLAMP_M/CLAMP_ADD clamp). Duplicated here (not imported) so the PMU
# emit stays self-contained and a single-port import side-effect (FM globals) is avoided.
CLAMP_M = 1.005      # log (magnitude) params: multiplicative margin on [min, max]
CLAMP_ADD = 0.005    # linear (signed) params: additive margin as a fraction of the span


def _sched_poly(u, y, logspace):
    """deg<=2 poly of y (or ln y) vs u=ln(iload) through the schedule corners. Matches
    fit_model._poly (np hi->lo coeff order)."""
    y = np.log(y) if logspace else y
    deg = min(len(u) - 1, 2)                # 3 corners -> quadratic; 2 -> linear
    return np.polyfit(u, y, deg)


def _sched_body(c, uvar):
    """ngspice/Verilog-A polynomial body in uvar for coeffs c (np hi->lo). Byte-identical
    in form to fit_model._body for the standard deg-2 case."""
    d = len(c) - 1
    parts = []
    for i, ci in enumerate(c):
        p = d - i
        if p == 0:
            parts.append(f"({ci:.6e})")
        elif p == 1:
            parts.append(f"({ci:.6e})*{uvar}")
        elif p == 2:
            parts.append(f"({ci:.6e})*{uvar}*{uvar}")
        else:
            parts.append(f"({ci:.6e})*{uvar}**{p}")
    return " + ".join(parts)


def _sched_expr(currents, vals, uvar, logspace):
    """CLAMPED VA expr for a scheduled param: deg<=2 poly in uvar=ln(iload) forced through
    the schedule corners, then min/max-clamped to the measured corner envelope (margin
    CLAMP_M/CLAMP_ADD). At a corner the poly == the baked literal AND the clamp is INACTIVE
    -> the at-corner value (hence the scorecard) is byte-exact. Mirrors fit_model._pexpr."""
    u = np.log(np.asarray(currents, dtype=float))
    y = np.asarray(vals, dtype=float)
    c = _sched_poly(u, y, logspace)
    body = _sched_body(c, uvar)
    if logspace:
        lo, hi = min(vals) / CLAMP_M, max(vals) * CLAMP_M
        return f"min(max(exp({body}),{lo:.6e}),{hi:.6e})"
    pad = CLAMP_ADD * (max(vals) - min(vals))
    lo, hi = min(vals) - pad, max(vals) + pad
    return f"min(max({body},{lo:.6e}),{hi:.6e})"


# per-rail scheduled params + their logspace flag -- EXACTLY fit_model.emit/emit_va's
# `specs` choices (magnitude params logspace=True; signed params False). The promoted
# per-load G0..w3 / pcb0..pcq live on each P[il] (fit_multiport line 134), so the schedule
# reads them per corner; the noise gn<k> are appended dynamically (len(nfk) varies).
_SCHED_SPEC_BASE = [("R_a", True), ("L_a", True), ("R_pl", True), ("R_b", True),
                    ("L_b", True),
                    ("G0", False), ("G1", False), ("w1", True), ("G2", False),
                    ("w2", True), ("G3", False), ("w3", True),
                    ("pcb0", False), ("pcb1", False), ("pcw0", True), ("pcq", True),
                    ("gnw", True), ("vreg", False)]


def _schedule_loads(vfit, P):
    """The ordered abscissa label list for this rail's ln(iload) schedule: the fit's
    `schedule_loads` (labels with a real, finite, nonzero, DISTINCT iv) when >1; else []
    (single OP / coverage-free -> bake literals). Guards against duplicate iv (a degenerate
    poly) by requiring >1 distinct current."""
    sl = list(vfit.get("schedule_loads", []) or [])
    sl = [il for il in sl if il in P]
    if len(sl) < 2:
        return []
    ivs = [float(P[il]["iv"]) for il in sl]
    if not all(np.isfinite(ivs)) or len(set(ivs)) < 2:
        return []
    return sl


def _voltage_block(o, vfit, supply, ground):
    """Render the Verilog-A statements + node/var declarations for ONE voltage rail.
    Reuses the EXACT contribution structure of fit_model.emit_va, namespaced by `<o>_`.

    TWO paths:
      * MULTI-LOAD (schedule_loads>1, real distinct iv): each load-dependent small-signal
        param becomes a CLAMPED quadratic in u=ln(iload_<o>) (a per-rail module parameter,
        nominal = the middle schedule load's iv). At each corner the scheduled expr == the
        per-corner fitted value (corner-exact); off-corner loads interpolate inside the
        measured envelope. The model now SPANS VALID_LOAD via ln(iload).
      * SINGLE-OP (one load / coverage-free): bake LITERALS exactly as before
        (byte-identical -> the stand-in tests stay green). This is the DEFAULT path."""
    P = vfit["P"]
    nfk = list(vfit["nfk"])
    Cout = float(vfit["cout"])
    ESR = float(vfit["esr"])
    cft = float(vfit.get("cft", 0.0))      # gated vin->vout feedthrough cap (0.0 -> not emitted)
    slew_a = float(vfit.get("slew_a", 0.0) or 0.0)   # branch-A regulation slew limit (0 -> not emitted)
    recov = vfit.get("recovery")           # gated overdamped 2nd-order Zout (None -> not emitted)
    sched = _schedule_loads(vfit, P)
    if sched:
        return _voltage_block_scheduled(o, vfit, supply, ground, sched, nfk, Cout, ESR, cft, slew_a, recov)
    il = _nom_corner(P)
    p = P[il]
    vreg = float(p["vreg"])
    G, Q = _primary_supply_psrr(vfit, p, supply)
    pcb0, pcb1, pcw0, pcq = (float(Q[0]), float(Q[1]), float(Q[2]), float(Q[3]))

    pre = o  # node/var namespace prefix
    # internal nodes for this rail (same roles as emit_va: vrg, nA, nC, nbb, np1-3,
    # vrf, ncs1/2, nw + Lorentzian noise nodes nk1..)
    nks = [f"{pre}_nk{k+1}" for k in range(len(nfk))]
    nodes = [f"{pre}_vrg", f"{pre}_nA", f"{pre}_nC", f"{pre}_nbb",
             f"{pre}_np1", f"{pre}_np2", f"{pre}_np3", f"{pre}_vrf",
             f"{pre}_ncs1", f"{pre}_ncs2", f"{pre}_nw"] + nks + _recov_nodes(pre, recov)

    # per-rail real vars (all baked literals assigned in initial_step) + derived RLC
    rvars = [f"{pre}_Ra", f"{pre}_La", f"{pre}_Rpl", f"{pre}_Rb", f"{pre}_Lb",
             f"{pre}_G0", f"{pre}_G1", f"{pre}_w1", f"{pre}_G2", f"{pre}_w2",
             f"{pre}_G3", f"{pre}_w3", f"{pre}_pcb0", f"{pre}_pcb1", f"{pre}_pcw0",
             f"{pre}_pcq", f"{pre}_gnw", f"{pre}_Cps",
             f"{pre}_pca1", f"{pre}_pca2", f"{pre}_Rpc", f"{pre}_Lpc", f"{pre}_Cpc",
             f"{pre}_gqb1"] + [f"{pre}_gn{k+1}" for k in range(len(nfk))]

    # vreg = the rail's regulated-output target. DEFAULT (no transient DC load-reg): a per-rail
    # MODULE PARAMETER (editable CDF field, default = fitted) so the user can retune the voltage
    # without re-fitting. When the fit derived a vreg(iload) DC load-reg schedule from the rail's
    # TRANSIENT steps (vfit['vreg_sched']), vreg becomes a load-scheduled real var instead -- a
    # clamped ln(iload) poly through the MEASURED settled-DC points -- so the model reproduces
    # the real load regulation (and the real DC level, not the 0.8 target). The single-OP default
    # (no transient) is byte-identical. (Mutually exclusive with the full multi-load AC schedule
    # above, which already load-schedules vreg from per-corner AC fits.)
    vreg_sched = vfit.get("vreg_sched")
    vreg_rvars, vreg_asg = [], []
    if vreg_sched:
        vcur = [float(x) for x in vreg_sched["currents"]]
        vval = [float(x) for x in vreg_sched["vregs"]]
        vnom = float(vreg_sched["i_nom"])
        vlo, vhi = min(vcur), max(vcur)
        uvar = f"{pre}_u"
        vreg_hdr = (f"parameter real iload_{pre} = {vnom:.6e};"
                    f"   // {o} load OP [A]; ln(iload_{pre}) drives the vreg DC load-reg schedule "
                    f"(VALID_LOAD [{vlo:g}..{vhi:g}])")
        vreg_rvars = [uvar, f"{pre}_vreg"]
        vreg_asg = [
            f"// {o} vreg DC load-reg: clamp load to [{vlo:g},{vhi:g}] A then vreg=poly(ln(iload))",
            f"{uvar} = ln(min(max(iload_{pre}, {vlo:.6e}), {vhi:.6e}));",
            f"{pre}_vreg = {_sched_expr(vcur, vval, uvar, False)};",
        ]
    else:
        vreg_hdr = (f"parameter real {pre}_vreg = {vreg:.6e};"
                    f"   // {o} regulated output target [V] (per-rail knob)")
    rvars = rvars + vreg_rvars
    Cn_par = "\n  ".join([vreg_hdr] + _cft_param(pre, cft) + _slew_param(pre, slew_a)
        + _recov_param(pre, recov) + [   # Cn = noise-corner caps (localparam)
        f"localparam real {pre}_Cn{k+1} = {1.0/(TWO_PI*nfk[k]*NRk):.6e};"
        f"   // {o} noise corner {nfk[k]:.4g} Hz"
        for k in range(len(nfk))])

    # --- initial_step assignments (vreg load-reg schedule first, then baked fitted params) ---
    asg = "\n      ".join(vreg_asg + [
        f"{pre}_Ra = {float(p['R_a']):.6e};",
        f"{pre}_La = {float(p['L_a']):.6e};",
        f"{pre}_Rpl = {float(p['R_pl']):.6e};",
        f"{pre}_Rb = {float(p['R_b']):.6e};",
        f"{pre}_Lb = {float(p['L_b']):.6e};",
        f"{pre}_G0 = {float(G[0]):.6e};",
        f"{pre}_G1 = {float(G[1]):.6e};  {pre}_w1 = {float(G[2]):.6e};",
        f"{pre}_G2 = {float(G[3]):.6e};  {pre}_w2 = {float(G[4]):.6e};",
        f"{pre}_G3 = {float(G[5]):.6e};  {pre}_w3 = {float(G[6]):.6e};",
        f"{pre}_pcb0 = {pcb0:.6e};  {pre}_pcb1 = {pcb1:.6e};",
        f"{pre}_pcw0 = {pcw0:.6e};  {pre}_pcq = {pcq:.6e};",
        f"{pre}_gnw = {float(p['gnw']):.6e};",
    ] + [f"{pre}_gn{k+1} = {float(p[f'gn{k+1}']):.6e};" for k in range(len(nfk))]
      + [
        f"{pre}_Cps = 1e-12;",
        f"{pre}_Cpc = 1e-12;",
        f"{pre}_pca1 = 1.0/({pre}_pcq*{pre}_pcw0);  {pre}_pca2 = 1.0/({pre}_pcw0*{pre}_pcw0);",
        f"{pre}_Rpc = {pre}_pca1/{pre}_Cpc;  {pre}_Lpc = {pre}_pca2/{pre}_Cpc;"
        f"  {pre}_gqb1 = {pre}_pcb1/{pre}_pca1;",
    ])

    body = _voltage_body(o, supply, ground, nfk, Cout, ESR, cft, slew_a, recov)
    return dict(nodes=nodes, rvars=rvars, params=Cn_par, asg=asg, body=body)


def _cft_param(pre, cft):
    """Per-rail gated vin->vout feedthrough cap param line(s). Empty list when cft<=0 so the
    header stays byte-identical to the pre-feedthrough emit (mirrors fit_model.emit_va's
    `if CFT > 0` gate). Editable knob (parameter, not localparam) like the old single-port .va."""
    if cft and cft > 0:
        return [f"parameter real {pre}_Cft = {float(cft):.6e};"
                f"   // {pre} gated vin->vout feedthrough cap [F]"]
    return []


def _slew_param(pre, sr):
    """Per-rail branch-A regulation SLEW-RATE limit param line(s). Empty list when sr is absent /
    <=0 / non-finite so the header stays byte-identical to the pre-slew emit (same opt-in gate as
    Cft / d2 / admittance zero). When present, the LDO pass-device large-signal current cannot
    change faster than SRa [A/s], which is what produces the deep load-transient UNDERSHOOT a
    pure-LTI Zout misses. Editable CDF knob (parameter, not localparam) -- the user tunes it in
    their TB. Two cautions are carried in the emitted comment: (1) a too-LOW SRa drives a
    non-physical deep undershoot (rail can swing past the supply rails); (2) the hard slew() pumps
    breakpoints, so a LONG transient (>> 1us) WITHOUT a sim maxstep can explode the time-step --
    the GHz use case already mandates a fine maxstep so it is unaffected."""
    if sr and sr > 0 and sr < 1e30:                       # finite + positive (reject inf / garbage)
        return [f"parameter real {pre}_SRa = {float(sr):.6e};"
                f"   // {pre} branch-A slew-rate limit [A/s] (large-signal undershoot). Physical"
                f" ~1e3..1e8; too low -> non-physical deep dip. For tran >> 1us set sim maxstep"
                f" <= ~1ns (hard slew() can blow up the time-step otherwise)."]
    return []


# anti-windup defaults (large-signal robustness; see _recov_param). The deadzone clamp is ZERO
# (value + slope) within +-VCL of vrg, so with VCL >> the real worst-case dip these never engage in
# any realistic transient -> DC/AC/in-envelope replay are untouched; they only bound the non-physical
# tank ring on huge out-of-envelope steps. Override per-rail via the recovery dict (Imax/Vcl/Gcl).
_RECOV_IMAX = 1.50e-02   # physical reg pass-current limit [A] (anti-windup on the slew target)
_RECOV_VCL  = 3.00e-01   # clamp deadzone half-width [V] (>> the ~88mV real worst-case dip)
_RECOV_GCL  = 2.00e+01   # clamp strength [A/V^2] beyond the deadzone


def _recov_ok(recov):
    """True iff a recovery (overdamped 2nd-order Zout) param dict is present + fully valid:
    all four of Lreg, Rreg, Cs, Rs finite + strictly positive. Mirrors the slew_a/cft opt-in
    gate -- any missing/<=0/garbage value -> False -> the rail emits byte-identical to today."""
    if not recov:
        return False
    try:
        Lreg = float(recov.get("Lreg", 0.0)); Rreg = float(recov.get("Rreg", 0.0))
        Cs = float(recov.get("Cs", 0.0)); Rs = float(recov.get("Rs", 0.0))
    except (TypeError, ValueError):
        return False
    vals = (Lreg, Rreg, Cs, Rs)
    return all(x > 0 and x < 1e30 for x in vals)


def _recov_nodes(pre, recov):
    """Extra internal nodes for the recovery branch (nB = slow-recovery-inductor node between
    branch A and vrg; nD = the AC snubber node). Empty list when the recovery term is OFF so
    the electrical declaration stays byte-identical."""
    return [f"{pre}_nB", f"{pre}_nD"] if _recov_ok(recov) else []


def _recov_param(pre, recov):
    """Per-rail recovery (overdamped 2nd-order Zout) param lines: the slow-recovery inductor
    (Lreg||Rreg) that stretches the post-dip Cout-recharge to a monotonic ~100ns climb, plus
    the DC-blocked Rs-Cs damping snubber that kills the slew-induced overshoot. Editable CDF
    knobs (parameter, not localparam). Empty list when OFF -> byte-identical header.

    These elements are LOSSLESS / DC-BLOCKED at DC (Lreg shorts, Cs opens) so Zdc is unchanged
    -> the regulated DC setpoint (vreg) is NOT moved; only the recovery SHAPE between the dip
    and steady state is reshaped. Same opt-in discipline as slew_a / Cft / d2.

    Also emits the ANTI-WINDUP knobs (Imax/Vcl/Gcl) that bound the loop's large-signal response:
    without them the slew-limited reg branch lags so long on an out-of-envelope harsh step that
    vout crashes and the lossless La/Lreg tank rings past the rails. Both clamps are zero inside
    the validated envelope (Vcl >> the real dip), so the in-envelope replay / DC / AC are
    untouched. Defaults from _RECOV_*; override per-rail via recovery['Imax'|'Vcl'|'Gcl']."""
    if not _recov_ok(recov):
        return []
    Lreg = float(recov["Lreg"]); Rreg = float(recov["Rreg"])
    Cs = float(recov["Cs"]); Rs = float(recov["Rs"])

    def _ov(key, default):
        try:
            v = float(recov.get(key))
        except (TypeError, ValueError):
            return default
        return v if (v > 0 and v < 1e30) else default
    Imax = _ov("Imax", _RECOV_IMAX); Vcl = _ov("Vcl", _RECOV_VCL); Gcl = _ov("Gcl", _RECOV_GCL)
    return [
        f"parameter real {pre}_Lreg = {Lreg:.6e};"
        f"   // {pre} slow-recovery inductor [H] (overdamped 2nd-order Zout; lossless at DC)",
        f"parameter real {pre}_Rreg = {Rreg:.6e};"
        f"   // {pre} recovery damp resistance [ohm] (sets the monotonic climb shape)",
        f"parameter real {pre}_Cs = {Cs:.6e};"
        f"   // {pre} recovery snubber cap [F] (AC-only damping, DC-blocked -> no droop)",
        f"parameter real {pre}_Rs = {Rs:.6e};"
        f"   // {pre} recovery snubber resistance [ohm]",
        f"parameter real {pre}_Imax = {Imax:.6e};"
        f"   // {pre} anti-windup reg pass-current limit [A]",
        f"parameter real {pre}_Vcl = {Vcl:.6e};"
        f"   // {pre} anti-windup clamp deadzone half-width [V] (>> the real dip)",
        f"parameter real {pre}_Gcl = {Gcl:.6e};"
        f"   // {pre} anti-windup clamp strength [A/V^2] beyond the deadzone",
    ]


def _branchA_reg(pre, slew_sr, recov=None):
    """Branch-A regulation contribution (node nA -> vrg through R_a). DEFAULT = the passive
    resistor `V(nA,vrg) <+ Ra*I(nA,vrg)` (byte-identical to the pre-slew emit). When a slew rate
    is fitted, the SAME R_a path is rate-limited via the built-in `slew()` operator:
    `I(nA,vrg) <+ slew(V(nA,vrg)/Ra, SRa)`. At DC and small signal slew()==identity, so it is
    EXACTLY the R_a resistor -> Zout/PSRR/noise (all DC+AC analyses) are unchanged; only large
    fast transients get rate-limited. SR->inf would also reduce to the resistor, but the absent
    case is gated at emit time so the default .va text is unchanged.

    When a `recov` (overdamped 2nd-order Zout) param set is present, a SLOW recovery inductor
    (Lreg||Rreg, lossless at DC) is inserted between branch A's node nA and the regulation node,
    and an AC-only Rs-Cs snubber (DC-blocked -> no droop) is added from vout to vrg. The
    regulation (slew or resistor) then feeds from the inserted node nB instead of nA. With recov
    OFF (the default) the inserted node + snubber are absent and the text is byte-identical.

    With recov ON the two ANTI-WINDUP bounds are also emitted (zero inside the validated envelope):
    the slew target current is clamped to +-Imax (a crashed vout can no longer demand absurd
    current), and a deadzone shunt clamp (zero value+slope within +-Vcl of vrg) bounds the
    non-physical La/Lreg tank ring on huge out-of-envelope steps."""
    use_recov = _recov_ok(recov)
    regnode = f"{pre}_nB" if use_recov else f"{pre}_nA"
    pre_lines = ""
    if use_recov:
        pre_lines = (f"// recovery branch A2: slow inductor (Lreg||Rreg), nA->nB (lossless at DC)\n"
                     f"    I({pre}_nA, {pre}_nB) <+ idt(V({pre}_nA, {pre}_nB))/{pre}_Lreg"
                     f" + V({pre}_nA, {pre}_nB)/{pre}_Rreg;\n    ")
    if slew_sr and slew_sr > 0 and slew_sr < 1e30:        # gate matches _slew_param (SRa declared)
        if use_recov:    # anti-windup: clamp the reg target current to +-Imax before slew()
            reg = (f"I({regnode}, {pre}_vrg) <+ slew(max(-{pre}_Imax, "
                   f"min({pre}_Imax, V({regnode}, {pre}_vrg)/{pre}_Ra)), {pre}_SRa);"
                   f"   // R_a regulation, slew-limited, target anti-windup clamped")
        else:
            reg = (f"I({regnode}, {pre}_vrg) <+ slew(V({regnode}, {pre}_vrg)/{pre}_Ra, {pre}_SRa);"
                   f"   // R_a regulation, large-signal slew-limited")
    else:
        reg = f"V({regnode}, {pre}_vrg) <+ {pre}_Ra*I({regnode}, {pre}_vrg);"
    post_lines = ""
    if use_recov:
        post_lines = (f"\n    // recovery snubber: series Rs-Cs (vout->vrg), DC-blocked, kills"
                      f" the post-dip overshoot\n"
                      f"    I({pre}, {pre}_nD) <+ {pre}_Cs*ddt(V({pre}, {pre}_nD));\n"
                      f"    V({pre}_nD, {pre}_vrg) <+ {pre}_Rs*I({pre}_nD, {pre}_vrg);\n"
                      f"    // deadzone anti-windup clamp: zero value+slope within +-Vcl of vrg"
                      f" (invisible to dip/AC), bounds the tank ring beyond it\n"
                      f"    I({pre}, {pre}_vrg) <+ (V({pre}, {pre}_vrg) >  {pre}_Vcl) ? "
                      f" {pre}_Gcl*(V({pre}, {pre}_vrg) - {pre}_Vcl)*(V({pre}, {pre}_vrg) - {pre}_Vcl)\n"
                      f"                  : (V({pre}, {pre}_vrg) < -{pre}_Vcl) ? "
                      f"-{pre}_Gcl*(V({pre}, {pre}_vrg) + {pre}_Vcl)*(V({pre}, {pre}_vrg) + {pre}_Vcl)\n"
                      f"                  : 0.0;")
    return pre_lines + reg + post_lines


def _cft_body(o, supply, cft):
    """Per-rail Cft feedthrough contribution (supply->{o}), emitted ONLY when cft>0. Mirrors
    the single-port emit_va `I(vin, vout) <+ Cft*ddt(V(vin, vout))` with vin=supply, vout={o}.
    Empty string when cft<=0 -> byte-identical body."""
    if cft and cft > 0:
        return (f"\n\n    // ---- gated vin->vout feedthrough cap (pass-device/package) ----"
                f"\n    I({supply}, {o}) <+ {o}_Cft*ddt(V({supply}, {o}));")
    return ""


def _voltage_body(o, supply, ground, nfk, Cout, ESR, cft=0.0, slew_sr=None, recov=None):
    """The per-rail VA contribution body -- IDENTICAL for the literal (single-OP) and the
    scheduled (multi-load) paths. Only the @(initial_step) assignments (`asg`) differ
    between them (literal numbers vs clamped ln(iload) exprs), so the topology/text of the
    contributions stays byte-equal. References the per-rail `<o>_*` real vars.

    `cft`>0 appends a gated supply->{o} feedthrough contribution (load-independent literal);
    cft<=0 (the default / single-OP) -> byte-identical to the pre-feedthrough body.
    `slew_sr`>0 rate-limits branch-A's R_a regulation current (large-signal undershoot);
    None/<=0 (the default) -> the passive R_a resistor, byte-identical to the pre-slew body."""
    pre = o
    nsec = "\n    ".join(
        f"I({pre}_nk{k+1}, {ground}) <+ V({pre}_nk{k+1}, {ground})/NRk"
        f" + {pre}_Cn{k+1}*ddt(V({pre}_nk{k+1}, {ground}));\n"
        f"    I({pre}_nk{k+1}, {ground}) <+ white_noise(4*`P_K*$temperature/NRk, \"{pre}_nk{k+1}\");\n"
        f"    I({o}, {ground})    <+ {pre}_gn{k+1}*V({pre}_nk{k+1}, {ground});"
        for k in range(len(nfk)))
    return f"""    // ============ voltage rail {o} (Cout={Cout:.3e}F ESR={ESR:.3e}ohm) ============
    V({pre}_vrf, {ground}) <+ vdc_{supply};       // supply DC reference for {o} PSRR
    V({pre}_vrg, {ground}) <+ {pre}_vreg;          // {o} regulated output reference

    // Zout branch C: series Cout + ESR (vout -> vrg)
    I({o}, {pre}_nC) <+ {Cout:.6e}*ddt(V({o}, {pre}_nC));
    V({pre}_nC, {pre}_vrg) <+ {ESR:.6e}*I({pre}_nC, {pre}_vrg);

    // Zout branch B: optional 2nd R-L (R_b->inf disables it)
    I({o}, {pre}_nbb) <+ idt(V({o}, {pre}_nbb))/{pre}_Lb;
    V({pre}_nbb, {pre}_vrg) <+ {pre}_Rb*I({pre}_nbb, {pre}_vrg);

    // Zout branch A: (L_a || R_pl) + R_a
    I({o}, {pre}_nA) <+ idt(V({o}, {pre}_nA))/{pre}_La + V({o}, {pre}_nA)/{pre}_Rpl;
    {_branchA_reg(pre, slew_sr, recov)}

    // intrinsic output noise: decoupled Norton current @{o} (white floor + {len(nfk)} Lorentzians)
    I({pre}_nw, {ground}) <+ V({pre}_nw, {ground})/NRk;
    I({pre}_nw, {ground}) <+ white_noise(4*`P_K*$temperature/NRk, "{pre}_nw");
    I({o}, {ground}) <+ {pre}_gnw*V({pre}_nw, {ground});
    {nsec}

    // PSRR path: i_c = G0 + sum Gi*LP_i(vin-vdd) into {o} (x Zout)
    I({supply}, {pre}_np1) <+ V({supply}, {pre}_np1)*({pre}_w1*{pre}_Cps);
    I({pre}_np1, {pre}_vrf) <+ {pre}_Cps*ddt(V({pre}_np1, {pre}_vrf));
    I({supply}, {pre}_np2) <+ V({supply}, {pre}_np2)*({pre}_w2*{pre}_Cps);
    I({pre}_np2, {pre}_vrf) <+ {pre}_Cps*ddt(V({pre}_np2, {pre}_vrf));
    I({supply}, {pre}_np3) <+ V({supply}, {pre}_np3)*({pre}_w3*{pre}_Cps);
    I({pre}_np3, {pre}_vrf) <+ {pre}_Cps*ddt(V({pre}_np3, {pre}_vrf));
    I({o}, {ground}) <+ -({pre}_G0*V({supply}, {pre}_vrf) + {pre}_G1*V({pre}_np1, {pre}_vrf)
                   + {pre}_G2*V({pre}_np2, {pre}_vrf) + {pre}_G3*V({pre}_np3, {pre}_vrf));

    // PSRR complex-conjugate 2nd-order section (non-min-phase / notch phase; inert if pcb0=pcb1=0)
    I({supply}, {pre}_ncs1) <+ V({supply}, {pre}_ncs1)/{pre}_Rpc;
    I({pre}_ncs1, {pre}_ncs2) <+ idt(V({pre}_ncs1, {pre}_ncs2))/{pre}_Lpc;
    I({pre}_ncs2, {pre}_vrf) <+ {pre}_Cpc*ddt(V({pre}_ncs2, {pre}_vrf));
    I({o}, {ground}) <+ -({pre}_pcb0*V({pre}_ncs2, {pre}_vrf) + {pre}_gqb1*V({supply}, {pre}_ncs1));{_cft_body(o, supply, cft)}
"""


def _voltage_block_scheduled(o, vfit, supply, ground, sched, nfk, Cout, ESR, cft=0.0, slew_a=0.0, recov=None):
    """MULTI-LOAD path: each load-dependent small-signal param is a CLAMPED quadratic in
    u=ln(iload_<o>) (a per-rail module parameter, nominal = the middle schedule load's iv).
    At a schedule corner the scheduled expr == that corner's fitted value (corner-exact);
    off-corner loads interpolate inside the measured envelope. The body is IDENTICAL to the
    literal path; only the @(initial_step) `asg` differ (clamped exprs instead of numbers)
    and a `u`/`iload_<o>` are introduced. Reuses the proven fit_model interpolation
    machinery via _sched_expr (deg<=2 poly through the corners + the same envelope clamp)."""
    pre = o
    P = vfit["P"]
    currents = [float(P[il]["iv"]) for il in sched]
    iload_nom = currents[len(currents) // 2]   # the middle schedule load's iv
    ilo, ihi = min(currents), max(currents)
    uvar = f"{pre}_u"

    # promoted per-load PSRR params (G0..w3, pcb0..pcq) live on each P[il]; the primary-
    # supply promotion is consistent across loads (fit_multiport line 134). Schedule the
    # promoted fields directly so the per-corner G/Q are what the AC fit produced.
    spec = list(_SCHED_SPEC_BASE) + [(f"gn{k+1}", True) for k in range(len(nfk))]

    def _vals(key):
        return [float(P[il][key]) for il in sched]

    nks = [f"{pre}_nk{k+1}" for k in range(len(nfk))]
    nodes = [f"{pre}_vrg", f"{pre}_nA", f"{pre}_nC", f"{pre}_nbb",
             f"{pre}_np1", f"{pre}_np2", f"{pre}_np3", f"{pre}_vrf",
             f"{pre}_ncs1", f"{pre}_ncs2", f"{pre}_nw"] + nks + _recov_nodes(pre, recov)

    # u is a per-rail real var (clamped ln of the rail's iload param); all scheduled params
    # follow exactly the literal block's rvar set.
    rvars = [uvar,
             f"{pre}_Ra", f"{pre}_La", f"{pre}_Rpl", f"{pre}_Rb", f"{pre}_Lb",
             f"{pre}_G0", f"{pre}_G1", f"{pre}_w1", f"{pre}_G2", f"{pre}_w2",
             f"{pre}_G3", f"{pre}_w3", f"{pre}_pcb0", f"{pre}_pcb1", f"{pre}_pcw0",
             f"{pre}_pcq", f"{pre}_gnw", f"{pre}_vreg", f"{pre}_Cps",
             f"{pre}_pca1", f"{pre}_pca2", f"{pre}_Rpc", f"{pre}_Lpc", f"{pre}_Cpc",
             f"{pre}_gqb1"] + [f"{pre}_gn{k+1}" for k in range(len(nfk))]

    # the per-rail iload schedule parameter (nominal = middle schedule load) + noise-corner
    # capacitor params (same as the literal block).
    sched_par = (f"parameter real iload_{pre} = {iload_nom:.6e};"
                 f"   // {o} load OP [A]; ln(iload_{pre}) drives the param schedule "
                 f"(VALID_LOAD [{ilo:g}..{ihi:g}])")
    Cn_par = sched_par + "".join(             # Cn = internal noise-corner caps (localparam)
        f"\n  {ln}" for ln in _cft_param(pre, cft) + _slew_param(pre, slew_a)
        + _recov_param(pre, recov)) + "".join(
        f"\n  localparam real {pre}_Cn{k+1} = {1.0/(TWO_PI*nfk[k]*NRk):.6e};"
        f"   // {o} noise corner {nfk[k]:.4g} Hz"
        for k in range(len(nfk)))

    # map our rvar names to the P-keys (the VA var is named <pre>_Ra etc; the fitted key is
    # R_a etc). Build the scheduled-assignment lines in the SAME ORDER the literal block
    # writes them so the .va layout stays parallel.
    keymap = {"R_a": "Ra", "L_a": "La", "R_pl": "Rpl", "R_b": "Rb", "L_b": "Lb",
              "G0": "G0", "G1": "G1", "w1": "w1", "G2": "G2", "w2": "w2",
              "G3": "G3", "w3": "w3", "pcb0": "pcb0", "pcb1": "pcb1",
              "pcw0": "pcw0", "pcq": "pcq", "gnw": "gnw", "vreg": "vreg"}
    for k in range(len(nfk)):
        keymap[f"gn{k+1}"] = f"gn{k+1}"

    sch_lines = [
        f"// clamp the rail load to the schedule envelope [{ilo:g},{ihi:g}] A then u=ln(iload)",
        f"{uvar} = ln(min(max(iload_{pre}, {ilo:.6e}), {ihi:.6e}));",
    ]
    for key, logspace in spec:
        expr = _sched_expr(currents, _vals(key), uvar, logspace)
        sch_lines.append(f"{pre}_{keymap[key]} = {expr};")

    asg = "\n      ".join(sch_lines + [
        f"{pre}_Cps = 1e-12;",
        f"{pre}_Cpc = 1e-12;",
        f"{pre}_pca1 = 1.0/({pre}_pcq*{pre}_pcw0);  {pre}_pca2 = 1.0/({pre}_pcw0*{pre}_pcw0);",
        f"{pre}_Rpc = {pre}_pca1/{pre}_Cpc;  {pre}_Lpc = {pre}_pca2/{pre}_Cpc;"
        f"  {pre}_gqb1 = {pre}_pcb1/{pre}_pca1;",
    ])

    body = _voltage_body(o, supply, ground, nfk, Cout, ESR, cft, slew_a, recov)
    return dict(nodes=nodes, rvars=rvars, params=Cn_par, asg=asg, body=body,
                scheduled=True, valid_load=(ilo, ihi))


def current_crow_from_isrc_fit(p, pin=None, tnom_c=55.0):
    """harness/fit_isrc.fit_isrc(...) param dict -> a `crow` that _current_block emits
    as the LARGE-SIGNAL VA form. Connects the offline-validated behavioral current model
    (8/8 vs the MOS-GT, harness/crossval_isrc.py) to this Cadence VA emit; the in-situ
    fit_multiport will produce the same fields on the box. `tnom_c` = the nominal temp the
    Idc/didt fit is referenced to (the manifest's tnom_c / middle of its temps)."""
    return dict(sink=p["name"], pin=pin or p["name"], pol=p["pol"],
                idc55=p["idc55"], didt=p["didt"], d2=p.get("d2", 0.0), g0=p["g0"], vc=p["vc"],
                gdd=p["gdd"], vknee=p["vknee"], knee_p=p["knee_p"],
                knee_side=p.get("knee_side"), vhi=p.get("vhi"),   # carry the data-detected knee
                Cp=p["cp"], y_wz=p.get("y_wz"), y_wp=p.get("y_wp"),  # 2nd-order admittance zero
                in_white=p["in_white"], in_kf=p["in_kf"], tnom_c=tnom_c)


def _current_block(o, crow, supply, ground):
    """Render the Verilog-A for ONE current bias output. Dispatches:
      * LARGE-SIGNAL form (Idc(T) + I-V compliance knee + g0 + Cp + SIGNED current-PSRR
        + white/flicker noise) when the fit carries `idc55` -- the offline-validated
        behavioral current model (harness/emit_isrc.py, 8/8 vs MOS-GT);
      * else the LEGACY AC-only form (admittance Y=g0+sCp + |PI(0)| magnitude PSRR),
        kept for backward compatibility with single-OP small-signal fits."""
    if "idc55" in crow:
        return _current_block_largesignal(o, crow, supply, ground)
    return _current_block_legacy(o, crow, supply, ground)


def _current_block_largesignal(o, crow, supply, ground):
    """I_pin = ( Idc(T) + g0*(Vo-vc) + gdd*(Vsup-vdc) ) * tanh( (knee_arg/Vk)^p )
              + Cp*ddt(Vo) + white_noise + flicker_noise.
    SINK draws at {o}->ground; SOURCE injects supply->{o}. $temperature is KELVIN, so the
    Idc nominal at 55 C uses 328.15 K. gdd sign is folded for the drive-node convention
    (sink probe reads -I_pin), exactly as validated in harness/emit_isrc.py. The knee base
    is sqrt-floored so the OP Jacobian of (arg/Vk)^p stays finite at Vo=0 when p<1."""
    pre = o
    pol = crow.get("pol", "sink")
    idc55 = float(crow["idc55"]); didt = float(crow.get("didt", 0.0))
    d2 = float(crow.get("d2", 0.0))                       # 2nd-order Idc(T) curvature [A/K^2]
    g0 = float(crow.get("g0", 0.0)); vc = float(crow.get("vc", 0.0))
    vk = float(crow.get("vknee", 0.1)); kp = float(crow.get("knee_p", 1.0))
    cp = float(crow.get("Cp", 0.0)); gdd = float(crow.get("gdd", 0.0))
    gdd_eff = -gdd if pol == "sink" else gdd
    inw2 = float(crow.get("in_white", 0.0)) ** 2
    kf = float(crow.get("in_kf", 0.0))
    tref_k = float(crow.get("tnom_c", 55.0)) + 273.15    # $temperature is KELVIN; user-set nominal
    vhi = float(crow.get("vhi", 1.05))                   # fitted high-side compliance ceiling (VDD0 default)
    # DRIVE direction follows pol (sink draws {o}->ground, source injects supply->{o}); the
    # compliance KNEE follows the DATA-DETECTED side (decoupled from pol): a sink can have a
    # high-side ceiling knee (the real WuR refs), and a flat ref has NO knee.
    side = crow.get("knee_side") or ("lo" if pol == "sink" else "hi")
    # Idc(T) law: linear by default; the 2nd-order term is emitted ONLY when d2 != 0 so a
    # linear model stays SUBSTRING-IDENTICAL to the pre-curvature .va (no _d2 var, no '+ 0.0*' tail).
    trefs = f"{tref_k:g}"
    temp_term = f"{pre}_idc55 + {pre}_didt*($temperature - {trefs})"
    d2_var, d2_asg = [], ""
    if d2 != 0.0:
        d2_var = [f"{pre}_d2"]
        d2_asg = f"  {pre}_d2 = {d2:.6e};"
        temp_term += f" + {pre}_d2*($temperature - {trefs})*($temperature - {trefs})"
    # 2nd-order output-admittance zero (cascode/Wilson): the fitted Y=g0*(1+s/wz)/(1+s/wp)+sCp
    # is realized as a PASSIVE lossy series-RC branch (cap Cz to an internal node, resistor Rz
    # to ground) in parallel with g0+sCp -- both Cz,Rz>0 when wp>wz, so it stays passive and
    # converges. Emitted ONLY when y_wz/y_wp are present (so a flat g0+sCp sink is byte-identical:
    # no _nz node, no Cz/Rz vars, no extra cards). Same opt-in pattern as the d2 Idc(T) term.
    ywz = crow.get("y_wz"); ywp = crow.get("y_wp")
    zero_node, zero_var, zero_asg, zero_body = [], [], "", ""
    if ywz and ywp and ywp > ywz and g0 != 0.0:
        g0a = abs(g0)
        Rz = 1.0 / (g0a * (ywp / ywz - 1.0))             # branch loss: Re(Y) HF rise = 1/Rz
        Cz = g0a * (ywp - ywz) / (ywz * ywp)             # branch cap: pole wp = 1/(Cz*Rz)
        zero_node = [f"{pre}_nz"]
        zero_var = [f"{pre}_Cz", f"{pre}_Rz"]
        zero_asg = f"  {pre}_Cz = {Cz:.6e};  {pre}_Rz = {Rz:.6e};"
        zero_body = (
            f"    I({o}, {pre}_nz) <+ {pre}_Cz*ddt(V({o}, {pre}_nz));"
            f"        // 2nd-order admittance zero (cascode/Wilson): lossy series C-R\n"
            f"    I({pre}_nz, {ground}) <+ V({pre}_nz, {ground})/{pre}_Rz;\n")
    rvars = [f"{pre}_didt", f"{pre}_g0", f"{pre}_vc", f"{pre}_gdd",
             f"{pre}_vk", f"{pre}_kp", f"{pre}_vhi", f"{pre}_Cp", f"{pre}_inw2", f"{pre}_kf"] \
        + d2_var + zero_var
    # {pre}_idc55 is the bias's DC output current at Tnom -> a per-bias MODULE PARAMETER (an
    # editable CDF field, default = fitted value) so the user can retune each bias current.
    idc_par = (f"parameter real {pre}_idc55 = {idc55:.6e};"
               f"   // {o} DC bias current @ {float(crow.get('tnom_c', 55.0)):g}C [A] (per-bias knob)")
    asg = (f"{pre}_didt = {didt:.6e};  {pre}_g0 = {g0:.6e};  "
           f"{pre}_vc = {vc:.6g};  {pre}_gdd = {gdd_eff:.6e};  {pre}_vk = {vk:.6g};  "
           f"{pre}_kp = {kp:.6g};  {pre}_vhi = {vhi:.6g};  {pre}_Cp = {cp:.6e};  "
           f"{pre}_inw2 = {inw2:.6e};  {pre}_kf = {kf:.6e};" + d2_asg + zero_asg)
    drive = f"I({o}, {ground})" if pol == "sink" else f"I({supply}, {o})"
    if side == "none":                                   # flat ref: no compliance knee in range
        gate_expr = "1.0"
    elif side == "hi":                                   # high-side ceiling knee at the fitted vhi
        karg = f"sqrt(({pre}_vhi - V({o},{ground}))*({pre}_vhi - V({o},{ground})) + 1e-12)"
        gate_expr = f"tanh(pow({karg}/{pre}_vk, {pre}_kp))"
    else:                                                # 'lo': low-side knee (Vo->0), legacy NMOS sink
        karg = f"sqrt(V({o},{ground})*V({o},{ground}) + 1e-12)"
        gate_expr = f"tanh(pow({karg}/{pre}_vk, {pre}_kp))"
    body = f"""    // ====== current bias {o} ({pol}, {side}-knee: Idc(T)+I-V knee+g0+Cp+signed PSRR+noise) ======
    {drive} <+ ({temp_term}
                  + {pre}_g0*(V({o},{ground}) - {pre}_vc)
                  + {pre}_gdd*(V({supply},{ground}) - vdc_{supply}))
                 * {gate_expr};
    I({o}, {ground}) <+ {pre}_Cp*ddt(V({o}, {ground}));   // output cap (Y imag part)
{zero_body}    I({o}, {ground}) <+ white_noise({pre}_inw2, "{pre}_wht");
    I({o}, {ground}) <+ flicker_noise({pre}_kf, 1.0, "{pre}_flk");
"""
    return dict(rvars=rvars, asg=asg, body=body, nodes=zero_node, params=idc_par)


def _current_block_legacy(o, crow, supply, ground):
    """Legacy AC-only behavioral current source: fitted admittance Y(s)=g0+sCp and a
    current-PSRR injection pi*(vin-vin_dc). crow: {sink,il,g0,Cp,yrms,ydc,pi:{s:{rms,dc}}}.
    pi_dc is a MAGNITUDE (|PI(0)|), so current-PSRR sign/phase are not modeled."""
    pre = o
    g0 = float(crow.get("g0", 0.0))
    Cp = float(crow.get("Cp", 0.0))
    # current-PSRR DC magnitude for the model's input supply (role-key or net match)
    pis = crow.get("pi", {})
    pi_dc = 0.0
    if pis:
        chosen = None
        for k in pis:
            if k.lower() in supply.lower() or supply.lower().endswith(k.lower()):
                chosen = k
                break
        if chosen is None:
            chosen = next(iter(pis))
        pi_dc = float(pis[chosen].get("dc", 0.0))
    rvars = [f"{pre}_g0", f"{pre}_Cp", f"{pre}_pidc"]
    asg = (f"{pre}_g0 = {g0:.6e};  {pre}_Cp = {Cp:.6e};  "
           f"{pre}_pidc = {pi_dc:.6e};")
    body = f"""    // ============ current bias {o} (Y=g0+sCp, current-PSRR pi_dc) ============
    // admittance + parasitic cap (sink output conductance), referenced to {ground}
    I({o}, {ground}) <+ {pre}_g0*V({o}, {ground}) + {pre}_Cp*ddt(V({o}, {ground}));
    // current-PSRR: supply ripple injects pi_dc*(vin - vin_dc) into the bias node
    I({o}, {ground}) <+ {pre}_pidc*(V({supply}, {ground}) - vdc_{supply});
"""
    return dict(rvars=rvars, asg=asg, body=body)


# --------------------------------------------------------------------- sanity
def va_sanity(va_text, supply, v_outs, i_outs, ground):
    """STATIC sanity check on the emitted .va. Returns (ok, problems). Verifies:
      - module/endmodule balance (exactly one of each, module before endmodule);
      - begin/end (analog block) balance;
      - the port list declares exactly the 1-input / N+M-output / ground interface
        (the supply, every voltage + current out, and the ground(s) appear in the
        module header port list and in the input/output/inout direction decls).
    `ground` may be a single net name (str) or a list of split-ground nets."""
    grounds = [ground] if isinstance(ground, str) else list(ground)
    problems = []
    nmod = va_text.count("\nmodule ") + (1 if va_text.startswith("module ") else 0)
    nend = va_text.count("endmodule")
    if nmod != 1:
        problems.append(f"expected 1 module, found {nmod}")
    if nend != 1:
        problems.append(f"expected 1 endmodule, found {nend}")
    if "module " in va_text and "endmodule" in va_text:
        if va_text.index("module ") > va_text.rindex("endmodule"):
            problems.append("module appears after endmodule")
    # begin/end balance (word-boundary begin; every endmodule line also has 'end')
    import re
    nbegin = len(re.findall(r"\bbegin\b", va_text))
    nendkw = len(re.findall(r"\bend\b", va_text))     # counts 'end' but not 'endmodule'
    if nbegin != nendkw:
        problems.append(f"begin/end mismatch: {nbegin} begin vs {nendkw} end")
    # port header: module NAME(p1, p2, ...);
    mh = re.search(r"module\s+\w+\s*\(([^)]*)\)\s*;", va_text)
    if not mh:
        problems.append("could not parse module port list")
        return (not problems), problems
    ports = [t.strip() for t in mh.group(1).split(",") if t.strip()]
    expected = [supply] + list(v_outs) + list(i_outs) + grounds
    if ports != expected:
        problems.append(f"port list {ports} != expected {expected}")
    # direction decls: supply input, outs output, ground inout
    def _decl(kind):
        out = set()
        for m in re.finditer(rf"\b{kind}\s+([^;]+);", va_text):
            out |= {t.strip() for t in m.group(1).split(",") if t.strip()}
        return out
    ins, outs, inouts = _decl("input"), _decl("output"), _decl("inout")
    if supply not in ins:
        problems.append(f"supply {supply} not declared 'input'")
    for o in list(v_outs) + list(i_outs):
        if o not in outs:
            problems.append(f"output {o} not declared 'output'")
    for g in grounds:
        if g not in inouts:
            problems.append(f"ground {g} not declared 'inout'")
    return (not problems), problems


def va_format_report(va_text, supply, v_outs, i_outs, grounds):
    """VALIDATE the emitted .va FORMAT for a copy-paste log (answers 'verify the target file'):
    structural va_sanity + module/port/param roster + per-rail emit SHAPE (vreg load-reg SCHEDULE
    vs baked literal, Cft on/off) + per-sink terms. Static text scan -- never raises."""
    import re
    grounds = [grounds] if isinstance(grounds, str) else list(grounds)
    L = []
    try:
        ok, probs = va_sanity(va_text, supply, v_outs, i_outs, grounds)
        L.append(f"[emit] --- TARGET .va VALIDATION: {'OK (structurally valid)' if ok else 'PROBLEMS'} ---")
        for p in probs:
            L.append(f"[emit]   ! {p}")
        mod = re.search(r"\bmodule\s+(\w+)\s*\(", va_text)
        params = re.findall(r"parameter\s+real\s+(\w+)\s*=", va_text)
        L.append(f"[emit]   module : {mod.group(1) if mod else '?'}  (supply {supply}, "
                 f"{len(v_outs)} rail(s), {len(i_outs)} sink(s), grounds {grounds})")
        L.append(f"[emit]   params : {len(params)} -> {', '.join(params) if params else '(none)'}")
        for o in v_outs:
            sched = (f"iload_{o}" in va_text) and (f"{o}_vreg = min(max(" in va_text)
            baked = f"parameter real {o}_vreg =" in va_text
            cft = f"{o}_Cft" in va_text
            vreg = "load-reg SCHEDULE" if sched else ("BAKED literal" if baked else "?")
            L.append(f"[emit]   rail {o}: vreg={vreg}; Cft={'on' if cft else 'off'}")
        for c in i_outs:
            L.append(f"[emit]   sink {c}: I-V/g0/PSRR/noise"
                     + ("; admittance-zero=on" if f"{c}_Cz" in va_text else ""))
    except Exception as e:                             # noqa: BLE001 -- a validator must never break emit
        L.append(f"[emit]   <validation unavailable: {e}>")
    return "\n".join(L)


# --------------------------------------------------------------------- emit
def _coverage_banner(fit_result, provenance):
    """Build the .va COVERAGE/OP/VALID_LOAD provenance comment line (HANDOFF §4). EVERY
    emitted model is self-documenting at the model boundary. Field source priority:
    explicit `provenance` dict {tier, op_iload, op_temp, valid_load:(lo,hi)} > fit_result
    ['meta'] (coverage_tier/op_iload/op_temp/valid_load, set by fit_multiport) > a clear
    'unspecified'/'?' default. The banner is a COMMENT (no VA semantics) -> va_sanity
    still passes. NOTE: emit_pmu_va is a pure LTI + large-signal current core today -- it
    emits NO tier>=T2 dropout/slew term; the banner documents that SCOPE (the in-situ
    dropout/slew emission is stage 2b)."""
    prov = provenance or {}
    meta = fit_result.get("meta", {})

    def _pick(prov_key, meta_key):
        if prov.get(prov_key) is not None:
            return prov[prov_key]
        return meta.get(meta_key)

    tier = _pick("tier", "coverage_tier")
    op_iload = _pick("op_iload", "op_iload")
    op_temp = _pick("op_temp", "op_temp")
    valid_load = _pick("valid_load", "valid_load")
    tier_s = tier if tier else "unspecified"
    il_s = f"{op_iload:g}" if op_iload is not None else "?"
    t_s = f"{op_temp:g}" if op_temp is not None else "?"
    op_s = f"{il_s}@{t_s}"
    if valid_load is not None:
        lo, hi = valid_load
        vl_s = f"[{lo:g}..{hi:g}]"
    else:
        vl_s = "?"
    return f"// COVERAGE={tier_s}  OP={op_s}  VALID_LOAD={vl_s}"


def model_output_ports(fit_result):
    """Ordered (voltage rails, current biases) port names of the model -- the ports a per-port
    ground can be bound to. Mirrors emit_pmu_va's derivation so a UI can list them up front."""
    voltage = fit_result["voltage"]
    v_outs = [voltage[rk].get("pin", rk) for rk in voltage]
    seen, i_outs = set(), []
    for r in fit_result.get("current", []):
        s = r["sink"]
        if s not in seen:
            i_outs.append(r.get("pin", r["sink"]))
            seen.add(s)
    return v_outs, i_outs


def emit_pmu_va(fit_result, cell_name, va_path, supply="AVDD1P0", ground="VSS",
                supply_dc=None, tnom_c=None, provenance=None, port_grounds=None):
    """Emit ONE combined Verilog-A module `cell_name` for the whole PMU: input `supply`
    (LEFT), every voltage rail + current bias from fit_result (RIGHT), `ground` (BOTTOM).

    fit_result := harness.fit_multiport.fit_multiport(npz, manifest) output.
    `provenance` (optional) := {tier, op_iload, op_temp, valid_load:(lo,hi)} -> stamps the
    header COVERAGE/OP/VALID_LOAD banner (else sourced from fit_result['meta'], else a clear
    default). `port_grounds` (optional) := {port_pin: ground_net} SPLIT-GROUND map -- each
    listed port returns to its own ground net instead of the single `ground`; the distinct
    nets become extra module ground pins. Ports absent from the map (or port_grounds=None)
    use `ground`, so the default path is byte-identical. Returns the written va_path
    (pathlib.Path). Raises ValueError if the emitted text fails the static VA sanity check
    (so a malformed interface never reaches the box)."""
    va_path = pathlib.Path(va_path)
    voltage = fit_result["voltage"]
    current = fit_result.get("current", [])
    v_keys = list(voltage.keys())
    # The MODULE PORT names are the designer's GUI symbol pin names (so the emitted .va binds
    # to the pmuBuildModelCell symbol whose pins ARE those names). fit_multiport propagates
    # them as 'pin'; fall back to the internal role key when absent (e.g. the stand-in
    # manifest carries no 'pin' field -> ports stay 'pll'/'vco').
    v_outs = [voltage[rk].get("pin", rk) for rk in v_keys]

    # one current row per sink (the fit carries one row per (sink, load); single-OP ->
    # one load -> one row per sink). Keep first-seen order = manifest order.
    crows, seen = [], set()
    for r in current:
        s = r["sink"]
        if s not in seen:
            crows.append(r)
            seen.add(s)
    i_outs = [r.get("pin", r["sink"]) for r in crows]

    # supply DC reference baked as vdc_<supply>. MUST match the supply used during
    # characterization: the SOURCE compliance knee (vdc - Vo) and the PSRR term
    # (Vsup - vdc) both pin to it -- a wrong vdc shifts the source knee and adds a
    # gdd*delta DC offset. Priority: explicit kwarg > fit meta > contract default 1.0 V.
    meta = fit_result.get("meta", {})
    if supply_dc is None:
        supply_dc = float(meta.get("supply_dc", 1.0))
    # nominal temp the Idc(T) fit is referenced to. Precedence: explicit kwarg OVERRIDES
    # every crow; else fit meta FILLS crows that lack it (the box path -- fit_multiport
    # doesn't set it yet); else each crow keeps its own (the bridge sets it from the fit);
    # else _current_block_largesignal defaults to 55 C.
    if tnom_c is not None:
        for r in crows:
            r["tnom_c"] = float(tnom_c)
    elif meta.get("tnom_c") is not None:
        for r in crows:
            r.setdefault("tnom_c", float(meta["tnom_c"]))

    # per-port ground assignment: port_grounds maps a PORT (pin) name -> its ground net; any
    # port absent uses the single `ground`, so the default (no map) path stays byte-identical.
    # The DISTINCT grounds actually referenced (first-seen order) become the module ground pins
    # -> one entry collapses to the old [ground] interface; >1 = split ground (e.g. VSS_VCO).
    pg = dict(port_grounds or {})
    v_gnd = [pg.get(p) or ground for p in v_outs]
    i_gnd = [pg.get(p) or ground for p in i_outs]
    grounds = []
    for g in v_gnd + i_gnd:
        if g not in grounds:
            grounds.append(g)
    if not grounds:
        grounds = [ground]

    # pass the PIN name (port) as the block's `o` -- it is used as BOTH the port reference
    # and the internal node namespace prefix; pin names are unique + valid VA identifiers.
    vblocks = [_voltage_block(port, voltage[rk], supply, g)
               for rk, port, g in zip(v_keys, v_outs, v_gnd)]
    cblocks = [_current_block(port, r, supply, g) for port, r, g in zip(i_outs, crows, i_gnd)]

    # assemble the single module ----------------------------------------------------
    all_ports = [supply] + v_outs + i_outs + grounds
    internal_nodes = []
    for vb in vblocks:
        internal_nodes += vb["nodes"]
    for cb in cblocks:                         # current-block 2nd-order admittance-zero node(s)
        internal_nodes += cb.get("nodes", [])
    all_elec = all_ports + internal_nodes

    rvars = []
    for vb in vblocks:
        rvars += vb["rvars"]
    for cb in cblocks:
        rvars += cb["rvars"]

    # module-level parameters (editable CDF fields): per-rail vreg + noise corners (voltage
    # blocks) and per-bias idc (current blocks). vdc_<supply>/NRk are added in the header.
    _param_lines = [vb["params"] for vb in vblocks if vb.get("params")]
    _param_lines += [cb["params"] for cb in cblocks if cb.get("params")]
    cn_params = "\n  ".join(_param_lines)
    asgs = "\n      ".join([vb["asg"] for vb in vblocks] + [cb["asg"] for cb in cblocks])
    bodies = "\n".join([vb["body"] for vb in vblocks] + [cb["body"] for cb in cblocks])

    # wrap long electrical/real decls so no single line is unwieldy
    def _wrap(decl_kw, names):
        lines, cur = [], decl_kw + " "
        for i, n in enumerate(names):
            piece = n + ("," if i < len(names) - 1 else ";")
            if len(cur) + len(piece) + 1 > 90:
                lines.append(cur.rstrip())
                cur = "    " + piece + " "
            else:
                cur += piece + " "
        lines.append(cur.rstrip())
        return "\n  ".join(lines)

    elec_decl = _wrap("electrical", all_elec)
    rvar_decl = _wrap("real", rvars)

    banner = _coverage_banner(fit_result, provenance)

    # rails whose params are scheduled in ln(iload) (>1 real distinct load); the header note
    # + the OP/scope line adapt so the .va is self-documenting about which path was emitted.
    sched_rails = [v_outs[i] for i, vb in enumerate(vblocks) if vb.get("scheduled")]
    if sched_rails:
        op_note = (f"// fitted admittance Y=g0+sCp + current-PSRR.\n"
                   f"// {len(sched_rails)}/{len(v_outs)} rail(s) load-SCHEDULED: each "
                   f"small-signal param is a clamped quadratic in ln(iload_<rail>)\n"
                   f"//   scheduled rails: {', '.join(sched_rails)} "
                   f"(set iload_<rail> to interpolate across VALID_LOAD; AT-corner = the "
                   f"fitted value)\n"
                   f"//   the remaining rails (single OP) bake fitted params as literals "
                   f"(NO laplace_nd). HB/PSS-robust.")
        scope_note = ("// SCOPE: LTI per-rail core spanning VALID_LOAD via ln(iload) "
                      "scheduling on the rails above\n"
                      "//        + large-signal current core -- NO tier>=T2 dropout/slew "
                      "term is emitted here. See VALID_LOAD above.")
    else:
        op_note = ("// fitted admittance Y=g0+sCp + current-PSRR. Single OP -> fitted "
                   "params baked as\n"
                   "// literals (NO laplace_nd, no ln(iload) interpolation). HB/PSS-robust.")
        scope_note = ("// SCOPE: pure LTI + large-signal current core -- NO tier>=T2 "
                      "dropout/slew term is emitted\n"
                      "//        here (the in-situ dropout/slew emission is stage 2b). "
                      "See VALID_LOAD above.")

    va = f"""// ============================================================
// Combined PMU behavioral model for Cadence Spectre (auto-gen: harness/emit_pmu_model.py)
// ONE module: input {supply} (LEFT) / {len(v_outs)} voltage rails + {len(i_outs)} current
// biases (RIGHT) / {' / '.join(grounds)} ground(s) (BOTTOM). Per-rail topology reuses the validated
// fit_model.emit_va transfer functions (Zout branches A/B/C + real PSRR bank + one
// complex 2nd-order section + decoupled Norton @vout noise); current biases use the
{op_note}
//   Voltage rails: {', '.join(v_outs)}
//   Current biases: {', '.join(i_outs) if i_outs else '(none)'}
{banner}
{scope_note}
// ============================================================
`include "constants.vams"
`include "disciplines.vams"

module {cell_name}({', '.join(all_ports)});
  input {supply};
  output {', '.join(v_outs + i_outs)};
  inout {', '.join(grounds)};
  {elec_decl}

  // INTERNAL model constants (localparam -> NOT instance/CDF parameters). vdc_{supply} is the
  // PSRR/compliance DC-operating-point REFERENCE the small-signal terms linearize around (= the
  // characterized supply DC; the live response still tracks the actual {supply} pin). NRk is the
  // fixed noise-section resistor (the fitted gn sets amplitude). Re-emit if the supply OP moves.
  localparam real vdc_{supply} = {supply_dc:g};   // {supply} DC operating-point reference [V]
  localparam real NRk = {NRk:.6e};   // fixed noise-section resistor (gm sets amplitude)
  {cn_params}

  {rvar_decl}

  analog begin
    @(initial_step) begin
      {asgs}
    end

{bodies}
  end
endmodule
"""
    ok, problems = va_sanity(va, supply, v_outs, i_outs, grounds)
    if not ok:
        raise ValueError(f"emit_pmu_va sanity check FAILED for {cell_name}: {problems}")
    va_path.parent.mkdir(parents=True, exist_ok=True)
    va_path.write_text(va)
    print(f"wrote {va_path}  (1 module, {len(v_outs)} V-rails + {len(i_outs)} I-biases)")
    print(va_format_report(va, supply, v_outs, i_outs, grounds))   # self-validate the target .va
    return va_path


if __name__ == "__main__":
    import argparse
    import sys
    HERE = pathlib.Path(__file__).resolve().parent
    ROOT = HERE.parent
    for _p in (str(HERE), str(ROOT / "cadence")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    ap = argparse.ArgumentParser(description="Emit the combined PMU Verilog-A model cell")
    ap.add_argument("--variant", required=True, help="results/ref/<variant>.npz stem")
    ap.add_argument("--manifest", required=True, help="pin-role manifest name/path")
    ap.add_argument("--cell", default="PMU_model", help="emitted module/cell name")
    ap.add_argument("--out", default=None, help="output .va path")
    ap.add_argument("--supply", default="AVDD1P0")
    ap.add_argument("--ground", default="VSS")
    a = ap.parse_args()
    import fit_multiport as FMP
    from insitu import manifest as _M
    m = _M.load(a.manifest)
    npz = ROOT / "results" / "ref" / f"{a.variant}.npz"
    res = FMP.fit_multiport(npz, m)
    out = a.out or (ROOT / "model" / f"{a.cell}.va")
    emit_pmu_va(res, a.cell, out, supply=a.supply, ground=a.ground)
