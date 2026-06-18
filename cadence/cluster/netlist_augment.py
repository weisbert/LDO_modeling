"""Offline (no-Virtuoso) group netlister -- the pure-CLI dual of insitu.augment.

The box default (insitu.pmu_corner.ade_group_netlist) netlists each measurement group's
one-hot in ADE over skillbridge. This module is the OFFLINE alternative: given ONE base
maestro `input.scs` (a .tran TB the designer hands us) it rewrites that base, BY TEXT, into
the per-group ac/noise extraction netlists the cluster sweep runs -- no live Virtuoso.

It is the offline analogue of augment.build_plan(m): the SAME stimulus placement, expressed
in Spectre netlist text instead of schematic ops, so the sign conventions feed importmp's
derivations identically (mirror augment's node orders EXACTLY):

  v_out  o -> APPEND  Iext_<o>  (<ground> <net>) isource mag=acm_v_out_<o>   [dc=0, AC only]
  i_out  c -> APPEND  <probe>   (<net> <ground>) vsource dc=<i_out.dc> mag=acm_i_out_<c>
  supply s -> MODIFY  the EXISTING supply source (tb_src OR auto-detect) -> mag=acm_supply_<s>

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
# '<net:PIN>'); an unresolved manifest still carries it. The offline netlister CANNOT run
# against placeholder nets (they would land in the netlist verbatim and mis-wire), so we
# guard on this prefix before producing anything.
NET_PLACEHOLDER_PREFIX = "<net:"

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


class NetlistAugmentError(RuntimeError):
    """The offline netlister cannot safely produce a netlist: an unresolved (placeholder)
    net, or a supply source that cannot be located (zero or ambiguous auto-detect)."""


# --------------------------------------------------------------------------- guards
def _resolved_nets_guard(m):
    """Require RESOLVED nets before producing anything. Scan supplies/v_out/i_out 'net'
    values; if any still carries the '<net:' placeholder prefix, raise -- the designer must
    resolve nets (Mode A in live Virtuoso) or hand-edit the manifest first."""
    bad = []
    for role in ("supplies", "v_out", "i_out"):
        for key, v in (m.get(role) or {}).items():
            net = str(v.get("net", ""))
            if net.startswith(NET_PLACEHOLDER_PREFIX):
                bad.append(f"{role}.{key}={net}")
    if bad:
        raise NetlistAugmentError(
            "the manifest still has UNRESOLVED placeholder nets: " + ", ".join(bad) + ". "
            "The offline netlister cannot run against '<net:...>' placeholders -- resolve the "
            "nets (run Mode A in the live Virtuoso) or hand-edit the manifest's 'net' fields "
            "to the real TB nets, then re-run.")


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


def _detect_supply_src(base_text, supply_net):
    """Auto-detect the vsource whose FIRST node == supply_net. A Spectre instance line reads
    `<name> (<n0> <n1> ...) <master> ...`; we match `vsource` masters whose first node is the
    supply net. Returns the instance NAME. Exactly one match -> use it; zero or >1 -> raise a
    clear NetlistAugmentError listing candidates and asking for tb_src."""
    matches, vsources = [], []
    for logical, _phys in _logical_lines(base_text):
        toks = _statement_tokens(logical)
        if len(toks) < 2 or not toks[1].startswith("("):
            continue
        name = toks[0]
        # gather the node list between the first '(' and the closing ')'
        joined = " ".join(toks[1:])
        if ")" not in joined:
            continue
        node_blob, _, rest = joined.partition(")")
        nodes = node_blob.lstrip("(").split()
        master = rest.split()[0] if rest.split() else ""
        if master != "vsource":
            continue
        vsources.append(name)
        if nodes and nodes[0] == supply_net:
            matches.append(name)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NetlistAugmentError(
            f"could not auto-detect a supply vsource driving net '{supply_net}' in the base "
            f"netlist (found vsources: {vsources or 'none'}). Add the source instance name as "
            f"supplies.<s>.tb_src in the manifest so the offline netlister knows which source "
            f"carries the supply AC magnitude.")
    raise NetlistAugmentError(
        f"AMBIGUOUS supply source for net '{supply_net}': {matches} all have it as their first "
        f"node. Disambiguate by setting supplies.<s>.tb_src to the intended source instance "
        f"name in the manifest.")


def _resolve_supply_src(base_text, m, s, v):
    """The supply source instance name: the manifest's tb_src override if present, else
    auto-detect by first-node match (decision 2)."""
    src = v.get("tb_src")
    if src:
        return src
    return _detect_supply_src(base_text, v["net"])


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


def _modify_supply_mag(base_text, src_name, mag_expr):
    """Find the supply source instance by NAME and set its mag=<mag_expr> in place. Returns
    (new_text, found). The first node-list instance whose name == src_name wins. A multi-line
    (backslash-continued) supply statement is collapsed to ONE clean line carrying the mag, so
    `mag=` never lands mid-statement after a dangling backslash."""
    out, found = [], False
    for logical, phys in _logical_lines(base_text):
        toks = _statement_tokens(logical)
        if not found and len(toks) >= 2 and toks[0] == src_name and toks[1].startswith("("):
            out.append(_set_mag_on_line(logical, mag_expr))
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
    """The one-hot `parameters` line: EVERY acm var default 0, THIS group's hot vars = 1."""
    from insitu import manifest as _manifest          # function-local: avoid circular import
    hot = {_manifest.acm_var(k, v) for k, v in group["hot"]}
    pairs = [f"{var}={'1' if var in hot else '0'}" for var in _all_acm_vars(m)]
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
def _analysis_line(m, group):
    """The group's analysis statement. ac group -> '<AC_NAME> <m.analysis.ac>'; noise group
    -> '<NOISE_NAME> (<oprobe> <ground>) <m.analysis.noise>' (the output port Spectre emits
    the 'out' signal for -- importmp's noise read needs it)."""
    if group["analysis"] == "noise":
        ground = m["ground"]
        return f"{NOISE_NAME} ({group['oprobe']} {ground}) {m['analysis']['noise']}"
    return f"{AC_NAME} {m['analysis']['ac']}"


# --------------------------------------------------------------------------- the factory
def make_offline_group_netlister(base_input_scs, m, out_base):
    """Build the OFFLINE group_netlister: callable(group) -> netlistdir.

    Reads the base maestro `input.scs` ONCE (the designer's .tran TB) and, per group g,
    writes out_base/<g['tag']>/input.scs that strips the base analysis and appends g's
    one-hot ac/noise extraction (stimuli + targeted saves). The returned callable plugs
    straight into insitu.pmu_corner.step_run's group_netlister seam.

    base_input_scs   path to the base input.scs (a dir holding it, or the file itself).
    m                the loaded manifest dict (the single source of truth; nets MUST be
                     resolved -- the placeholder guard trips otherwise).
    out_base         dir under which each group's <tag>/input.scs is written.
    """
    _resolved_nets_guard(m)                              # fail loud on '<net:...>' placeholders
    base_path = pathlib.Path(base_input_scs)
    if base_path.is_dir():
        base_path = base_path / "input.scs"
    if not base_path.is_file():
        raise NetlistAugmentError(
            f"base netlist not found: {base_path} (pass the dir holding input.scs, or the "
            f"file itself). The designer hands a base maestro .tran input.scs we rewrite.")
    base_text = base_path.read_text()
    out_base = pathlib.Path(out_base)

    # pre-resolve every supply source ONCE (auto-detect / tb_src) -- a missing/ambiguous
    # source is an error we want to raise at factory-build time, not mid-sweep.
    supply_srcs = {s: _resolve_supply_src(base_text, m, s, v)
                   for s, v in m["supplies"].items()}

    def _netlister(group):
        from insitu import manifest as _manifest      # function-local: avoid circular import
        ground = m["ground"]
        # 1) start from the base, strip its .tran TB analysis (commented, visibly marked)
        text, n_stripped = _strip_analyses(base_text)
        # 2) modify the supply source(s) in place to carry the supply-AC magnitude
        for s, v in m["supplies"].items():
            acm = _manifest.acm_var("supply", s)
            text, found = _modify_supply_mag(text, supply_srcs[s], acm)
            if not found:
                raise NetlistAugmentError(
                    f"supply source '{supply_srcs[s]}' (supplies.{s}) is not an instance in the "
                    f"base netlist -- tb_src/auto-detect named a source that does not exist. "
                    f"Check supplies.{s}.tb_src against the base input.scs.")

        # 3) build the appended one-hot extraction block
        lines = [
            "",
            "// ============================================================",
            f"// [offline-netlister] group {group['tag']} -- "
            f"{group['analysis']} one-hot {group['hot']} "
            f"({n_stripped} base analysis stmt(s) stripped above)",
            "// ============================================================",
            "simulator lang=spectre",          # guard a trailing spice-lang section
            _params_line(m, group),
        ]
        # v_out isources: PLUS=ground, MINUS=net (mirror augment: +1A into the out net)
        for o, v in m["v_out"].items():
            acm = _manifest.acm_var("v_out", o)
            lines.append(f"Iext_{o} ({ground} {v['net']}) isource mag={acm}")
        # i_out named probes: PLUS=net, MINUS=ground; dc=<compliance>; read <probe>:p
        for c, v in m["i_out"].items():
            probe = _manifest._probe_name(m, c)
            acm = _manifest.acm_var("i_out", c)
            lines.append(f"{probe} ({v['net']} {ground}) vsource dc={float(v['dc']):g} mag={acm}")
        # the group's analysis (ac, or noise with its output port) + the targeted save union
        lines.append(_analysis_line(m, group))
        lines.append(_save_line(m, group))
        lines.append("")

        netdir = out_base / group["tag"]
        netdir.mkdir(parents=True, exist_ok=True)
        (netdir / "input.scs").write_text(text + "\n" + "\n".join(lines))
        print(f"[netlist_augment] {group['tag']}: stripped {n_stripped} base analysis "
              f"stmt(s), wrote one-hot {group['analysis']} netlist -> {netdir/'input.scs'}")
        return str(netdir)

    return _netlister
