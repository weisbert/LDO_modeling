"""P4 -- PSF -> generalized multi-port npz (the contract firewall, multi-port edition).

The read-side is the DUAL of the stimulus-side: the SAME manifest that told augment
where to inject tells us how to derive each contract array from the raw PSF. We read
ONLY raw signals (node voltages V, our probe currents <probe>:p) and compute every
ratio/PSRR in Python here -- never as an ADE/ALPS expression -- so the npz is
engine-agnostic (the whole point of the firewall).

Derivations (identical physics to cadence/extract_pmu.py, just sourced from PSF files
instead of in-process spectre runs):
    z      Zout         = V(out)            (1 A AC injected -> V/I = V)
    couple Z_a->b       = V(other_out)      (1 A AC at a, read b)
    psrr   H = Vout/Vsup                    (1 V AC on the supply)
    noise  Sv           = out               (noise analysis output PSD)
    y      Y = -I(probe)                    (1 V AC on the sink pin)
    pi     dI/dVsup = -I(probe)/Vsup        (1 V AC on the supply, read sink probe)

Output schema (SAME keys cadence/extract_pmu.py writes, validated against the trusted
results/ref/pmu_standin.npz by the P4 gate):
    z_<o>_<load>, couple_<a>_<b>_<load>, noise_<o>_<load>,
    p_<o>_<s>_<load>, y_<c>_<load>, pi_<c>_<s>_<load>     (+ loads, meta_*)

A "load" is a designer-defined OPAQUE corner label, pulled from the session (P3) or the
manifest fallback -- never interpreted here.

Two PSF layouts are handled transparently:
    CLI / dev fixture : <root>/<tag>/raw/<name>.ac|.noise        (extract_pmu work dirs)
    ADE / production  : an explicit {tag: psf_file_or_dir} map from run.py (P3), which
                        itself tried <corner>/<test>/psf/ (ALPS) and .../netlist/ (Spectre)
"""
import pathlib
import numpy as np

import psf                       # cadence/psf.py (on sys.path via insitu/__init__)
from . import manifest as _manifest


_EXT = {"ac": ".ac", "noise": ".noise"}


# --------------------------------------------------------------------------- locate
def _find_psf_file(path, analysis):
    """Resolve a single PSF file for an analysis from a file or a directory.
    A dir is searched (CLI: <dir>/raw/<x>.ac ; ADE: <dir>/{psf,netlist}/<x>.ac ; or the
    dir itself)."""
    p = pathlib.Path(path)
    ext = _EXT[analysis]
    if p.is_file():
        return p
    for sub in ("", "raw", "psf", "netlist"):
        d = p / sub if sub else p
        if d.is_dir():
            hits = sorted(f for f in d.iterdir() if f.is_file() and f.suffix == ext)
            if hits:
                return hits[0]
    raise FileNotFoundError(f"no {ext} PSF under {p} for analysis '{analysis}'")


def _resolve(root, psf_map, tag, analysis):
    if psf_map is not None:
        if tag not in psf_map:
            raise FileNotFoundError(f"psf_map has no entry for point '{tag}'")
        return _find_psf_file(psf_map[tag], analysis)
    if root is None:
        raise ValueError("provide either root=<dir> (CLI layout) or psf_map={tag:path}")
    return _find_psf_file(pathlib.Path(root) / tag / "raw", analysis)


# ----------------------------------------------------------------------- read+derive
def _signal(d, key, where):
    if key not in d:
        raise KeyError(f"{where}: signal '{key}' not in PSF (saved? targeted-save miss). "
                       f"available: {[k for k in d if k != '_sweep']}")
    return np.asarray(d[key])


def _read_current(d, probe, where, probe_alias):
    """Read a probe current by OUR named key <probe>:p; fall back to a provided alias
    (e.g. the CLI gold used Vb500/Vb1u where the manifest names Vprobe_*)."""
    for key in ([f"{probe}:p"] + ([probe_alias] if probe_alias else [])):
        if key and key in d:
            return np.asarray(d[key])
    raise KeyError(f"{where}: current '{probe}:p' not saved (currents need an EXPLICIT "
                   f"save, not allpub). available: {[k for k in d if k.endswith(':p')]}")


def _derive(point, d, probe_alias=None):
    """Apply one measurement point's derive rule to its parsed PSF dict -> npz array."""
    f = _signal(d, "freq", point["tag"]).real
    kind = point["derive"]
    rd = point["reads"]
    if kind in ("z", "couple"):                       # V at the read net (1 A injected)
        V = _signal(d, rd[0][1], point["tag"])
        return np.c_[f, V.real, V.imag]
    if kind == "noise":
        Sv = _signal(d, "out", point["tag"]).real
        return np.c_[f, Sv]
    if kind == "psrr":
        Vo = _signal(d, rd[0][1], point["tag"])
        Vs = _signal(d, rd[1][1], point["tag"])
        H = Vo / Vs
        return np.c_[f, H.real, H.imag]
    if kind == "y":
        I = _read_current(d, rd[0][1], point["tag"], probe_alias)
        Y = -I                                        # admittance into the sink (V=1)
        return np.c_[f, Y.real, Y.imag]
    if kind == "pi":
        I = _read_current(d, rd[0][1], point["tag"], probe_alias)
        Vs = _signal(d, rd[1][1], point["tag"])
        PI = -I / Vs
        return np.c_[f, PI.real, PI.imag]
    raise ValueError(f"unknown derive '{kind}'")


# --------------------------------------------------------------------------- assemble
def from_psf_multiport(root=None, *, manifest, psf_map=None, load="nom",
                       probe_aliases=None, strict=True):
    """Read every measurement point's PSF -> the generalized multi-port npz dict for one
    `load` (corner) label. `manifest` is a loaded manifest dict; `root` selects the CLI
    layout, or pass an explicit `psf_map={tag: file|dir}` from run.py. `probe_aliases`
    maps a sink key -> a fallback PSF current key (for foreign probe names, e.g. the CLI
    gold's Vb500/Vb1u). With strict=False, points whose PSF is missing are skipped."""
    m = manifest
    points = _manifest.measurements(m)
    out = {}
    for pt in points:
        alias = None
        if probe_aliases and pt["reads"] and pt["reads"][0][0] == "i":
            # the sink key is the 2nd token of the tag's probe; map via the i_out key
            for c in m["i_out"]:
                if _manifest._probe_name(m, c) == pt["reads"][0][1]:
                    alias = probe_aliases.get(c)
        try:
            fpath = _resolve(root, psf_map, pt["tag"], pt["analysis"])
            d = psf.read_psf(fpath)
            out[f"{pt['key']}_{load}"] = _derive(pt, d, probe_alias=alias)
        except (FileNotFoundError, KeyError) as e:
            if strict:
                raise
            out.setdefault("_skipped", []).append(f"{pt['tag']}: {e}")
    return out


def assemble_multiport(manifest, loads_psf, outpath=None, meta=None):
    """Combine per-load multi-port reads into one results/ref/<name>.npz.
    `loads_psf` = {load_label: (root_or_None, psf_map_or_None, probe_aliases_or_None)} OR
    {load_label: dict_already_read}. Returns the written path."""
    m = manifest
    loads = list(loads_psf.keys())
    ref = {"loads": np.array([str(x) for x in loads])}
    for lbl, spec in loads_psf.items():
        if isinstance(spec, dict) and not {"root", "psf_map"} & set(spec):
            ref.update(spec)                          # already-read arrays
        else:
            root = spec.get("root"); pmap = spec.get("psf_map")
            aliases = spec.get("probe_aliases")
            ref.update(from_psf_multiport(root=root, manifest=m, psf_map=pmap,
                                          load=lbl, probe_aliases=aliases,
                                          strict=spec.get("strict", True)))
    for k, v in (meta or {}).items():
        ref[f"meta_{k}"] = np.array(v)
    from . import ROOT
    REFDIR = ROOT / "results" / "ref"
    REFDIR.mkdir(parents=True, exist_ok=True)
    out = pathlib.Path(outpath) if outpath else (REFDIR / f"{m['name']}_ade.npz")
    np.savez(out, **ref)
    return out


# ------------------------------------------------------------------ downstream reuse
def split_ports(ref, manifest):
    """Explode a multi-port npz dict into per-voltage-output SINGLE-port views that the
    EXISTING fit_model / ModelerCore consume UNCHANGED (z_<load>, p_<load>, noise_<load>).

    Returns {output: {"npz": <single-port dict>, "supplies": {s: p_array}, "loads": [...]}}.
    The single-port `npz` uses the FIRST supply for its primary `p_<load>` (the legacy
    fitter takes one PSRR); the full per-supply set is in `supplies` for fit_multiport.
    Current ports are returned separately by `current_ports()`."""
    m = manifest
    loads = [str(x) for x in ref["loads"]]
    sups = list(m["supplies"])
    prim = sups[0] if sups else None
    res = {}
    for o in m["v_out"]:
        sp = {"loads": np.array(loads)}
        for mk in ("meta_cout", "meta_esr", "meta_vin1p0", "meta_vin1p8"):
            if mk in ref:
                sp[mk] = ref[mk]
        supmap = {}
        for il in loads:
            zk = f"z_{o}_{il}"
            if zk in ref:
                sp[f"z_{il}"] = ref[zk]
            nk = f"noise_{o}_{il}"
            if nk in ref:
                sp[f"noise_{il}"] = ref[nk]
            for s in sups:
                pk = f"p_{o}_{s}_{il}"
                if pk in ref:
                    supmap.setdefault(s, {})[il] = ref[pk]
            if prim and f"p_{o}_{prim}_{il}" in ref:
                sp[f"p_{il}"] = ref[f"p_{o}_{prim}_{il}"]   # legacy single-PSRR slot
        res[o] = {"npz": sp, "supplies": supmap, "loads": loads, "primary_supply": prim}
    return res


def current_ports(ref, manifest):
    """Per-current-sink views: {sink: {"loads", "y": {il:arr}, "pi": {(s,il):arr}}}."""
    m = manifest
    loads = [str(x) for x in ref["loads"]]
    res = {}
    for c in m["i_out"]:
        y = {il: ref[f"y_{c}_{il}"] for il in loads if f"y_{c}_{il}" in ref}
        pi = {(s, il): ref[f"pi_{c}_{s}_{il}"]
              for s in m["current_psrr_supplies"] for il in loads
              if f"pi_{c}_{s}_{il}" in ref}
        res[c] = {"loads": loads, "y": y, "pi": pi}
    return res


def load_multiport(path):
    d = np.load(path, allow_pickle=True)
    return {k: d[k] for k in d.files}
