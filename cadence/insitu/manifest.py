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
      "supplies": { "<s>": {"net","dc","tb_src"?} },        # role: supply
      "v_out":    { "<o>": {"net","iload"?} },               # role: voltage output
      "i_out":    { "<c>": {"net","dc","probe_src"?} },      # role: current output (sink)
      "bias":     { "<b>": {"net","dc"} },                   # role: bias port (held, optional)
      "leave_alone": ["BIAS_EN", "PLL_CTRL<3:0>", ...],      # role: leave_alone
      "corners":  {"pull_from_session": true, "fallback": ["nom"]},
      "current_psrr_supplies": ["1p0"],          # which supplies to current-PSRR (subset)
      "analysis": {"ac": "...", "noise": "..."}  # ALPS/Spectre-shared analysis lines
    }

The two consumers:
  * augment (P2) reads supplies/v_out/i_out to know WHERE to append acm_* sources/probes
    and WHAT to save (targeted, never allpub).
  * importmp (P4) reads the SAME roles to know HOW to derive each contract array from the
    raw PSF (Zout=V@1A, PSRR=Vout/Vsup, Y=-I, etc.) -- the read-side is the dual of the
    stimulus-side, both pinned by this one manifest.

`measurements()` turns the roles into the explicit 8-point matrix (the single source of
truth shared by augment + import), so the two halves can never drift.
"""
import json
import pathlib

# canonical roles (the designer's tagging vocabulary)
ROLES = ("supply", "v_out", "i_out", "bias", "leave_alone")

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
    return m


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
    for o, v in m["v_out"].items():
        claim(v.get("net"), f"v_out.{o}")
    for c, v in m["i_out"].items():
        claim(v.get("net"), f"i_out.{c}")
        v.setdefault("dc", 0.0)
    for b, v in m["bias"].items():
        claim(v.get("net"), f"bias.{b}")
    for s in m["current_psrr_supplies"]:
        if s not in m["supplies"]:
            raise ManifestError(f"current_psrr_supplies references unknown supply '{s}'")
    return True


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
