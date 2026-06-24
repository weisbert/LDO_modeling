"""C -- build a valid pin-role manifest from the GUI inputs + a resolved net map.

The GUI (Extract tab) hands us symbol PIN NAMES + a few scalars; the B resolver
(insitu/resolve.py) turns those pins into TB nets. This module is the glue: it maps
`(gui, netmap)` into the exact JSON schema that `insitu.manifest` consumes, so the
result passes `manifest.validate(m)` and `manifest.measurements(m)` yields the right
point matrix for ANY count of rails/sinks/supplies.

Design rules baked in here (all from the SHARED CONTRACT):
  * Roles are NEVER inferred -- the GUI already tags each pin (supply / v_out / i_out).
    We only translate the tagging into the manifest vocabulary.
  * Role KEYS are short + stable, derived from the pin name (VDD0P8_DIG -> 'dig',
    IBP_POLY_500N_VCO_Fit -> 'i500n'), but the ORIGINAL pin name is preserved in a
    side field 'pin' on every port so the GUI/report can map back 1:1.
  * Bias policy is ASYMMETRIC (voltage vs current outputs -- the two ports linearize
    differently, see cadence/COMPANY_RUNBOOK.md / the alignment notes):
      - v_out 'iload': pure METADATA. The Zout probe augment adds is an isource with dc=0
        (AC only), so it never moves the OP -- the TB's own load biases the rail (TRUE
        in-situ). iload is emitted only when the user supplies one (a recorded operating-
        current tag), else omitted.
      - i_out 'dc': the COMPLIANCE voltage the augment probe vsource APPLIES at the pin --
        it REPLACES the node's DC driver (PORT-ISOLATED), so this is NOT "let the TB bias
        it". Supply vdc per i_out = the pin's real operating node voltage. If omitted,
        manifest.validate defaults dc=0.0 (a clamp to 0 V, rarely the real OP) and the key
        is reported in m['_warnings'] so the orchestrator can prompt for it.
  * current_psrr_supplies defaults to the single real supply (AVDD1P0).

Public API (pinned cross-component interface):
    build_manifest(gui: dict, netmap: dict) -> manifest dict
    write_manifest(gui: dict, netmap: dict, path) -> path

`gui` keys (see SHARED CONTRACT):
    tb_lib, tb_cell, tb_view, dut_inst, dut_lib, dut_cell,
    supply = {pin, dc},                     # single supply, e.g. AVDD1P0 @ 1.0
    v_outs = [pin, ...],                    # voltage rails (symbol pin names)
    i_outs = [pin, ...],                    # bias-current outputs (symbol pin names)
    ground,                                 # TB ground net (e.g. 'gnd!' / 'VSS')
    corner,                                 # process-corner label (informational)
    biases   = {pin: dc, ...}   (optional)  # held bias ports
    iload    = {pin: A,  ...}   (optional)  # per-v_out load current override
    vdc      = {pin: V,  ...}   (optional)  # per-i_out forced dc (compliance OP) override
    iv_sweep = {pin: [vlo,vhi,step] | "auto"}  (optional)  # per-i_out I-V knee sweep (G5)
    temps    = [degC, ...]      (optional)  # temperature points for Idc(T)/PTAT/noise(T)
    tnom_c   = degC             (optional)  # nominal/model-bake temp (default = middle of temps)
    name, tb_inst, extract_cell, extract_view, ade_src_test, analysis (all optional)

`netmap` : {pin -> net} from the B resolver (resolve_nets). Every pin referenced by the
GUI (supply.pin, each v_out, each i_out, each bias) MUST appear in netmap, else we raise
-- a missing net is a resolver gap, not something to guess.
"""
import json
import pathlib
import re

from . import manifest as _manifest
from .resolve import UNRESOLVED as _UNRESOLVED


class BuildError(ValueError):
    """A GUI/netmap input is internally inconsistent (missing net, dup role key, ...)."""


# ---------------------------------------------------------------------------
# role-key derivation: short, stable, collision-free keys from a symbol pin name
# ---------------------------------------------------------------------------
def _slug(pin):
    """Lowercase the pin and squeeze runs of non-alphanumerics to a single '_'."""
    return re.sub(r"[^0-9a-z]+", "_", pin.lower()).strip("_")


def role_key_v(pin):
    """Short role key for a voltage rail pin.

    Strategy: drop a leading supply-ish prefix (VDD<volt>_ / VOUT_ / V_) and keep the
    descriptive tail -- VDD0P8_DIG -> 'dig', VDD0P8_PLL -> 'pll', VDD0P8_VCO -> 'vco'.
    Falls back to the full slug if nothing is left.
    """
    s = _slug(pin)
    # strip a leading vdd<digits>p<digits> / vout / v token + its separator
    m = re.match(r"^(?:vdd[0-9p]*|vout|vo|v)_(.+)$", s)
    tail = m.group(1) if m else s
    return tail or s


def role_key_i(pin):
    """Short role key for a bias-current pin.

    Bias pins read like IBP_POLY_500N_VCO_Fit / IBP_PTAT_TUNE_1P5U_VCO -- the human-
    salient part is the magnitude token (500N, 1P8U, 1P5U). We key on 'i' + that token
    when present (i500n, i1p8u, i1p5u); else fall back to a compacted slug.
    """
    s = _slug(pin)
    mag = re.search(r"([0-9]+p?[0-9]*[fpnumk])", s)   # 500n, 1p8u, 1p5u, ...
    if mag:
        return "i" + mag.group(1)
    # no magnitude token: compact the slug, prefer the part after a leading ib*/i prefix
    m = re.match(r"^i[a-z]*_(.+)$", s)
    tail = (m.group(1) if m else s).replace("_", "")
    return "i" + tail if not tail.startswith("i") else tail


def role_key_s(pin):
    """Short role key for a supply pin: AVDD1P0 -> 'avdd1p0' (just the slug)."""
    return _slug(pin)


def _assign_keys(pins, keyfn, kind):
    """Build an ordered {key -> pin} map, de-duplicating collisions with a numeric
    suffix so two pins can never claim the same role key (which would silently merge
    them in the manifest)."""
    out = {}
    for pin in pins:
        base = keyfn(pin)
        if not base:
            raise BuildError(f"{kind} pin {pin!r} produced an empty role key")
        key = base
        n = 2
        while key in out:
            key = f"{base}{n}"
            n += 1
        out[key] = pin
    return out


def supply_pins(gui):
    """The supply PIN names from a gui dict, accepting either the legacy single
    gui['supply']={pin,..} or the multi gui['supplies']=[{pin,..},...]. Used by callers that
    build a netmap pin list or pick the model symbol's input pin."""
    if gui.get("supplies"):
        return [s["pin"] for s in gui["supplies"] if s.get("pin")]
    s = gui.get("supply") or {}
    return [s["pin"]] if s.get("pin") else []


def _cpsrr_keys(gui, s_keys):
    """Resolve current_psrr_supplies to a list of supply ROLE KEYS. Accepts the GUI giving
    supply PINS or role keys; default = ALL supplies (matches manifest._fill_defaults, so the
    builder and the validator agree). Single-supply -> [the one key], unchanged."""
    want = gui.get("current_psrr_supplies")
    if not want:
        return list(s_keys)                                # all supplies
    pin2key = {pin: k for k, pin in s_keys.items()}
    out = []
    for w in want:
        if w in s_keys:
            out.append(w)                                  # already a role key
        elif w in pin2key:
            out.append(pin2key[w])                         # a supply pin name
        else:
            raise BuildError(f"current_psrr_supplies entry {w!r} is not a supply pin or key "
                             f"(have pins {sorted(pin2key)} / keys {sorted(s_keys)})")
    return list(dict.fromkeys(out))                        # dedup (pin + its own slug), keep order


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------
def _net(netmap, pin, where):
    net = netmap.get(pin)
    if not net:
        raise BuildError(
            f"{where}: pin {pin!r} has no resolved net in netmap "
            f"(run the B resolver first; a missing net is a resolver gap, not a guess)")
    if net == _UNRESOLVED:
        # the B resolver returns this truthy marker for a pin it could not bind; it must NOT
        # leak into the manifest (it would flow into augment/import as a bogus net, or trip
        # validate's "tagged twice" on a 2nd unresolved pin -- a misleading red herring).
        raise BuildError(
            f"{where}: pin {pin!r} resolved to {_UNRESOLVED!r} -- the B resolver could not "
            f"bind it to a TB net. Check the instance/pin name and that the TB schematic is "
            f"checked + saved, then re-resolve before building the manifest.")
    return net


def build_manifest(gui, netmap):
    """Map (gui, netmap) -> a manifest dict that passes insitu.manifest.validate().

    Pure data transform: no simulator, no session, no file IO. The ORIGINAL symbol pin
    name is preserved on every port under 'pin' (and on the supply) for traceability;
    insitu.manifest ignores unknown keys, so this is forward-compatible.
    """
    if not isinstance(gui, dict):
        raise BuildError("gui must be a dict")
    if not isinstance(netmap, dict):
        raise BuildError("netmap must be a dict {pin -> net}")

    for req in ("tb_lib", "tb_cell", "dut_lib", "dut_cell"):
        if not gui.get(req):
            raise BuildError(f"gui.{req} is required")

    # supplies: accept either the legacy single gui['supply']={pin,dc} OR the multi form
    # gui['supplies']=[{pin,dc[,tb_src]},...]. Exactly one of the two; normalize to a list.
    if gui.get("supplies") is not None and gui.get("supply") is not None:
        raise BuildError("give either gui['supplies'] (list) or gui['supply'] (single), not both")
    if gui.get("supplies") is not None:
        supply_list = list(gui["supplies"])
        if not supply_list:
            raise BuildError("gui['supplies'] is empty (need at least one {pin,dc})")
    else:
        supply_list = [gui["supply"]] if gui.get("supply") else []
    if not supply_list:
        raise BuildError("gui needs a supply: gui['supply']={pin,dc} or "
                         "gui['supplies']=[{pin,dc},...] (e.g. AVDD1P0 @ 1.0)")
    seen_pins = set()
    for i, sup in enumerate(supply_list):
        if not (sup or {}).get("pin"):
            raise BuildError(f"supplies[{i}].pin is required (e.g. AVDD1P0)")
        if sup.get("dc") is None:
            raise BuildError(f"supplies[{i}].dc is required (a number, e.g. 1.0)")
        if sup["pin"] in seen_pins:
            raise BuildError(f"duplicate supply pin {sup['pin']!r} in gui['supplies']")
        seen_pins.add(sup["pin"])

    v_pins = list(gui.get("v_outs") or [])
    i_pins = list(gui.get("i_outs") or [])
    if not (v_pins or i_pins):
        raise BuildError("gui must list at least one v_out or i_out pin to model")

    bias_in = gui.get("biases") or {}
    iload_in = gui.get("iload") or {}
    vdc_in = gui.get("vdc") or {}
    iv_in = gui.get("iv_sweep") or {}            # per i_out I-V knee sweep: [vlo,vhi,step] | "auto"
    # UNIFIED SOURCE-REUSE: the existing TB source to REUSE on each port, GUI-collected. Absent
    # -> the netlister auto-detects the source on the net (or, for an OPEN v_out/i_out pin, falls
    # back to inserting Iext/Vprobe). We do NOT fabricate a name -- a fabricated probe_src would
    # name a non-existent source and trip the type/exists guardrail.
    v_src_in = gui.get("v_src") or gui.get("src") or {}        # per v_out: existing load isource
    i_src_in = gui.get("i_src") or gui.get("probe_src") or {}  # per i_out: existing vdc vsource

    # --- DUT block -------------------------------------------------------
    tb_cell = gui["tb_cell"]
    dut = {
        "lib": gui["dut_lib"],
        "cell": gui["dut_cell"],
        "tb_lib": gui["tb_lib"],
        "tb_cell": tb_cell,
        "tb_inst": gui.get("dut_inst") or gui.get("tb_inst") or "",
        "extract_cell": gui.get("extract_cell") or f"{tb_cell}_extract",
    }
    if gui.get("tb_view"):
        dut["tb_view"] = gui["tb_view"]
        dut.setdefault("extract_view", gui.get("extract_view") or gui["tb_view"])
    elif gui.get("extract_view"):
        dut["extract_view"] = gui["extract_view"]
    if gui.get("ade_src_test"):
        dut["ade_src_test"] = gui["ade_src_test"]

    # --- supplies (N; dedup role keys exactly like v_out/i_out) ----------
    s_keys = _assign_keys([s["pin"] for s in supply_list], role_key_s, "supply")
    pin_to_sup = {s["pin"]: s for s in supply_list}
    supplies = {}
    for s_key, s_pin in s_keys.items():
        sup = pin_to_sup[s_pin]
        entry = {"net": _net(netmap, s_pin, f"supply.{s_key}"), "dc": sup["dc"], "pin": s_pin}
        if sup.get("tb_src"):
            entry["tb_src"] = sup["tb_src"]
        supplies[s_key] = entry

    # --- v_out rails -----------------------------------------------------
    v_keys = _assign_keys(v_pins, role_key_v, "v_out")
    v_out = {}
    for key, pin in v_keys.items():
        entry = {"net": _net(netmap, pin, f"v_out.{key}"), "pin": pin}
        # in-situ bias policy: only carry iload when the user supplied one
        if pin in iload_in and iload_in[pin] is not None:
            entry["iload"] = iload_in[pin]
        # reuse: the existing TB load isource, only when the GUI named one (else auto-detect)
        if v_src_in.get(pin):
            entry["src"] = v_src_in[pin]
        v_out[key] = entry

    # --- i_out current sinks --------------------------------------------
    i_keys = _assign_keys(i_pins, role_key_i, "i_out")
    i_out = {}
    no_compliance = []
    for key, pin in i_keys.items():
        entry = {"net": _net(netmap, pin, f"i_out.{key}"), "pin": pin}
        # reuse: the existing TB compliance vdc vsource to set acm on, only when the GUI named
        # one (else the netlister auto-detects the vsource on the net, or -- for an OPEN pin --
        # falls back to inserting Vprobe_<key>; the read derives the probe name the same way).
        if i_src_in.get(pin):
            entry["probe_src"] = i_src_in[pin]
        # A current output reuses the designer's vdc at the pin (it already applies the
        # compliance AND we add the AC mag). 'dc' here is the pin's operating node voltage;
        # supply vdc per i_out for a real OP. If omitted, manifest.validate defaults dc=0.0
        # (clamps the node to 0 V, almost never the real compliance) -> flag it in m['_warnings'].
        if pin in vdc_in and vdc_in[pin] is not None:
            entry["dc"] = vdc_in[pin]
        else:
            no_compliance.append(f"{key}({pin})")
        # I-V compliance-knee sweep (G5): per-pin [vlo, vhi, step] or "auto" (0 -> supply+margin).
        # User-defined so the same harness serves any project's pins/rails without a code edit.
        if pin in iv_in and iv_in[pin] is not None:
            entry["iv_sweep"] = iv_in[pin]
        i_out[key] = entry

    # --- held bias ports (optional) -------------------------------------
    bias = {}
    for pin, dc in bias_in.items():
        bkey = _slug(pin)
        bias[bkey] = {"net": _net(netmap, pin, f"bias.{bkey}"), "dc": dc, "pin": pin}

    m = {
        "name": gui.get("name") or f"{dut['cell']}".lower(),
        "dut": dut,
        "ground": gui.get("ground") or "gnd!",
        "supplies": supplies,
        "v_out": v_out,
        "i_out": i_out,
        "bias": bias,
        "leave_alone": list(gui.get("leave_alone") or []),
        "corners": {"pull_from_session": True,
                    "fallback": [gui.get("corner") or "nom"]},
        # current-PSRR reference supplies: explicit GUI subset (pins or keys), else ALL
        # supplies (matches manifest._fill_defaults). Single-supply -> [the one key].
        "current_psrr_supplies": _cpsrr_keys(gui, s_keys),
    }
    if gui.get("corner"):
        m["corner"] = gui["corner"]
    if gui.get("analysis"):
        m["analysis"] = dict(gui["analysis"])
    # temperature points (degC) for Idc(T)/PTAT/noise(T) -- user-defined per project / PDK;
    # the nominal (model-bake) temperature is the middle point (or m['tnom_c'] if given).
    # The RUN axis is coverage.temps (manifest.temps()/pmu_corner read ONLY there); a top-level
    # m['temps'] is invisible to the runner -> write the consumed location so the Extract-tab
    # temperature field actually drives the per-temperature sims. tnom_c stays top-level (emit reads it).
    if gui.get("temps"):
        tps = [float(t) for t in gui["temps"]]
        m.setdefault("coverage", {})["temps"] = tps
        m["tnom_c"] = float(gui.get("tnom_c", tps[len(tps) // 2]))
    elif gui.get("tnom_c") is not None:
        m["tnom_c"] = float(gui["tnom_c"])
    # current-output noise (coverage.enable.inoise) is OPT-IN: no tier auto-enables it, so the
    # from-scratch GUI builder MUST request it explicitly or a GUI-built manifest can never measure
    # the bias sinks' output-current noise (the Report tab's current-noise panel stays blank). This
    # is the symmetric write to coverage.temps above. Off -> leave the override out entirely (never
    # write inoise:false, which would mask a future tier default); coverage_enabled() reads the key.
    if gui.get("inoise"):
        m.setdefault("coverage", {}).setdefault("enable", {})["inoise"] = True

    warnings = []
    # in-situ compliance warning: current outputs whose compliance vdc was not supplied get
    # validate()'s dc=0.0 default below -- record them so the orchestrator can prompt the user
    # for the real node voltage (a 0 V clamp is almost never the true OP for a bias current).
    if no_compliance:
        warnings.append(
            "current-output compliance (vdc) not supplied for: " + ", ".join(no_compliance)
            + " -- defaulting dc=0.0 (clamps the pin to 0 V). Supply gui['vdc'][<pin>] = the "
              "pin's real operating node voltage for a meaningful OP.")
    # multi-supply: PSRR vs EVERY supply is measured + fitted, but the combined model cell exposes
    # ONE input port (the first supply) and emits PSRR only vs it. Make that drop LOUD, not silent.
    if len(supplies) > 1:
        skeys = list(supplies)
        warnings.append(
            f"multiple supplies {skeys}: the model emits PSRR only vs the LEFT-input supply "
            f"'{skeys[0]}'. PSRR vs {skeys[1:]} is measured + fitted but NOT emitted yet "
            f"(multi-supply-input model is pending). Drop to one supply if you only need rail #1.")
    if warnings:
        m["_warnings"] = warnings

    # final guard: must satisfy the consumer's schema (raises ManifestError otherwise).
    # validate() mutates i_out entries (defaults dc) -- run on the dict we return.
    _manifest.validate(m)
    return m


def write_manifest(gui, netmap, path):
    """Build the manifest and write it to `path` as pretty JSON. Returns the path."""
    m = build_manifest(gui, netmap)
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, indent=2) + "\n")
    return p


if __name__ == "__main__":
    # tiny self-demo against the EXACT real-PMU pins (placeholder nets)
    gui = dict(
        tb_lib="PMU_lib", tb_cell="PMU_TB", tb_view="schematic", dut_inst="I0",
        dut_lib="PMU_lib", dut_cell="PMU_top",
        supply={"pin": "AVDD1P0", "dc": 1.0},
        v_outs=["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
        i_outs=["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit", "IBP_PTAT_TUNE_1P5U_VCO"],
        ground="VSS", corner="tt_25c",
    )
    netmap = {p: f"net_{p}" for p in
              [gui["supply"]["pin"], *gui["v_outs"], *gui["i_outs"]]}
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    mm = build_manifest(gui, netmap)
    print(_manifest.summary(mm))
