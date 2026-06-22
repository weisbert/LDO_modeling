"""Offline (no-Virtuoso) group netlister -- the pure-CLI dual of insitu.augment.

The box default (insitu.pmu_corner.ade_group_netlist) netlists each measurement group's
one-hot in ADE over skillbridge. This module is the OFFLINE alternative: given ONE base
maestro `input.scs` (a .tran TB the designer hands us) it rewrites that base, BY TEXT, into
the per-group ac/noise extraction netlists the cluster sweep runs -- no live Virtuoso.

It is the offline analogue of augment.build_plan(m): the SAME stimulus placement, expressed
in Spectre netlist text instead of schematic ops, so the sign conventions feed importmp's
derivations identically (mirror augment's node orders EXACTLY).

UNIFIED SOURCE-REUSE MODEL: the designer's TB already places an idc/vdc on every v_out / i_out
pin, so EVERY role REUSES that existing source (sets its mag=acm) rather than appending a 2nd
driver (which would double-drive the node and contaminate the read). Per role:

  supply s -> REUSE the VSOURCE on net (supplies.<s>.tb_src OR auto-detect) -> mag=acm_supply_<s>
  v_out  o -> REUSE the ISOURCE on net (v_out.<o>.src   OR auto-detect) -> mag=acm_v_out_<o>
              (read = node voltage -> needs current injection -> an isource)
  i_out  c -> REUSE the VSOURCE on net (i_out.<c>.probe_src OR auto-detect) -> mag=acm_i_out_<c>
              (read = <source>:p current under a voltage drive -> needs a vsource)

FALLBACK (open pin): if NO source is named AND none auto-detected on a v_out/i_out net, fall
back to the OLD insert -- append  Iext_<o> (<ground> <net>) isource mag=acm  for a v_out, or
<probe> (<net> <ground>) vsource dc=<compliance> mag=acm  for an i_out -- so an open pin still
works (the only place the old Iext/Vprobe strings survive). A clear "fallback-insert" note prints.

TYPE GUARDRAIL: a named/detected role source's master must match the role (v_out->isource,
i_out->vsource, supply->vsource); a wrong-master named source is a clear error (the read math
depends on it).

Per group we emit a `parameters` line declaring EVERY acm var (default 0) with THIS group's
hot vars set to 1 -- AC superposition makes each saved port an identifiable transfer. The base
TB's own analysis statement(s) (the .tran) are STRIPPED (commented with a visible marker) so the
run carries ONLY this group's analysis (ac, or noise with its output port). The save set is the
union of every member point's targeted saves (node voltages + <probe>:p currents) -- never allpub.

This module NEVER reaches a session: it is pure text + the manifest. The npz firewall is
untouched -- it writes raw stimuli + targeted saves only; all ratios/PSRR stay in importmp.

  from cluster.netlist_augment import make_offline_group_netlister
  gnl = make_offline_group_netlister(base_input_scs, m, out_base)   # -> callable(group)->netlistdir
"""
import pathlib

# The net placeholder the resolver fills (insitu.resolve / the manifest templates emit
# '<net:PIN>'); an unresolved manifest still carries it. B+ net resolution (below) resolves a
# '<net:PIN>' against the base netlist: PIN that IS a real net -> PIN (the common net==pin case,
# zero hand-edits); PIN that is NOT a net -> a hard error (net!=pin needs the real TB net).
NET_PLACEHOLDER_PREFIX = "<net:"

# The simulator master each role's REUSED/inserted source must be: a v_out reads a node
# voltage (1 A AC injected) -> an isource; a supply / an i_out drive a voltage and read a node
# V / a probe :p current -> a vsource. The type guardrail rejects a named source of the wrong
# master, and auto-detect only matches the right master.
ROLE_MASTER = {"supplies": "vsource", "v_out": "isource", "i_out": "vsource"}

# The visible marker we prefix a stripped (commented-out) base analysis with, so the
# designer can eyeball exactly what the offline netlister removed.
STRIP_MARKER = "// [offline-netlister stripped analysis] "

# A conservative set of Spectre analysis keywords. A top-level statement is treated as an
# analysis (and stripped) when its SECOND whitespace token is one of these -- and its 2nd
# token does NOT begin with '(' (a '(' 2nd token means a node list -> an INSTANCE, never
# stripped). Covers the .tran TB analysis the base carries plus the common companions.
ANALYSIS_KEYWORDS = frozenset({
    "tran", "dc", "ac", "noise", "dcmatch", "stb", "pz", "sp", "pss", "pac",
    "pnoise", "pstb", "hb", "hbac", "hbnoise", "envlp", "montecarlo", "sweep",
    "info", "xf", "pxf", "qpss", "qpac", "qpnoise", "qpsp", "qpxf", "tdr", "sens",
})

# A stable analysis name for the emitted ac / noise statement (one per group netlist).
AC_NAME = "acz"
NOISE_NAME = "nz"
# Stable analysis names for the coverage DC sweep (I-V / dropout) and transient (slew) groups.
DC_NAME = "dcz"
TRAN_NAME = "trz"


class NetlistAugmentError(RuntimeError):
    """The offline netlister cannot safely produce a netlist: an unresolvable placeholder net
    (net!=pin), a role source that cannot be located (zero or ambiguous auto-detect), or a
    named source of the wrong master type for its role."""


# ----------------------------------------------------------------- base-netlist parsing
def _statement_tokens(line):
    """The whitespace tokens of a netlist line with its trailing comment stripped. Returns
    [] for a blank / comment-only / directive line we never treat as a statement."""
    s = line.strip()
    if not s:
        return []
    # strip a trailing line comment ('//' Spectre-lang, or '*'/';' only when leading)
    if s.startswith("//") or s.startswith("*") or s.startswith(";"):
        return []
    if s.startswith("simulator") or s.startswith("parameters") or s.startswith("global") \
            or s.startswith("include") or s.startswith("ahdl_include") \
            or s.startswith("save") or s.startswith("saveOptions") or s.startswith("subckt") \
            or s.startswith("ends") or s.startswith("model"):
        return []
    return s.split()


def _continues(raw):
    """A physical netlist line continues onto the next when it ends with a backslash (ignoring
    a trailing '//' line comment). This is Spectre's line-continuation -- maestro wraps long
    instance/analysis statements this way, so the rewriting passes MUST see logical statements."""
    code = raw.split("//", 1)[0].rstrip()
    return code.endswith("\\")


def _logical_lines(text):
    """Group physical lines into LOGICAL statements, merging backslash line-continuations.
    Returns [(logical_text, physical_lines)]: `logical_text` is the whole statement joined into
    one line with the trailing '\\'s removed (for detection / tokenizing); `physical_lines` is
    the verbatim raw lines that form it, so an UNMODIFIED statement re-emits byte-identical and a
    multi-line statement is never half-stripped or half-rewritten."""
    units, phys, parts = [], [], []
    for raw in text.splitlines():
        phys.append(raw)
        if _continues(raw):
            parts.append(raw.split("//", 1)[0].rstrip()[:-1].rstrip())   # drop the trailing '\'
            continue
        parts.append(raw)
        units.append((" ".join(p.strip() for p in parts).strip(), phys))
        phys, parts = [], []
    if phys:                                              # trailing line with a dangling backslash
        units.append((" ".join(p.strip() for p in parts).strip(), phys))
    return units


def _subckt_delta(logical):
    """The subckt-nesting change a LOGICAL statement makes: +1 for a subckt HEADER, -1 for an
    `ends`, 0 otherwise. Handles BOTH Spectre (`subckt`/`inline subckt`/`ends [name]`) and
    spice-lang (`.subckt`/`.ends [name]`); case-insensitive (spice directives may be upper-cased).
    A comment line (`//`/`*`/`;`) or any other statement is 0, so commented-out subckt text never
    perturbs the depth count. `simulator lang=...` is a plain statement here -> 0."""
    s = logical.strip()
    if not s:
        return 0
    toks = s.lower().split()
    first = toks[0]
    if first in ("subckt", ".subckt"):
        return +1
    if first == "inline" and len(toks) >= 2 and toks[1] == "subckt":
        return +1
    if first in ("ends", ".ends"):
        return -1
    return 0


def _scoped_logical_lines(text):
    """Like _logical_lines but also yields each statement's subckt-nesting DEPTH:
    (logical_text, physical_lines, depth), depth==0 == top level. A subckt HEADER is reported at
    the OUTER depth it opens from and its body at depth>=1; the matching `ends` is reported at the
    outer depth it closes back to. Every by-NAME resolver / rewriter iterates this and matches
    ONLY at depth==0, so a subckt-internal instance (e.g. a DUT pass device named `I1`) can never
    shadow a top-level TB source of the same name. A dangling/unclosed subckt simply leaves the
    tail at depth>0 (skipped by depth==0 matchers -- the safe behaviour)."""
    depth = 0
    for logical, phys in _logical_lines(text):
        delta = _subckt_delta(logical)
        if delta < 0:
            depth = max(0, depth + delta)                 # `ends`: report AT the outer depth
            yield logical, phys, depth
        else:
            yield logical, phys, depth                    # header reported at the outer depth...
            depth += delta                                # ...then descend into its body


def _parse_instance(logical):
    """Parse a LOGICAL netlist statement as an instance `<name> (<n0> <n1> ...) <master> <rest>`.
    Returns (name, nodes, master, rest_tokens) or None when the line is not an instance (a
    directive, an analysis, or a malformed node list). The single shared parser used by node-set
    building, source auto-detect, and scan_netlist_sources -- so they never drift."""
    toks = _statement_tokens(logical)
    if len(toks) < 2 or not toks[1].startswith("("):
        return None
    name = toks[0]
    joined = " ".join(toks[1:])
    if ")" not in joined:
        return None
    node_blob, _, rest = joined.partition(")")
    nodes = node_blob.lstrip("(").split()
    rest_toks = rest.split()
    master = rest_toks[0] if rest_toks else ""
    return name, nodes, master, rest_toks


def _base_nets(base_text):
    """The SET of every TOP-LEVEL net token in the base netlist (every node of every depth-0
    instance node-list). B+ net resolution checks a manifest pin against this set -- a manifest
    pin is always a top-level TB net, so a subckt-internal net of the same name must NOT count
    (it would falsely 'resolve' a pin that is not the real top-level net)."""
    nets = set()
    for logical, _phys, depth in _scoped_logical_lines(base_text):
        if depth != 0:
            continue
        inst = _parse_instance(logical)
        if inst:
            nets.update(inst[1])                          # the node list
    return nets


def _resolve_nets(m, base_text):
    """B+ net resolution: turn each role net of the form '<net:PIN>' into a real net using the
    base netlist. PIN that IS a net in the base -> PIN (the common net==pin case, resolved
    silently, in place). PIN that is NOT a net -> collected; any unresolvable -> raise. Mutates
    m's supplies/v_out/i_out/bias 'net' fields in place (the factory works on its own loaded m).
    Returns the set of base nets (reused by the source pre-resolve)."""
    nets = _base_nets(base_text)
    unresolvable = []
    for role in ("supplies", "v_out", "i_out", "bias"):
        for key, v in (m.get(role) or {}).items():
            net = str(v.get("net", ""))
            if not net.startswith(NET_PLACEHOLDER_PREFIX):
                continue
            pin = net[len(NET_PLACEHOLDER_PREFIX):].rstrip(">")
            if pin in nets:
                v["net"] = pin                            # net==pin -> resolve silently
            else:
                unresolvable.append(f"{role}.{key} (<net:{pin}>)")
    if unresolvable:
        raise NetlistAugmentError(
            "could not resolve placeholder net(s) against the base netlist: "
            + ", ".join(unresolvable) + ". Each listed pin name is NOT a net in the base "
            "netlist; set the real TB net in the manifest's 'net' field (run Mode A in the live "
            "Virtuoso, or hand-edit) -- net==pin resolves automatically, net!=pin needs the net.")
    return nets


def _is_analysis_statement(line):
    """A top-level statement is an analysis when its SECOND whitespace token is a known
    Spectre analysis keyword. A 2nd token beginning with '(' is a node list -> an INSTANCE,
    never an analysis (so `Xdut (...) PMU_top` is safe)."""
    toks = _statement_tokens(line)
    if len(toks) < 2:
        return False
    second = toks[1]
    if second.startswith("("):
        return False
    return second in ANALYSIS_KEYWORDS


def _strip_analyses(base_text):
    """Comment out every top-level analysis statement in the base netlist (the .tran TB
    analysis we replace per group). Returns (stripped_text, n_stripped). Visible marker so
    the designer can eyeball what was removed; INSTANCES are never touched. A multi-line
    (backslash-continued) analysis is commented on EVERY physical line with the trailing '\\'
    dropped, so no orphan continuation is left live below the comment."""
    out, n = [], 0
    for logical, phys in _logical_lines(base_text):
        if _is_analysis_statement(logical):
            for raw in phys:
                body = raw.rstrip()
                if body.endswith("\\"):
                    body = body[:-1].rstrip()             # neutralise the continuation
                out.append(STRIP_MARKER + body)
            n += 1
        else:
            out.extend(phys)                              # unmodified -> verbatim physical lines
    return "\n".join(out), n


# the manifest field naming the source-to-reuse, and the human label, per role
_SRC_FIELD = {"supplies": "tb_src", "v_out": "src", "i_out": "probe_src"}


def _find_instance(base_text, name):
    """The (name, nodes, master, rest) of the first TOP-LEVEL instance whose name == `name`, or
    None. Subckt-internal instances are skipped (depth>0) -- a DUT pass device named `I1` must
    never be returned for a manifest source named `I1`; the real source is the top-level one."""
    for logical, _phys, depth in _scoped_logical_lines(base_text):
        if depth != 0:
            continue
        inst = _parse_instance(logical)
        if inst and inst[0] == name:
            return inst
    return None


def _detect_src(base_text, net, master):
    """Auto-detect the instance of master `master` whose FIRST node == `net`. Returns the
    instance NAME, or None when NONE matches (the caller decides: fall back to insert for an
    OPEN v_out/i_out pin, or -- for a supply -- raise). >1 match of the right master on the net
    is AMBIGUOUS and always raises (the designer must name the source). The type guardrail lives
    here too: we only ever match the role's expected master, so a wrong-master source on the net
    is simply not detected (an OPEN pin) -- a WRONG-master *named* source is caught by the caller."""
    matches = []
    for logical, _phys, depth in _scoped_logical_lines(base_text):
        if depth != 0:
            continue
        inst = _parse_instance(logical)
        if not inst:
            continue
        name, nodes, m_master, _rest = inst
        if m_master == master and nodes and nodes[0] == net:
            matches.append(name)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    raise NetlistAugmentError(
        f"AMBIGUOUS {master} on net '{net}': {matches} all have it as their first node. "
        f"Disambiguate by naming the intended source in the manifest.")


# back-compat alias: the old supply-only name still works (callers / tests may reference it).
def _detect_supply_src(base_text, supply_net):
    src = _detect_src(base_text, supply_net, "vsource")
    if src is None:
        raise NetlistAugmentError(
            f"could not auto-detect a supply vsource driving net '{supply_net}' in the base "
            f"netlist. Add the source instance name as supplies.<s>.tb_src in the manifest.")
    return src


def _resolve_role_src(base_text, role, key, v):
    """Pre-resolve ONE role object's source. Returns (src_name, fallback) where:
      * src_name is the instance to REUSE (set mag on), or None when no source -> fallback.
      * fallback is True ONLY for an OPEN v_out/i_out pin (no named source, none auto-detected)
        -- the caller then inserts the old Iext/Vprobe. A supply NEVER falls back (it has no
        own-source to inject; a missing supply source is a hard error).
    Type guardrail: a NAMED source must exist AND match the role's master; auto-detect only ever
    matches the right master. Raised early (factory-build time), never mid-sweep."""
    master = ROLE_MASTER[role]
    field = _SRC_FIELD[role]
    named = v.get(field)
    if named:
        inst = _find_instance(base_text, named)
        if inst is None:
            raise NetlistAugmentError(
                f"{role}.{key}.{field}='{named}' is not an instance in the base netlist "
                f"(named source not found). Check {role}.{key}.{field} against the base input.scs.")
        if inst[2] != master:
            # v_out/i_out can fall back to an inserted Iext_/Vprobe_; supplies cannot.
            escape = ("" if role == "supplies" else
                      f", or leave {role}.{key}.{field} blank to auto-insert a {master}")
            raise NetlistAugmentError(
                f"{role}.{key}.{field}='{named}' is a '{inst[2]}' but role {role} requires a "
                f"'{master}' (the read math: a v_out injects current -> isource; a supply/i_out "
                f"drives a voltage -> vsource). Name a {master} for {role}.{key}{escape}.")
        return named, False
    # no named source -> auto-detect the right master on the net
    det = _detect_src(base_text, v["net"], master)
    if det is not None:
        return det, False
    # nothing on the net: supplies hard-fail; v_out/i_out fall back to insert (open pin)
    if role == "supplies":
        raise NetlistAugmentError(
            f"could not auto-detect a supply vsource driving net '{v['net']}' (supplies.{key}) "
            f"in the base netlist. Add the source instance name as supplies.{key}.tb_src.")
    return None, True


def _resolve_supply_src(base_text, m, s, v):
    """The supply source instance name: tb_src override if present, else auto-detect. Kept for
    back-compat; the unified pre-resolve uses _resolve_role_src."""
    src, _fb = _resolve_role_src(base_text, "supplies", s, v)
    return src


def _set_mag_on_line(line, mag_expr):
    """Rewrite an instance line to carry mag=<mag_expr>: replace an existing `mag=...` token,
    else append one. Preserves a trailing comment if any (re-appended after the params)."""
    # split off a trailing line comment so we never append params inside it
    body, sep, comment = line.partition("//")
    toks = body.split()
    replaced = False
    for i, t in enumerate(toks):
        if t.startswith("mag="):
            toks[i] = f"mag={mag_expr}"
            replaced = True
            break
    if not replaced:
        toks.append(f"mag={mag_expr}")
    # preserve the original leading whitespace (indent) of the line
    indent = line[:len(line) - len(line.lstrip())]
    out = indent + " ".join(toks)
    if sep:
        out = out + " " + sep + comment
    return out


def _modify_mag(base_text, src_name, mag_expr):
    """Find ANY source instance by NAME (name-based + type-agnostic: works for the reused
    supply vsource, a v_out load isource, or an i_out vdc vsource alike) and set its
    mag=<mag_expr> in place. Returns (new_text, found). The first node-list instance whose name
    == src_name wins. A multi-line (backslash-continued) statement is collapsed to ONE clean
    line carrying the mag, so `mag=` never lands mid-statement after a dangling backslash."""
    out, found = [], False
    for logical, phys, depth in _scoped_logical_lines(base_text):
        toks = _statement_tokens(logical)
        if (not found and depth == 0 and len(toks) >= 2
                and toks[0] == src_name and toks[1].startswith("(")):
            out.append(_set_mag_on_line(logical, mag_expr))
            found = True
        else:
            out.extend(phys)                              # unmodified -> verbatim physical lines
    return "\n".join(out), found


# back-compat alias (the old supply-specific name; same name-based, type-agnostic setter).
_modify_supply_mag = _modify_mag


def _set_dc_on_line(line, value):
    """Rewrite an instance line to carry dc=<value:g>: replace an existing `dc=...` token, else
    append one. Mirrors _set_mag_on_line EXACTLY (trailing-comment + indent preserving)."""
    body, sep, comment = line.partition("//")
    toks = body.split()
    expr = f"{float(value):g}"
    replaced = False
    for i, t in enumerate(toks):
        if t.startswith("dc="):
            toks[i] = f"dc={expr}"
            replaced = True
            break
    if not replaced:
        toks.append(f"dc={expr}")
    indent = line[:len(line) - len(line.lstrip())]
    out = indent + " ".join(toks)
    if sep:
        out = out + " " + sep + comment
    return out


def _modify_dc(base_text, src_name, value):
    """Find ANY source instance by NAME (name-based + type-agnostic, mirrors _modify_mag) and set
    its dc=<value:g> in place. Returns (new_text, found). The first node-list instance whose name
    == src_name wins; a multi-line statement is collapsed to ONE clean line carrying the dc, so
    `dc=` never lands mid-statement after a dangling backslash. Used to set a rail's load per
    sweep point (op_loads)."""
    out, found = [], False
    for logical, phys, depth in _scoped_logical_lines(base_text):
        toks = _statement_tokens(logical)
        if (not found and depth == 0 and len(toks) >= 2
                and toks[0] == src_name and toks[1].startswith("(")):
            out.append(_set_dc_on_line(logical, value))
            found = True
        else:
            out.extend(phys)                              # unmodified -> verbatim physical lines
    return "\n".join(out), found


def _set_pwl_on_line(line, wave_tokens):
    """Rewrite an instance line into a PWL source: DROP its dc=.. and mag=.. tokens and append
    'type=pwl wave=[<wave_tokens>]', keeping the name + node list + master + any other params.
    Mirrors _set_mag_on_line's trailing-comment + indent handling."""
    body, sep, comment = line.partition("//")
    toks = [t for t in body.split() if not (t.startswith("dc=") or t.startswith("mag="))]
    toks.append("type=pwl")
    toks.append(f"wave=[{wave_tokens}]")
    indent = line[:len(line) - len(line.lstrip())]
    out = indent + " ".join(toks)
    if sep:
        out = out + " " + sep + comment
    return out


def _modify_to_pwl(base_text, src_name, wave_tokens):
    """Find ANY source instance by NAME (name-based, mirrors _modify_mag) and replace its dc=.. and
    mag=.. tokens with 'type=pwl wave=[<wave_tokens>]' in place (the node list + master kept).
    Returns (new_text, found). The first node-list instance whose name == src_name wins; a
    multi-line statement is collapsed to ONE clean line. Used to drive the transient stepped
    source (the reused v_out load isource)."""
    out, found = [], False
    for logical, phys, depth in _scoped_logical_lines(base_text):
        toks = _statement_tokens(logical)
        if (not found and depth == 0 and len(toks) >= 2
                and toks[0] == src_name and toks[1].startswith("(")):
            out.append(_set_pwl_on_line(logical, wave_tokens))
            found = True
        else:
            out.extend(phys)                              # unmodified -> verbatim physical lines
    return "\n".join(out), found


# --------------------------------------------------------------------- acm parameters
def _all_acm_vars(m):
    """Every acm_* design variable the manifest can drive, default 0 (mirrors
    augment.design_vars: one per v_out / supply / i_out stimulus)."""
    from insitu import manifest as _manifest          # function-local: avoid circular import
    out = []
    for o in m["v_out"]:
        out.append(_manifest.acm_var("v_out", o))
    for s in m["supplies"]:
        out.append(_manifest.acm_var("supply", s))
    for c in m["i_out"]:
        out.append(_manifest.acm_var("i_out", c))
    return out


def _params_line(m, group):
    """The `parameters` line: EVERY acm var default 0, THIS group's hot vars set.

    Plain ac/noise -> the one-hot (this group's hot vars = 1). An "ac" group carrying g['amp']
    (the 2x lin-gate self-check) sets its hot var to that amp value (e.g. 2.0) instead of 1. A
    "dc"/"tran" group has NO AC stimulus -> EVERY acm var is 0 (the swept/PWL source carries the
    stimulus); the var declarations stay present so the reused-source mag= references resolve."""
    from insitu import manifest as _manifest          # function-local: avoid circular import
    analysis = group["analysis"]
    if analysis in ("dc", "tran"):
        hotval = {}                                  # no AC stimulus -> every acm at 0
    else:
        amp = group.get("amp")
        val = f"{float(amp):g}" if amp else "1"      # ac-with-amp -> the amp; else the one-hot 1
        hotval = {_manifest.acm_var(k, v): val for k, v in group["hot"]}
    pairs = [f"{var}={hotval.get(var, '0')}" for var in _all_acm_vars(m)]
    return "parameters " + " ".join(pairs)


# ---------------------------------------------------------------------- the save set
def _save_line(m, group):
    """The targeted save set = union over the group's member points of pt['save']:
    ('v', net) -> 'NET' ; ('i', probe) -> '<probe>:p'. One `save` line, order-stable."""
    seen, items = set(), []
    for pt in group["members"]:
        for kind, ref in pt["save"]:
            tok = ref if kind == "v" else f"{ref}:p"
            if tok not in seen:
                seen.add(tok)
                items.append(tok)
    return "save " + " ".join(items)


# ----------------------------------------------------------------------- analysis line
def _ac_owner(m, group):
    """The (role, key) that OWNS an ac group's analysis line -- the one-hot stimulus mapped to
    the manifest role name ('supply'->'supplies', else the kind). Returns None for a group with
    no hot stimulus (the global ac line is used)."""
    if not group["hot"]:
        return None
    kind, key = group["hot"][0]
    role = "supplies" if kind == "supply" else kind     # 'v_out'/'i_out' map 1:1
    return role, key


def _noise_owner(m, group):
    """The v_out key that OWNS a noise group -- identified by oprobe net == v_out[o].net."""
    for o, v in m["v_out"].items():
        if v["net"] == group.get("oprobe"):
            return o
    return None


def _sweep_clause(sweep):
    """The Spectre DC-sweep clause for a coverage sweep dict: 'start=<a> stop=<b> lin=<n>' for a
    lin sweep, 'start=<a> stop=<b> log=<n>' for a log sweep (n = points). Mirrors _expand_sweep's
    type/start/stop/n contract so the offline sweep matches the manifest's intended grid."""
    a = float(sweep["start"]); b = float(sweep["stop"]); n = int(sweep.get("n", 0) or 0)
    kw = "log" if sweep.get("type") == "log" else "lin"
    return f"start={a:g} stop={b:g} {kw}={n}"


def _analysis_line(m, group, hot_src=None):
    """The group's analysis statement, with the PER-OBJECT analysis override applied (keyed by
    the group OWNER), falling back to the global m['analysis']. ac group -> '<AC_NAME> <ac>';
    noise group -> '<NOISE_NAME> (<oprobe> <ground>) <noise>' (the output port Spectre emits the
    'out' signal for -- importmp's noise read needs it).

    Coverage kinds:
      dc  (iv / dropout) -> '<DC_NAME> dc dev=<hot_src> param=dc <sweep>' (sweep the reused source
          of the group's single hot stimulus over g['sweep']; hot_src is its instance name).
      tran(slew)         -> '<TRAN_NAME> tran stop=<tstop:g>' (+ ' step=<tstep:g>' when set); the
          stepped source itself is rewritten to a PWL in the netlist text (not here)."""
    from insitu import manifest as _manifest          # function-local: avoid circular import
    analysis = group["analysis"]
    if analysis == "dc":
        sweep = group["sweep"]
        return f"{DC_NAME} dc dev={hot_src} param=dc {_sweep_clause(sweep)}"
    if analysis == "tran":
        e = group.get("edge") or 1e-9
        stop = group.get("tstop") or (e * 1000)
        line = f"{TRAN_NAME} tran stop={float(stop):g}"
        if group.get("tstep"):
            line += f" step={float(group['tstep']):g}"
        return line
    if analysis == "noise":
        ground = m["ground"]
        o = _noise_owner(m, group)
        noise = _manifest.analysis_line_for(m, "v_out", o, "noise") if o else m["analysis"]["noise"]
        return f"{NOISE_NAME} ({group['oprobe']} {ground}) {noise}"
    owner = _ac_owner(m, group)
    ac = (_manifest.analysis_line_for(m, owner[0], owner[1], "ac") if owner
          else m["analysis"]["ac"])
    return f"{AC_NAME} {ac}"


# The Spectre options statement we emit to run the whole netlist at a coverage temperature.
# Confirmed on local Spectre 18.1: `<name> options temp=<n>` is accepted (no fatal) and runs the
# whole netlist at that analog temperature -- the exact keyword IS `temp`. ALPS keyword unverified.
COVTEMP_NAME = "_covtemp"


def _group_hot_role_key(group):
    """The (role, key) of a group's single hot stimulus, role-mapped to the manifest role name
    ('supply'->'supplies', else the kind 1:1). None for a group with no hot stimulus."""
    if not group["hot"]:
        return None
    kind, key = group["hot"][0]
    role = "supplies" if kind == "supply" else kind
    return role, key


# --------------------------------------------------------------------------- the factory
def make_offline_group_netlister(base_input_scs, m, out_base, op_loads=None, temp=None):
    """Build the OFFLINE group_netlister: callable(group) -> netlistdir.

    Reads the base maestro `input.scs` ONCE (the designer's .tran TB) and, per group g,
    writes out_base/<g['tag']>/input.scs that strips the base analysis and appends g's
    extraction (one-hot ac/noise; or a DC sweep / transient PWL for the coverage kinds) plus
    targeted saves. The returned callable plugs straight into insitu.pmu_corner.step_run's
    group_netlister seam.

    base_input_scs   path to the base input.scs (a dir holding it, or the file itself).
    m                the loaded manifest dict (the single source of truth). B+ net resolution
                     runs here against the base netlist: a '<net:PIN>' where PIN is a real base
                     net resolves silently to PIN (mutated in place); net!=pin hard-stops.
    out_base         dir under which each group's <tag>/input.scs is written.
    op_loads         optional {v_out_key: dc_float}: AFTER the reuse-mag pass, rewrite each named
                     v_out's REUSED load isource's dc= to the given float (orchestration sets each
                     rail's load per sweep point). None (default) -> identical to today (the OP dc
                     the base carries). Keys not naming a reused v_out are ignored.
    temp             optional float: when set, the appended block emits a Spectre options
                     statement running the WHOLE netlist at <temp> (`options temp=`, confirmed
                     accepted on local Spectre 18.1). None (default) -> no temp line.
    """
    op_loads = op_loads or {}
    base_path = pathlib.Path(base_input_scs)
    if base_path.is_dir():
        base_path = base_path / "input.scs"
    if not base_path.is_file():
        raise NetlistAugmentError(
            f"base netlist not found: {base_path} (pass the dir holding input.scs, or the "
            f"file itself). The designer hands a base maestro .tran input.scs we rewrite.")
    base_text = base_path.read_text()
    out_base = pathlib.Path(out_base)

    # B+ net resolution FIRST (needs the base_text): resolve '<net:PIN>' where PIN is a real
    # base net (net==pin) in place; net!=pin raises. Subsequent measurements(m) see real nets.
    _resolve_nets(m, base_text)

    # pre-resolve every role's source ONCE -- (named tb_src/src/probe_src, else auto-detect by
    # the role's master on the net). Errors early (missing/ambiguous/wrong-master) at factory
    # build time, never mid-sweep. role_srcs[(role,key)] = (src_name|None, fallback_insert?).
    role_srcs = {}
    for role in ("supplies", "v_out", "i_out"):
        for key, v in m[role].items():
            src, fallback = role_srcs[(role, key)] = _resolve_role_src(base_text, role, key, v)
            # i_out: pin the REUSED vsource name into probe_src so manifest._probe_name (the
            # save token + the importmp :p read) targets the source we actually set mag on --
            # when probe_src was auto-detected (not named), without this the read would look for
            # the FALLBACK name Vprobe_<c>:p that the reused source does not emit.
            if role == "i_out" and src and not fallback:
                v["probe_src"] = src

    def _apply_op_loads(text):
        """Rewrite each named v_out's REUSED load isource dc= to op_loads[key] (the orchestration
        per-point rail load). Only v_out roles with a reused (non-fallback) source are touched;
        keys not naming such a v_out are ignored. A no-op when op_loads is empty."""
        for key, dc in op_loads.items():
            v = m.get("v_out", {}).get(key)
            if not v:
                continue                                # not a v_out role -> ignore
            src, fallback = role_srcs.get(("v_out", key), (None, True))
            if not src or fallback:
                continue                                # open pin (no reused isource) -> ignore
            text, _found = _modify_dc(text, src, dc)
        return text

    def _hot_src(group):
        """The reused source instance name of a coverage group's single hot stimulus (the swept
        DC source / the stepped transient source). Raises if the hot pin is open (no source to
        sweep/step -- a coverage point needs a real source on its rail)."""
        rk = _group_hot_role_key(group)
        if rk is None:
            raise NetlistAugmentError(
                f"coverage group {group['tag']} ({group['analysis']}) has no hot stimulus to "
                f"sweep/step.")
        src, fallback = role_srcs.get(rk, (None, True))
        if not src or fallback:
            raise NetlistAugmentError(
                f"coverage group {group['tag']} ({group['analysis']}) hot {rk} has no reused "
                f"source on its net (open pin). Name/place a source on {rk[0]}.{rk[1]} to "
                f"sweep/step it.")
        return src

    def _netlister(group):
        from insitu import manifest as _manifest      # function-local: avoid circular import
        ground = m["ground"]
        analysis = group["analysis"]
        # 1) start from the base, strip its .tran TB analysis (commented, visibly marked)
        text, n_stripped = _strip_analyses(base_text)
        inserts = []                                   # (role, key) that fall back to insert
        hot_src = None                                 # the dc-swept source name (dc groups only)

        if analysis in ("ac", "noise"):
            # 2a) REUSE every existing role source: set its mag=acm in place. supplies always
            #     reuse (a missing supply source already errored at factory build); v_out/i_out
            #     reuse when a source was found, else fall back to inserting Iext/Vprobe (open pin).
            for role in ("supplies", "v_out", "i_out"):
                kind = "supply" if role == "supplies" else role
                for key, v in m[role].items():
                    acm = _manifest.acm_var(kind, key)
                    src, fallback = role_srcs[(role, key)]
                    if fallback:
                        inserts.append((role, key))
                        continue
                    text, found = _modify_mag(text, src, acm)
                    if not found:                      # pre-resolve named it but it vanished
                        raise NetlistAugmentError(
                            f"{role}.{key} source '{src}' is not an instance in the base netlist "
                            f"-- named/auto-detect picked a source that does not exist. Check "
                            f"{role}.{key}.{_SRC_FIELD[role]} against the base input.scs.")
            text = _apply_op_loads(text)               # per-point rail loads (op_loads)
        elif analysis == "dc":
            # 2b) DC sweep (iv / dropout): do NOT one-hot mag -- every source keeps its OP dc.
            #     The group's single hot stimulus is SWEPT by the dc analysis (dev=<hot_src>).
            #     op_loads still set the non-swept rails' loads (the swept rail's dc is overridden
            #     by the sweep regardless).
            hot_src = _hot_src(group)
            text = _apply_op_loads(text)
        elif analysis == "tran":
            # 2c) transient slew: rewrite the group's stepped source (the reused v_out load
            #     isource) to a PWL stepping from->to; every other source keeps its OP dc.
            step = group["step"]
            f, t = float(step["from"]), float(step["to"])
            e = group.get("edge") or 1e-9
            stop = group.get("tstop") or (e * 1000)
            t0 = stop * 0.1
            t1 = t0 + e
            wave = f"0 {f:g} {t0:g} {f:g} {t1:g} {t:g} {stop:g} {t:g}"
            text = _apply_op_loads(text)               # other rails' loads (the stepped one PWL'd)
            stepped = _hot_src(group)
            text, found = _modify_to_pwl(text, stepped, wave)
            if not found:
                raise NetlistAugmentError(
                    f"tran group {group['tag']}: stepped source '{stepped}' is not an instance "
                    f"in the base netlist.")
        else:
            raise NetlistAugmentError(
                f"group {group['tag']}: unknown analysis '{analysis}'.")

        # 3) build the appended extraction block
        lines = [
            "",
            "// ============================================================",
            f"// [offline-netlister] group {group['tag']} -- "
            f"{analysis} one-hot {group['hot']} "
            f"({n_stripped} base analysis stmt(s) stripped above)",
            "// ============================================================",
            "simulator lang=spectre",          # guard a trailing spice-lang section
            _params_line(m, group),
        ]
        # FALLBACK-INSERT (open pin only, ac/noise path): the old Iext/Vprobe strings -- the ONLY
        # place they survive. v_out isource PLUS=ground MINUS=net (+1A into the out net, mirrors
        # augment); i_out probe vsource PLUS=net MINUS=ground dc=<compliance>; read <probe>:p.
        for role, key in inserts:
            v = m[role][key]
            if role == "v_out":
                acm = _manifest.acm_var("v_out", key)
                lines.append(f"Iext_{key} ({ground} {v['net']}) isource mag={acm}")
            else:                                       # i_out
                probe = _manifest._probe_name(m, key)
                acm = _manifest.acm_var("i_out", key)
                lines.append(f"{probe} ({v['net']} {ground}) vsource "
                             f"dc={float(v['dc']):g} mag={acm}")
            print(f"[netlist_augment] fallback-insert {role}.{key} "
                  f"(open pin on net '{v['net']}' -- no TB source to reuse)")
        # run the whole netlist at the coverage temperature (one options line); the `temp` keyword
        # is confirmed accepted on local Spectre 18.1 (see COVTEMP_NAME).
        if temp is not None:
            lines.append(f"{COVTEMP_NAME} options temp={float(temp):g}   "
                         f"// coverage temperature (Spectre `options temp=`, validated locally)")
        # the group's analysis (ac/noise; or the dc sweep / tran) + the targeted save union
        lines.append(_analysis_line(m, group, hot_src=hot_src))
        lines.append(_save_line(m, group))
        lines.append("")

        netdir = out_base / group["tag"]
        netdir.mkdir(parents=True, exist_ok=True)
        (netdir / "input.scs").write_text(text + "\n" + "\n".join(lines))
        print(f"[netlist_augment] {group['tag']}: stripped {n_stripped} base analysis "
              f"stmt(s), wrote {analysis} netlist -> {netdir/'input.scs'}")
        return str(netdir)

    return _netlister


# ------------------------------------------------------------------------ scan (GUI)
def _parse_dc(rest_tokens):
    """The float value of a `dc=<x>` token in an instance's trailing parameter tokens, or None.
    Tolerates SI suffixes Spectre accepts (500u, 1.28, 1p5) -- best-effort float() with a small
    suffix map; an unparseable dc returns None (the GUI shows it raw / blank)."""
    _SUF = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
            "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    for t in rest_tokens:
        if t.startswith("dc="):
            raw = t[3:]
            try:
                return float(raw)
            except ValueError:
                if raw and raw[-1] in _SUF:
                    try:
                        return float(raw[:-1]) * _SUF[raw[-1]]
                    except ValueError:
                        return None
                return None
    return None


def _detect_on_net(base_text, net, prefer_master=None):
    """Scan for a source whose first node == net. Prefers a 2-terminal source master (vsource/
    isource) -- so the DUT subckt instance (also first-node==net) never masquerades as the
    driver -- and, among those, prefers `prefer_master` (the role's expected master). Returns
    (name, master, dc, rest) for the chosen instance, or None when nothing drives the net.

    Selection order: (1) an instance of prefer_master; else (2) any vsource/isource; else (3)
    None. The GUI flags a (2)-only hit as type_ok=False (a wrong-master driver on the pin)."""
    src_masters = {"vsource", "isource"}
    preferred, any_src = None, None
    for logical, _phys, depth in _scoped_logical_lines(base_text):
        if depth != 0:
            continue
        inst = _parse_instance(logical)
        if not inst:
            continue
        name, nodes, master, rest = inst
        if not (nodes and nodes[0] == net and master in src_masters):
            continue
        hit = (name, master, _parse_dc(rest), rest)
        if prefer_master is not None and master == prefer_master and preferred is None:
            preferred = hit
        if any_src is None:
            any_src = hit
    return preferred or any_src


def scan_netlist_sources(base_input_scs, m):
    """Scan the base netlist for the driving source of every role+key (the GUI 'detected source'
    view). Applies B+ net resolution first (scans by resolved net; for an unresolvable <net:PIN>
    it scans by PIN). NEVER raises on a missing source -- returns instance=None, type_ok=False so
    the GUI can show 'not found'.

    Returns: {"supplies":{s:{instance,master,dc,net,type_ok}}, "v_out":{...}, "i_out":{...},
              "bias":{...}}. For each entry:
      instance = the driving instance NAME (named tb_src/src/probe_src if it exists in the base,
                 else the source auto-detected on the net), or None.
      master   = its simulator master (vsource/isource/...), or None.
      dc       = the float of its dc= token, or None.
      net      = the resolved net it scanned (the PIN for an unresolvable placeholder).
      type_ok  = master matches the role's expected master (bias has no expected master -> True
                 when an instance was found)."""
    base_path = pathlib.Path(base_input_scs)
    if base_path.is_dir():
        base_path = base_path / "input.scs"
    base_text = base_path.read_text()

    def _net_of(v):
        net = str(v.get("net", ""))
        if net.startswith(NET_PLACEHOLDER_PREFIX):       # B+: scan by the PIN (resolved or not)
            return net[len(NET_PLACEHOLDER_PREFIX):].rstrip(">")
        return net

    out = {"supplies": {}, "v_out": {}, "i_out": {}, "bias": {}}
    for role in ("supplies", "v_out", "i_out", "bias"):
        want_master = ROLE_MASTER.get(role)              # None for bias (no driver constraint)
        for key, v in (m.get(role) or {}).items():
            net = _net_of(v)
            named = v.get(_SRC_FIELD.get(role, "")) if role != "bias" else None
            inst = _find_instance(base_text, named) if named else None
            if inst is None:                             # named missing / no name -> detect on net
                hit = _detect_on_net(base_text, net, prefer_master=want_master)
            else:
                name, nodes, master, rest = inst
                hit = (name, master, _parse_dc(rest), rest)
            if hit is None:
                out[role][key] = dict(instance=None, master=None, dc=None,
                                      net=net, type_ok=False)
            else:
                name, master, dc, _rest = hit
                type_ok = True if want_master is None else (master == want_master)
                out[role][key] = dict(instance=name, master=master, dc=dc,
                                      net=net, type_ok=type_ok)
    return out
