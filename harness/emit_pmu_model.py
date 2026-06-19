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


def _voltage_block(o, vfit, supply, ground):
    """Render the Verilog-A statements + node/var declarations for ONE voltage rail.
    Reuses the EXACT contribution structure of fit_model.emit_va, namespaced by `<o>_`
    and with the fitted params baked in as literals (single OP -> no interpolation)."""
    P = vfit["P"]
    il = _nom_corner(P)
    p = P[il]
    nfk = list(vfit["nfk"])
    Cout = float(vfit["cout"])
    ESR = float(vfit["esr"])
    vreg = float(p["vreg"])
    G, Q = _primary_supply_psrr(vfit, p, supply)
    pcb0, pcb1, pcw0, pcq = (float(Q[0]), float(Q[1]), float(Q[2]), float(Q[3]))

    pre = o  # node/var namespace prefix
    # internal nodes for this rail (same roles as emit_va: vrg, nA, nC, nbb, np1-3,
    # vrf, ncs1/2, nw + Lorentzian noise nodes nk1..)
    nks = [f"{pre}_nk{k+1}" for k in range(len(nfk))]
    nodes = [f"{pre}_vrg", f"{pre}_nA", f"{pre}_nC", f"{pre}_nbb",
             f"{pre}_np1", f"{pre}_np2", f"{pre}_np3", f"{pre}_vrf",
             f"{pre}_ncs1", f"{pre}_ncs2", f"{pre}_nw"] + nks

    # per-rail real vars (all baked literals assigned in initial_step) + derived RLC
    rvars = [f"{pre}_Ra", f"{pre}_La", f"{pre}_Rpl", f"{pre}_Rb", f"{pre}_Lb",
             f"{pre}_G0", f"{pre}_G1", f"{pre}_w1", f"{pre}_G2", f"{pre}_w2",
             f"{pre}_G3", f"{pre}_w3", f"{pre}_pcb0", f"{pre}_pcb1", f"{pre}_pcw0",
             f"{pre}_pcq", f"{pre}_gnw", f"{pre}_vreg", f"{pre}_Cps",
             f"{pre}_pca1", f"{pre}_pca2", f"{pre}_Rpc", f"{pre}_Lpc", f"{pre}_Cpc",
             f"{pre}_gqb1"] + [f"{pre}_gn{k+1}" for k in range(len(nfk))]

    Cn_par = "\n  ".join(
        f"parameter real {pre}_Cn{k+1} = {1.0/(TWO_PI*nfk[k]*NRk):.6e};"
        f"   // {o} noise corner {nfk[k]:.4g} Hz"
        for k in range(len(nfk)))

    # --- initial_step assignments (baked fitted params) ---
    asg = "\n      ".join([
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
        f"{pre}_vreg = {vreg:.6e};",
    ] + [f"{pre}_gn{k+1} = {float(p[f'gn{k+1}']):.6e};" for k in range(len(nfk))]
      + [
        f"{pre}_Cps = 1e-12;",
        f"{pre}_Cpc = 1e-12;",
        f"{pre}_pca1 = 1.0/({pre}_pcq*{pre}_pcw0);  {pre}_pca2 = 1.0/({pre}_pcw0*{pre}_pcw0);",
        f"{pre}_Rpc = {pre}_pca1/{pre}_Cpc;  {pre}_Lpc = {pre}_pca2/{pre}_Cpc;"
        f"  {pre}_gqb1 = {pre}_pcb1/{pre}_pca1;",
    ])

    # --- noise sections (Norton @vout) ---
    nsec = "\n    ".join(
        f"I({pre}_nk{k+1}, {ground}) <+ V({pre}_nk{k+1}, {ground})/NRk"
        f" + {pre}_Cn{k+1}*ddt(V({pre}_nk{k+1}, {ground}));\n"
        f"    I({pre}_nk{k+1}, {ground}) <+ white_noise(4*`P_K*$temperature/NRk, \"{pre}_nk{k+1}\");\n"
        f"    I({o}, {ground})    <+ {pre}_gn{k+1}*V({pre}_nk{k+1}, {ground});"
        for k in range(len(nfk)))

    body = f"""    // ============ voltage rail {o} (Cout={Cout:.3e}F ESR={ESR:.3e}ohm) ============
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
    V({pre}_nA, {pre}_vrg) <+ {pre}_Ra*I({pre}_nA, {pre}_vrg);

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
    I({o}, {ground}) <+ -({pre}_pcb0*V({pre}_ncs2, {pre}_vrf) + {pre}_gqb1*V({supply}, {pre}_ncs1));
"""
    return dict(nodes=nodes, rvars=rvars, params=Cn_par, asg=asg, body=body)


def current_crow_from_isrc_fit(p, pin=None, tnom_c=55.0):
    """harness/fit_isrc.fit_isrc(...) param dict -> a `crow` that _current_block emits
    as the LARGE-SIGNAL VA form. Connects the offline-validated behavioral current model
    (8/8 vs the MOS-GT, harness/crossval_isrc.py) to this Cadence VA emit; the in-situ
    fit_multiport will produce the same fields on the box. `tnom_c` = the nominal temp the
    Idc/didt fit is referenced to (the manifest's tnom_c / middle of its temps)."""
    return dict(sink=p["name"], pin=pin or p["name"], pol=p["pol"],
                idc55=p["idc55"], didt=p["didt"], g0=p["g0"], vc=p["vc"],
                gdd=p["gdd"], vknee=p["vknee"], knee_p=p["knee_p"], Cp=p["cp"],
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
    g0 = float(crow.get("g0", 0.0)); vc = float(crow.get("vc", 0.0))
    vk = float(crow.get("vknee", 0.1)); kp = float(crow.get("knee_p", 1.0))
    cp = float(crow.get("Cp", 0.0)); gdd = float(crow.get("gdd", 0.0))
    gdd_eff = -gdd if pol == "sink" else gdd
    inw2 = float(crow.get("in_white", 0.0)) ** 2
    kf = float(crow.get("in_kf", 0.0))
    tref_k = float(crow.get("tnom_c", 55.0)) + 273.15    # $temperature is KELVIN; user-set nominal
    rvars = [f"{pre}_idc55", f"{pre}_didt", f"{pre}_g0", f"{pre}_vc", f"{pre}_gdd",
             f"{pre}_vk", f"{pre}_kp", f"{pre}_Cp", f"{pre}_inw2", f"{pre}_kf"]
    asg = (f"{pre}_idc55 = {idc55:.6e};  {pre}_didt = {didt:.6e};  {pre}_g0 = {g0:.6e};  "
           f"{pre}_vc = {vc:.6g};  {pre}_gdd = {gdd_eff:.6e};  {pre}_vk = {vk:.6g};  "
           f"{pre}_kp = {kp:.6g};  {pre}_Cp = {cp:.6e};  {pre}_inw2 = {inw2:.6e};  "
           f"{pre}_kf = {kf:.6e};")
    if pol == "sink":
        karg = f"sqrt(V({o},{ground})*V({o},{ground}) + 1e-12)"
        drive = f"I({o}, {ground})"
    else:
        karg = f"sqrt((vdc_{supply}-V({o},{ground}))*(vdc_{supply}-V({o},{ground})) + 1e-12)"
        drive = f"I({supply}, {o})"
    body = f"""    // ====== current bias {o} ({pol}: Idc(T)+I-V knee+g0+Cp+signed PSRR+noise) ======
    {drive} <+ ({pre}_idc55 + {pre}_didt*($temperature - {tref_k:g})
                  + {pre}_g0*(V({o},{ground}) - {pre}_vc)
                  + {pre}_gdd*(V({supply},{ground}) - vdc_{supply}))
                 * tanh(pow({karg}/{pre}_vk, {pre}_kp));
    I({o}, {ground}) <+ {pre}_Cp*ddt(V({o}, {ground}));   // output cap (Y imag part)
    I({o}, {ground}) <+ white_noise({pre}_inw2, "{pre}_wht");
    I({o}, {ground}) <+ flicker_noise({pre}_kf, 1.0, "{pre}_flk");
"""
    return dict(rvars=rvars, asg=asg, body=body)


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
        (the supply, every voltage + current out, and the ground appear in the
        module header port list and in the input/output/inout direction decls)."""
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
    expected = [supply] + list(v_outs) + list(i_outs) + [ground]
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
    if ground not in inouts:
        problems.append(f"ground {ground} not declared 'inout'")
    return (not problems), problems


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


def emit_pmu_va(fit_result, cell_name, va_path, supply="AVDD1P0", ground="VSS",
                supply_dc=None, tnom_c=None, provenance=None):
    """Emit ONE combined Verilog-A module `cell_name` for the whole PMU: input `supply`
    (LEFT), every voltage rail + current bias from fit_result (RIGHT), `ground` (BOTTOM).

    fit_result := harness.fit_multiport.fit_multiport(npz, manifest) output.
    `provenance` (optional) := {tier, op_iload, op_temp, valid_load:(lo,hi)} -> stamps the
    header COVERAGE/OP/VALID_LOAD banner (else sourced from fit_result['meta'], else a clear
    default). Returns the written va_path (pathlib.Path). Raises ValueError if the emitted
    text fails the static VA sanity check (so a malformed interface never reaches the box)."""
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

    # pass the PIN name (port) as the block's `o` -- it is used as BOTH the port reference
    # and the internal node namespace prefix; pin names are unique + valid VA identifiers.
    vblocks = [_voltage_block(port, voltage[rk], supply, ground)
               for rk, port in zip(v_keys, v_outs)]
    cblocks = [_current_block(port, r, supply, ground) for port, r in zip(i_outs, crows)]

    # assemble the single module ----------------------------------------------------
    all_ports = [supply] + v_outs + i_outs + [ground]
    internal_nodes = []
    for vb in vblocks:
        internal_nodes += vb["nodes"]
    all_elec = all_ports + internal_nodes

    rvars = []
    for vb in vblocks:
        rvars += vb["rvars"]
    for cb in cblocks:
        rvars += cb["rvars"]

    cn_params = "\n  ".join(vb["params"] for vb in vblocks if vb["params"])
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

    va = f"""// ============================================================
// Combined PMU behavioral model for Cadence Spectre (auto-gen: harness/emit_pmu_model.py)
// ONE module: input {supply} (LEFT) / {len(v_outs)} voltage rails + {len(i_outs)} current
// biases (RIGHT) / {ground} ground (BOTTOM). Per-rail topology reuses the validated
// fit_model.emit_va transfer functions (Zout branches A/B/C + real PSRR bank + one
// complex 2nd-order section + decoupled Norton @vout noise); current biases use the
// fitted admittance Y=g0+sCp + current-PSRR. Single OP -> fitted params baked as
// literals (NO laplace_nd, no ln(iload) interpolation). HB/PSS-robust.
//   Voltage rails: {', '.join(v_outs)}
//   Current biases: {', '.join(i_outs) if i_outs else '(none)'}
{banner}
// SCOPE: pure LTI + large-signal current core -- NO tier>=T2 dropout/slew term is emitted
//        here (the in-situ dropout/slew emission is stage 2b). See VALID_LOAD above.
// ============================================================
`include "constants.vams"
`include "disciplines.vams"

module {cell_name}({', '.join(all_ports)});
  input {supply};
  output {', '.join(v_outs + i_outs)};
  inout {ground};
  {elec_decl}

  parameter real vdc_{supply} = {supply_dc:g};   // {supply} DC operating point [V]
                                    // (PSRR / current-PSRR DC reference)
  parameter real NRk = {NRk:.6e};   // fixed noise-section resistor (gm sets amplitude)
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
    ok, problems = va_sanity(va, supply, v_outs, i_outs, ground)
    if not ok:
        raise ValueError(f"emit_pmu_va sanity check FAILED for {cell_name}: {problems}")
    va_path.parent.mkdir(parents=True, exist_ok=True)
    va_path.write_text(va)
    print(f"wrote {va_path}  (1 module, {len(v_outs)} V-rails + {len(i_outs)} I-biases)")
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
