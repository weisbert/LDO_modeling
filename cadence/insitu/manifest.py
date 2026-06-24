"""P1 -- the pin-role manifest: the designer-supplied contract that drives the whole
Mechanism-A flow. "Capture-and-augment, not reconstruct": the designer TAGS each
DUT-boundary pin with a role + how to stimulate it; we never infer roles, hunt the
designer's sources, or hardcode net/corner/section names.

A manifest is plain JSON (so a designer can hand-edit it, and the GUI Extract tab can
load/save it). Schema:

    {
      "name": "pmu_top",
      "dut": {"lib","cell","tb_lib","tb_cell","tb_inst","extract_cell"},
      "ground": "gnd!",
      "supplies": { "<s>": {"net","dc","tb_src"?,"analysis"?} },        # role: supply
      "v_out":    { "<o>": {"net","iload"?,"src"?,"analysis"?} },        # role: voltage output
      "i_out":    { "<c>": {"net","dc","probe_src"?,"analysis"?} },      # role: current output (sink)
      "bias":     { "<b>": {"net","dc"} },                   # role: bias port (held, optional)
      "leave_alone": ["BIAS_EN", "PLL_CTRL<3:0>", ...],      # role: leave_alone
      "corners":  {"pull_from_session": true, "fallback": ["nom"]},
      "current_psrr_supplies": ["1p0"],          # which supplies to current-PSRR (subset)
      "analysis": {"ac": "...", "noise": "..."}  # ALPS/Spectre-shared analysis lines
    }

Source-reuse model (the UNIFIED contract): EVERY role REUSES the existing TB source on its
net rather than appending a fresh one -- the designer's TB already places an idc/vdc on
every v_out / i_out pin, so appending a 2nd driver would double-drive the node. Per role:
  supply s : reuse supplies.<s>.tb_src (else auto-detect the VSOURCE on net), set mag=acm.
  v_out  o : reuse v_out.<o>.src      (else auto-detect the ISOURCE  on net), set mag=acm.
  i_out  c : reuse i_out.<c>.probe_src (else auto-detect the VSOURCE on net), set mag=acm.
An OPEN pin with no source falls back to the old insert (append Iext_<o>/Vprobe_<c>).

Per-object analysis override (OPTIONAL): supplies.<s>.analysis, v_out.<o>.analysis,
i_out.<c>.analysis are dicts {ac?, noise?} (v_out may carry both; supply/i_out only ac).
Absent -> the global m["analysis"] is used. The netlister keys the override by the group
OWNER (the one-hot stimulus, or the v_out owning a noise group).

The two consumers:
  * augment (P2) reads supplies/v_out/i_out to know WHICH existing source to set acm on
    (or, for an open pin, where to append) and WHAT to save (targeted, never allpub).
  * importmp (P4) reads the SAME roles to know HOW to derive each contract array from the
    raw PSF (Zout=V@1A, PSRR=Vout/Vsup, Y=-I, etc.) -- the read-side is the dual of the
    stimulus-side, both pinned by this one manifest.

`measurements()` turns the roles into the explicit 8-point matrix (the single source of
truth shared by augment + import), so the two halves can never drift.
"""
import json
import math
import pathlib

# canonical roles (the designer's tagging vocabulary)
ROLES = ("supply", "v_out", "i_out", "bias", "leave_alone")

# ---------------------------------------------------------------- coverage tiers
# The coverage tier ladder (§2 of HANDOFF_MODELING_COVERAGE): nested additive presets.
# A tier turns ITS items on plus every lower tier's; T0 (ac/noise) is always on.
TIERS = ("T0", "T1", "T2", "T3", "T4")
COVERAGE_ITEMS = ("ac", "noise", "slew", "iv", "dropout", "load_schedule", "temp", "inoise")
_TIER_ITEMS = {"T0": ("ac", "noise"), "T1": ("slew",), "T2": ("iv", "dropout"),
               "T3": ("load_schedule",), "T4": ("temp",)}

# AC magnitude "hot" design-variable per stimulus, default 0 (AC superposition: a mag=1
# AC source has dc=0 so it never moves the shared OP; setting exactly ONE acm_*=1 per
# point keeps each transfer function identifiable).
def acm_var(kind, key):
    """The acm_* design-variable name for a given stimulus (stable, collision-free)."""
    return f"acm_{kind}_{key}"


class ManifestError(ValueError):
    pass


def load(path):
    """Load + validate a manifest JSON. Returns the dict (with defaults filled). Raises
    ManifestError with an actionable message on any contract violation."""
    p = pathlib.Path(path)
    if not p.exists():
        # allow bare names resolved against the package manifests/ dir
        from . import MANIFEST_DIR
        cand = MANIFEST_DIR / (p.name if p.suffix == ".json" else f"{p.name}.json")
        if cand.exists():
            p = cand
        else:
            raise ManifestError(f"manifest not found: {path} (also tried {cand})")
    try:
        m = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ManifestError(f"{p}: invalid JSON -- {e}")
    m = _fill_defaults(m)
    validate(m)
    m["_path"] = str(p)
    return m


def _fill_defaults(m):
    m.setdefault("ground", "gnd!")
    m.setdefault("supplies", {})
    m.setdefault("v_out", {})
    m.setdefault("i_out", {})
    m.setdefault("bias", {})
    m.setdefault("leave_alone", [])
    m.setdefault("corners", {"pull_from_session": True, "fallback": ["nom"]})
    m["corners"].setdefault("pull_from_session", True)
    m["corners"].setdefault("fallback", ["nom"])
    # default: current-PSRR against every declared supply (override to a subset to match
    # a trusted reference, e.g. the CLI gold measured pi only vs 1p0)
    m.setdefault("current_psrr_supplies", list(m.get("supplies", {}).keys()))
    m.setdefault("analysis", {})
    m["analysis"].setdefault("ac", "ac start=10 stop=500M dec=20")
    m["analysis"].setdefault("noise", "noise start=10 stop=100M dec=20")
    d = m.setdefault("dut", {})
    d.setdefault("extract_cell", (d.get("tb_cell", "TB") + "_extract"))
    # coverage section (§2 tiers + per-rail sweep params). DEFAULT = FULL tier ladder, but
    # with NO declared loads/transient/iv/dropout -> the tier alone adds ZERO measurement
    # points, so a shipped (coverage-free) manifest yields the identical T0 matrix.
    cov = m.setdefault("coverage", {})
    cov.setdefault("tier", "T4")            # default = FULL tier ladder
    cov.setdefault("enable", {})            # per-item bool overrides on top of the tier
    cov.setdefault("loads", {})             # {v_out_key: {"sweep":{...}|None, "points":[...],
                                            #              "nominal":float?, "holdout":float?}}
    cov.setdefault("transient", {})         # {v_out_key: {"steps":[{"from","to","label"?}],
                                            #              "edge":float?, "tstop":float?, "tstep":float?}}
    cov.setdefault("iv", {})                # {i_out_key: {"sweep":{"type","start","stop","n"}}}
    cov.setdefault("dropout", {})           # {v_out_key: {"sweep":{"type","start","stop","n"}}}
    cov.setdefault("temps", [])             # [] => single (session) temp; else e.g. [-40,55,125]
    cov.setdefault("lin_gate", False)       # guardrail-4 2x-amplitude AC self-check (off by default)
    cov.setdefault("slew_en", 0)            # the emitted VA param's default (0 = run LTI)
    return m


def coverage_enabled(m, item):
    """Is a coverage ITEM active? An explicit coverage.enable[item] override wins; else the
    item is on when the manifest's tier is >= the tier that introduces it. ac/noise are on at
    every tier. NB: a LOADED manifest can never carry an unknown tier -- validate()/_validate_coverage
    reject it loudly (fail-fast on a typo). The `else len(TIERS)-1` below is purely DEFENSIVE for an
    in-memory dict that bypassed validate() (e.g. a hand-built fixture): a junk tier degrades to full
    rather than IndexError, it is NOT a supported manifest feature."""
    cov = m.get("coverage") or {}
    en = cov.get("enable") or {}
    if item in en:
        return bool(en[item])
    tier = cov.get("tier", "T4")
    idx = TIERS.index(tier) if tier in TIERS else len(TIERS) - 1   # unknown tier: defensive only
    for t in TIERS[: idx + 1]:
        if item in _TIER_ITEMS[t]:
            return True
    return False


def _expand_sweep(sweep):
    """A {'type':'lin'|'log','start','stop','n'} sweep -> an ascending python list of floats.
    n<=1 -> [start]. log requires start>0,stop>0. Pure python (no numpy)."""
    if not sweep:
        return []
    typ = sweep.get("type", "lin"); n = int(sweep.get("n", 0) or 0)
    a = float(sweep["start"]); b = float(sweep["stop"])
    if n <= 1:
        return [a]
    if typ == "log":
        la, lb = math.log(a), math.log(b)
        return [math.exp(la + (lb - la) * i / (n - 1)) for i in range(n)]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def load_points(m, o):
    """Merged ascending, deduped iload values for v_out o: _expand_sweep(loads[o].sweep)
    UNION loads[o].points UNION nominal/holdout if given. [] when nothing declared (=> the
    single-OP behavior of today)."""
    spec = ((m.get("coverage") or {}).get("loads") or {}).get(o) or {}
    vals = list(_expand_sweep(spec.get("sweep")))
    vals += [float(x) for x in (spec.get("points") or [])]
    for k in ("nominal", "holdout"):
        if spec.get(k) is not None:
            vals.append(float(spec[k]))
    out = sorted({round(v, 18) for v in vals})
    return out


def temps(m):
    """The temperature corner list (coverage.temps), or [] for single (session) temp."""
    return list((m.get("coverage") or {}).get("temps") or [])


def slew_en_default(m):
    return int((m.get("coverage") or {}).get("slew_en", 0) or 0)


def _validate_analysis_override(v, where, allowed):
    """A per-object 'analysis' override is OPTIONAL. When present it must be a dict carrying
    only string-valued keys drawn from `allowed` ({'ac'} for supply/i_out, {'ac','noise'} for
    v_out). Raises ManifestError on a non-dict, an unknown key, or a non-string value."""
    a = v.get("analysis")
    if a is None:
        return
    if not isinstance(a, dict):
        raise ManifestError(f"{where}.analysis must be a dict {{ac?, noise?}}, got {type(a).__name__}")
    for k, val in a.items():
        if k not in allowed:
            raise ManifestError(
                f"{where}.analysis has unsupported key '{k}' (allowed: {sorted(allowed)})")
        if not isinstance(val, str):
            raise ManifestError(f"{where}.analysis.{k} must be a string analysis line")


def validate(m):
    """Pure structural checks (no simulator/session). Raises ManifestError on violation."""
    if not m.get("name"):
        raise ManifestError("manifest needs a 'name'")
    d = m.get("dut") or {}
    for k in ("lib", "cell", "tb_lib", "tb_cell"):
        if not d.get(k):
            raise ManifestError(f"dut.{k} is required (lib/cell of the DUT + its testbench)")
    if not (m["v_out"] or m["i_out"]):
        raise ManifestError("manifest must declare at least one v_out or i_out to model")
    seen = {}
    def claim(net, where):
        if not net:
            raise ManifestError(f"{where}: missing 'net'")
        if net in seen:
            raise ManifestError(f"net '{net}' tagged twice: {seen[net]} and {where}")
        seen[net] = where
    for s, v in m["supplies"].items():
        claim(v.get("net"), f"supplies.{s}")
        if "dc" not in v:
            raise ManifestError(f"supplies.{s}.dc is required (the rail's DC value / OP)")
        _validate_analysis_override(v, f"supplies.{s}", {"ac"})
    for o, v in m["v_out"].items():
        claim(v.get("net"), f"v_out.{o}")
        _validate_analysis_override(v, f"v_out.{o}", {"ac", "noise"})
    for c, v in m["i_out"].items():
        claim(v.get("net"), f"i_out.{c}")
        v.setdefault("dc", 0.0)
        _validate_analysis_override(v, f"i_out.{c}", {"ac", "noise"})   # noise: opt-in inoise
    for b, v in m["bias"].items():
        claim(v.get("net"), f"bias.{b}")
    for s in m["current_psrr_supplies"]:
        if s not in m["supplies"]:
            raise ManifestError(f"current_psrr_supplies references unknown supply '{s}'")
    _validate_coverage(m)
    return True


def _validate_sweep(sweep, where):
    """A sweep must be a {type in {lin,log}, start, stop, n} dict. Lenient on extras."""
    if not isinstance(sweep, dict):
        raise ManifestError(f"{where} must be a dict {{type,start,stop,n}}, got {type(sweep).__name__}")
    for k in ("start", "stop", "n"):
        if k not in sweep:
            raise ManifestError(f"{where} is missing '{k}' (needs type,start,stop,n)")
    typ = sweep.get("type", "lin")
    if typ not in ("lin", "log"):
        raise ManifestError(f"{where}.type must be 'lin' or 'log', got {typ!r}")


def _validate_coverage(m):
    """LENIENT coverage checks -- every coverage entry is optional (defaults fill it). Catches
    a bad tier, an unknown/non-bool enable item, a loads/transient/iv/dropout key that does NOT
    name a declared v_out/i_out, a malformed sweep, and a non-numeric temps list."""
    cov = m.get("coverage") or {}
    tier = cov.get("tier", "T4")
    if tier not in TIERS:
        raise ManifestError(f"coverage.tier '{tier}' not in {list(TIERS)}")
    en = cov.get("enable", {})
    if not isinstance(en, dict):
        raise ManifestError(f"coverage.enable must be a dict, got {type(en).__name__}")
    for k, v in en.items():
        if k not in COVERAGE_ITEMS:
            raise ManifestError(
                f"coverage.enable has unknown item '{k}' (allowed: {list(COVERAGE_ITEMS)})")
        if not isinstance(v, bool):
            raise ManifestError(f"coverage.enable.{k} must be a bool, got {type(v).__name__}")
    # loads/transient/dropout key a v_out; iv keys an i_out
    for sect, owner in (("loads", "v_out"), ("transient", "v_out"),
                        ("dropout", "v_out"), ("iv", "i_out")):
        spec = cov.get(sect) or {}
        if not isinstance(spec, dict):
            raise ManifestError(f"coverage.{sect} must be a dict keyed by {owner}, "
                                f"got {type(spec).__name__}")
        for key, entry in spec.items():
            if key not in m[owner]:
                raise ManifestError(
                    f"coverage.{sect}['{key}'] names an undeclared {owner} "
                    f"(declared: {list(m[owner])})")
            if isinstance(entry, dict) and entry.get("sweep") is not None:
                _validate_sweep(entry["sweep"], f"coverage.{sect}['{key}'].sweep")
    tps = cov.get("temps", [])
    if not isinstance(tps, list):
        raise ManifestError(f"coverage.temps must be a list of numbers, got {type(tps).__name__}")
    for t in tps:
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise ManifestError(f"coverage.temps must be numbers, got {t!r}")


def analysis_override(m, role, key, kind):
    """The per-object analysis line for (role, key, kind in {'ac','noise'}), or None to fall
    back to the global m['analysis'][kind]. role in {'supplies','v_out','i_out'}. This is the
    single lookup the netlister uses so the offline + live paths read the override identically."""
    v = (m.get(role) or {}).get(key) or {}
    a = v.get("analysis") or {}
    return a.get(kind)


def analysis_line_for(m, role, key, kind):
    """The resolved analysis line for (role, key, kind): the per-object override if present,
    else the global m['analysis'][kind]."""
    return analysis_override(m, role, key, kind) or m["analysis"][kind]


def measurements(m):
    """Derive the explicit measurement matrix from the roles -- the SINGLE source of
    truth shared by augment (stimulus side) and import (read/derive side). Each entry:

        {tag, analysis, hot: [(kind,key), ...], reads: [...], derive, key, save: [...]}

      tag      = run/point label (also the CLI-PSF subdir name, e.g. 'z_pll')
      analysis = 'ac' | 'noise'
      hot      = list of (kind,key) whose acm_* var is set to 1 for this point (exactly
                 one for identifiability; [] for noise which needs no AC stimulus)
      reads    = the raw PSF signals to pull: ('v', net) node voltage | ('i', probe) current
      derive   = how Python turns reads -> the contract array (firewall: ratios in Python)
      key      = the npz array key (без the _<load> suffix; load appended at import)
      save     = the targeted save set for this point (nets + probe:p), never allpub
    """
    outs = list(m["v_out"])
    sups = list(m["supplies"])
    sinks = list(m["i_out"])
    M = []

    # voltage outputs: Zout (1A AC in -> V), output-to-output coupling, noise
    for o in outs:
        onet = m["v_out"][o]["net"]
        M.append(dict(tag=f"z_{o}", analysis="ac", hot=[("v_out", o)],
                      reads=[("v", onet)], derive="z", key=f"z_{o}",
                      save=[("v", onet)]))
        for o2 in outs:
            if o2 == o:
                continue
            o2net = m["v_out"][o2]["net"]
            M.append(dict(tag=f"c_{o}_{o2}", analysis="ac", hot=[("v_out", o)],
                          reads=[("v", o2net)], derive="z", key=f"couple_{o}_{o2}",
                          save=[("v", onet), ("v", o2net)]))
        M.append(dict(tag=f"n_{o}", analysis="noise", hot=[], reads=[("noise", onet)],
                      derive="noise", key=f"noise_{o}", save=[("v", onet)]))

    # voltage output x supply: PSRR = Vout/Vsup
    for o in outs:
        onet = m["v_out"][o]["net"]
        for s in sups:
            snet = m["supplies"][s]["net"]
            M.append(dict(tag=f"p_{o}_{s}", analysis="ac", hot=[("supply", s)],
                          reads=[("v", onet), ("v", snet)], derive="psrr",
                          key=f"p_{o}_{s}", save=[("v", onet), ("v", snet)]))

    # current sinks: admittance (1V AC at pin -> -I) and current-PSRR (supply AC -> -I/Vsup)
    for c in sinks:
        cnet = m["i_out"][c]["net"]
        probe = _probe_name(m, c)
        M.append(dict(tag=f"y_{c}", analysis="ac", hot=[("i_out", c)],
                      reads=[("i", probe)], derive="y", key=f"y_{c}",
                      save=[("i", probe)]))
        for s in m["current_psrr_supplies"]:
            snet = m["supplies"][s]["net"]
            M.append(dict(tag=f"pi_{c}_{s}", analysis="ac", hot=[("supply", s)],
                          reads=[("i", probe), ("v", snet)], derive="pi",
                          key=f"pi_{c}_{s}", save=[("i", probe), ("v", snet)]))

    # ---------------------------------------------------------------- coverage-gated kinds
    # APPENDED after the T0 core (above) so the existing 6 derives stay first + byte-identical.
    # Each kind is gated on its tier item AND on declared params: a manifest that declares no
    # loads/transient/iv/dropout adds ZERO points here (the backward-compat lever). The new
    # points carry EXTRA keys (sweep/step/edge/tstop/tstep/amp) that the T0 consumers ignore;
    # only run.groups branches on analysis/amp.

    # NB: the gated blocks read coverage defensively ((m.get("coverage") or {})) because
    # measurements() is also called on manifests that bypass _fill_defaults (build_manifest's
    # in-memory dict) -- a missing coverage section just means zero coverage points.
    cov = m.get("coverage") or {}

    # T2: i_out vdc I-V sweep (analysis 'dc') -- sweep the reused vdc, read probe current vs Vsink
    if coverage_enabled(m, "iv"):
        for c in sinks:
            spec = (cov.get("iv") or {}).get(c)
            if not spec or not (spec.get("sweep") or spec.get("points")):
                continue
            probe = _probe_name(m, c)
            # iv carries an optional 'points' (specific compliance/I-V voltages added to the
            # swept grid); the netlister folds sweep+points into one dc value list.
            M.append(dict(tag=f"iv_{c}", analysis="dc", hot=[("i_out", c)],
                          reads=[("i", probe)], derive="iv", key=f"iv_{c}",
                          save=[("i", probe)], sweep=spec.get("sweep"),
                          points=spec.get("points")))

    # current-bias OUTPUT-CURRENT noise (analysis 'noise', probe-form oprobe) -- one per sink.
    # OPT-IN via coverage.enable.inoise (no tier auto-enables it) because the `oprobe=<probe>`
    # current-noise netlist is box-validate-pending; the model FORM/fit/emit are validated. Reads
    # the probe vsource's current-noise PSD (A/rtHz, the sink's output-current noise) -> feeds
    # fit_isrc._fit_noise (in_white/in_kf). 'oprobe_src' marks the group as a current-noise group.
    if coverage_enabled(m, "inoise"):
        for c in sinks:
            probe = _probe_name(m, c)
            M.append(dict(tag=f"ni_{c}", analysis="noise", hot=[], reads=[("inoise", probe)],
                          derive="noise_i", key=f"noise_i_{c}", save=[("i", probe)],
                          oprobe_src=probe))

    # T2: v_out DC iload sweep -> dropout / load-reg (analysis 'dc') -- sweep the reused load
    #     isource, read Vout. Uses coverage.dropout[o].sweep, else coverage.loads[o].sweep.
    if coverage_enabled(m, "dropout"):
        for o in outs:
            dspec = (cov.get("dropout") or {}).get(o)
            sweep = (dspec or {}).get("sweep") or \
                ((cov.get("loads") or {}).get(o) or {}).get("sweep")
            if not sweep:
                continue
            onet = m["v_out"][o]["net"]
            M.append(dict(tag=f"dc_{o}", analysis="dc", hot=[("v_out", o)],
                          reads=[("v", onet)], derive="dropout", key=f"dc_{o}",
                          save=[("v", onet)], sweep=sweep))

    # T1: transient load steps -> slew/recovery (analysis 'tran') -- one point per declared step
    if coverage_enabled(m, "slew"):
        for o in outs:
            tspec = (cov.get("transient") or {}).get(o)
            if not tspec:
                continue
            onet = m["v_out"][o]["net"]
            for st in (tspec.get("steps") or []):
                lbl = st.get("label") or f"{st['from']:g}_{st['to']:g}"
                M.append(dict(tag=f"tr_{o}_{lbl}", analysis="tran", hot=[("v_out", o)],
                              reads=[("v", onet)], derive="trans", key=f"tr_{o}_{lbl}",
                              save=[("v", onet)], step=st, edge=tspec.get("edge"),
                              tstop=tspec.get("tstop"), tstep=tspec.get("tstep")))

    # guardrail-4: 2x-amplitude linearity self-check on each Zout point (analysis 'ac', amp=2)
    if (m.get("coverage") or {}).get("lin_gate"):
        for o in outs:
            onet = m["v_out"][o]["net"]
            M.append(dict(tag=f"z2_{o}", analysis="ac", hot=[("v_out", o)],
                          reads=[("v", onet)], derive="z", key=f"z2_{o}",
                          save=[("v", onet)], amp=2.0))
    return M


def _probe_name(m, c):
    """The named probe source we own at current-sink <c> (we read its :p). Stable name so
    augment writes it and import reads the identical key -- never reverse-engineer the
    DUT's internal terminal numbering."""
    return m["i_out"][c].get("probe_src") or f"Vprobe_{c}"


def summary(m):
    """One-line-per-role human summary for the doctor/GUI."""
    L = [f"manifest '{m['name']}'  DUT {m['dut']['lib']}/{m['dut']['cell']}  "
         f"TB {m['dut']['tb_lib']}/{m['dut']['tb_cell']} -> {m['dut']['extract_cell']}"]
    L.append(f"  supplies : " + ", ".join(f"{s}={v['net']}@{v['dc']}V"
                                           for s, v in m["supplies"].items()))
    L.append(f"  v_out    : " + ", ".join(f"{o}={v['net']}" for o, v in m["v_out"].items()))
    L.append(f"  i_out    : " + ", ".join(f"{c}={v['net']}" for c, v in m["i_out"].items()))
    if m["leave_alone"]:
        L.append(f"  leave_alone: " + ", ".join(m["leave_alone"]))
    # temperature guardrail: a current reference's Idc(T)/PTAT slope + noise(T) are characterized
    # ONLY across coverage.temps. With current outputs present but no temps declared, every ref is
    # measured at the single session temp -- a PTAT/CTAT ref then has NO temperature coefficient
    # fitted. The tier alone never adds temps (the points must be declared) -> make the gap LOUD.
    if m["i_out"] and not temps(m):
        ptat = [c for c, v in m["i_out"].items()
                if any(k in c.lower() or k in str(v.get("net", "")).lower()
                       or k in str(v.get("pin", "")).lower()
                       for k in ("ptat", "ctat"))]
        nmnote = (" incl. temperature-defined ref(s): " + ", ".join(ptat)) if ptat else ""
        L.append("  ! NO temperature corners (coverage.temps empty): current refs" + nmnote
                 + " run at the single session temp only -> Idc(T)/PTAT slope + current-noise(T) "
                 "NOT characterized. Declare coverage.temps (e.g. -40,25,125) to sweep temperature.")
    meas = measurements(m)
    L.append(f"  -> {len(meas)} measurement points: "
             + ", ".join(x["tag"] for x in meas))
    return "\n".join(L)


def propose(session=None, dut=None):  # noqa: ARG001  (P2 nice-to-have; stub now)
    """STUB (Phase-2 nice-to-have): probe a DUT instance's pins via DC/AC and draft a
    role guess for the designer to CONFIRM. Roles are the designer's judgement, so this
    only ever proposes -- it never finalizes. Returns a skeleton manifest dict."""
    raise NotImplementedError(
        "propose() is a documented Phase-2 stub: auto-role-proposal from a live probe. "
        "Today the designer supplies the manifest (e.g. insitu/manifests/pmu_top.json).")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Load + validate a pin-role manifest")
    ap.add_argument("manifest", help="path or bare name (resolved in insitu/manifests/)")
    a = ap.parse_args()
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from insitu import manifest as _m
    mm = _m.load(a.manifest)
    print(_m.summary(mm))
