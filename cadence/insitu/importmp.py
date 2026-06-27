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

Coverage-gated kinds (STAGE 1b -- these read RAW points; the extraction of slope/slew
lives model-side in stage 2, never here):
    iv     I-V curve    = [Vsweep, I.real]  (DC sweep of the reused i_out vdc -> probe :p)
    trans  slew wave    = [t, V.real]       (transient: time axis + the v_out node voltage)

Output schema (SAME keys cadence/extract_pmu.py writes, validated against the trusted
results/ref/pmu_standin.npz by the P4 gate):
    z_<o>_<load>, couple_<a>_<b>_<load>, noise_<o>_<load>,
    p_<o>_<s>_<load>, y_<c>_<load>, pi_<c>_<s>_<load>     (+ loads, meta_*)
  coverage-gated (STAGE 1b, present only when the manifest declares the params):
    iv_<c>_<load> = [Vsweep, I], dc_<o>_<load> = [Iload, Vout], tr_<o>_<label> = [t, V]
  GUARDRAIL-3: check_zout_dc_consistency(ref, manifest) cross-checks Zout(s->0) vs the DC
  load-reg slope (a cheap post-assemble sanity warning, never a hard gate).

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


_EXT = {"ac": ".ac", "noise": ".noise", "dc": ".dc", "tran": ".tran"}


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


def _read_noise_out(d, probe, where):
    """Read the current-output noise PSD of a probe-form `.noise` analysis. Spectre may name the
    output the conventional 'out' (the oprobe output signal) or a probe-named key -> try in order.
    Box-confirm which the run actually writes (the only unknown in this read path)."""
    for key in ("out", f"{probe}:p", probe):
        if key and key in d:
            return np.asarray(d[key])
    raise KeyError(f"{where}: current-noise output not saved (need 'out' or '{probe}'). "
                   f"available: {[k for k in d if k != '_sweep']}")


def _sweep_axis(d, where, swept=None, kinds=("dc", "sweep")):
    """The swept independent variable of a DC sweep PSF (the I-V vdc voltage / dropout iload).

    We emit the Spectre DC sweep as `dc dev=<src> param=dc ...` (netlist_augment._analysis_line),
    so psf.read_psf names the sweep column "dc" and stamps d["_sweep"]="dc". This helper is
    ROBUST to the box's PSF axis naming -- binary PSF / ALPS can name the swept-variable column by
    the swept PARAMETER, by "dc"/"sweep", or by the source's own dc node -- so it tries, in order:
    the explicit swept-param name (when the caller knows it), the d["_sweep"]-named column, then the
    conventional keys, and as a last resort any non-probe-current numeric column. Confirmed on
    local Spectre 18.1 (psfascii): the DC-sweep axis is named exactly "dc" and d["_sweep"]=="dc",
    so the first two candidates hit and the fallbacks never fire here. The fallbacks remain ONLY
    for the box's BINARY PSF / ALPS, whose swept-variable column name is still unconfirmed."""
    cands = []
    if swept:
        cands.append(swept)
    sw = d.get("_sweep")
    if sw:
        cands.append(sw)
    cands += list(kinds) + ["_sweep"]
    for k in cands:
        if k and k != "_sweep" and k in d:
            return np.asarray(d[k]).real
    # last resort: the first plain (non-current, non-marker) column -- the source's own dc node
    for k, v in d.items():
        if k in ("_sweep", "freq", "time") or k.endswith(":p"):
            continue
        arr = np.asarray(v)
        if arr.dtype.kind in "fi":                    # a real-valued swept node, not a complex sig
            return arr.real
    raise KeyError(f"{where}: no DC sweep axis in PSF "
                   f"(tried {cands}). available: {[k for k in d if k != '_sweep']}")


def _time_axis(d, where):
    """The transient TIME axis. psf.read_psf names the tran sweep column "time" (Spectre) and
    stamps d["_sweep"]="time"; a box binary PSF may surface it under that stamped name. Confirmed
    on local Spectre 18.1 (psfascii): the tran axis is literally "time". We accept
    ONLY a genuine time axis: the literal "time" column, or the _sweep-stamped column WHEN the stamp
    itself denotes time. A foreign axis (e.g. an AC PSF's "freq" mis-routed to a tran read) must FAIL
    LOUD here rather than be returned as a wrong silent array."""
    if "time" in d:
        return np.asarray(d["time"]).real
    stamp = d.get("_sweep")
    if stamp and "time" in str(stamp).lower() and stamp in d:
        return np.asarray(d[stamp]).real
    raise KeyError(f"{where}: no transient time axis in PSF (need a 'time' column or a time-stamped "
                   f"_sweep; refusing a non-time axis). available: {[k for k in d if k != '_sweep']}")


def _passivity_sign(f, Z):
    """Sign that makes a driving-point impedance PHYSICAL: +1 if Re(Z(s->0)) >= 0 already, -1 if
    the array must be flipped. A STABLE regulator's closed-loop Zout is positive-real, so
    Re(Zout(s->0)) >= 0 is mandatory -- the orientation-agnostic discriminator for the
    reused-LOAD-source injection vs a dedicated/inserted injector. The unified source-reuse
    refactor (d0a9cf9) drives the rail's existing LOAD isource ('Iload (out ground)', which DRAWS
    +1A from the node) instead of the old inserted 'Iext (ground out)' / synthetic 'Iac (0 out)'
    (which PUSH +1A in); the raw V/I then differs by a GLOBAL sign and only the draw orientation
    comes out negative-real. The y/pi derives already absorb this same reused-vsource flip (Y=-I,
    PI=-I/Vsup); the z derive was missed.

    Anchored on the LOW-FREQUENCY real part (the clean positive resistive floor) via a median over
    the lowest ~decade -- robust to one noisy point, and immune to the HF phase wrap (|phase|->180
    near the output resonance) that a band-average would trip on. Inject-orientation GT (synthetic
    + open-pin insert path) is already positive-real -> +1 (UNTOUCHED, so the 220-test synthetic
    suite is unaffected); reuse-draw real-chip GT -> -1 (corrected). Keys on a physical invariant,
    NOT on port names / manifest / source ids -- so it generalizes to any chip and any draw
    orientation, and a genuinely positive-real array is never altered."""
    f = np.asarray(f).real
    Z = np.asarray(Z)
    if f.size == 0:
        return 1.0
    order = np.argsort(f)
    fo, Zo = f[order], Z[order]
    cut = max(fo[0] * 10.0, fo[min(4, fo.size - 1)])      # lowest decade, but at least 5 points
    return -1.0 if float(np.median(Zo[fo <= cut].real)) < 0.0 else 1.0


def _derive(point, d, probe_alias=None):
    """Apply one measurement point's derive rule to its parsed PSF dict -> npz array."""
    kind = point["derive"]
    rd = point["reads"]
    # coverage-gated DC/transient kinds have NO freq axis -- handle them before the AC freq read
    if kind == "iv":                                  # i_out vdc I-V sweep: [Vsweep, I.real]
        # the swept voltage (DC axis) vs the probe current into the sink. The probe key is the
        # reads[0][1] sink probe; reuse _read_current (it resolves <probe>:p + the CLI alias).
        # DC current is real (no AC phase) -> I.real is the physical current into the sink.
        V = _sweep_axis(d, point["tag"])
        I = _read_current(d, rd[0][1], point["tag"], probe_alias).real
        return np.c_[V, I]
    if kind == "dropout":                             # v_out load-isource DC sweep: [Iload, Vout]
        # the swept LOAD current (DC axis) vs the output node voltage -> the load-regulation /
        # dropout curve. The sibling of `iv` (sweep a reused source, read a real DC signal); here
        # the swept variable is the load isource current and the read is the v_out node voltage.
        # GUARDRAIL-3 (check_zout_dc_consistency) consumes this [Iload, Vout] array.
        Iload = _sweep_axis(d, point["tag"])
        V = _signal(d, rd[0][1], point["tag"]).real
        return np.c_[Iload, V]
    if kind == "trans":                               # transient slew waveform: [t, V.real]
        # the RAW waveform only. The slew-rate / LTI-subtracted-residual EXTRACTION is the model
        # side (stage 2), NOT here -- importmp's job is the firewall, not the fit.
        t = _time_axis(d, point["tag"])
        V = _signal(d, rd[0][1], point["tag"]).real
        return np.c_[t, V]
    f = _signal(d, "freq", point["tag"]).real
    if kind == "z":                                   # driving-point Zout: V at out (|1 A| in)
        V = _signal(d, rd[0][1], point["tag"])
        sgn = _passivity_sign(f, V)                   # enforce Re(Zout(s->0)) >= 0 (see helper)
        if sgn < 0:
            print(f"  importmp: z '{point['tag']}' was negative-real at DC (reused-load source "
                  f"draws from the node) -> sign-normalized to a passive Zout")
        return np.c_[f, sgn * V.real, sgn * V.imag]
    if kind == "couple":                              # transfer impedance V_b / I_a (out a->b)
        # The SAME reused-source injection sign applies as for z, but a TRANSFER impedance is not
        # constrained positive-real, so the passivity auto-detect (valid only for the driving-point
        # z) must NOT be used here. couple_<a>_<b> is not consumed by fit_multiport/report today;
        # when it is, carry the INJECTING port a's inject-sign (source node order) through to here.
        V = _signal(d, rd[0][1], point["tag"])
        return np.c_[f, V.real, V.imag]
    if kind == "noise":
        Sv = _signal(d, "out", point["tag"]).real
        return np.c_[f, Sv]
    if kind == "noise_i":                             # current-output noise PSD (probe-form .noise)
        # a current bias port's OUTPUT-CURRENT noise (A/rtHz): the probe vsource that holds the
        # sink pin reads the sink's output current, so a `noise ... oprobe=<probe>` analysis emits
        # its current noise. Spectre may surface that under the conventional 'out' key (the oprobe
        # output) or a probe-named key -> try 'out' first, then the probe's own key (box-confirm).
        In = _read_noise_out(d, rd[0][1], point["tag"]).real
        return np.c_[f, In]
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
                       probe_aliases=None, strict=True, optional_kinds=(), progress=None):
    """Read every measurement point's PSF -> the generalized multi-port npz dict for one
    `load` (corner) label. `manifest` is a loaded manifest dict; `root` selects the CLI
    layout, or pass an explicit `psf_map={tag: file|dir}` from run.py. `probe_aliases`
    maps a sink key -> a fallback PSF current key (for foreign probe names, e.g. the CLI
    gold's Vb500/Vb1u). With strict=False, points whose PSF is missing/unreadable are skipped.

    `optional_kinds` is a per-DERIVE allow-list of points that may be skipped EVEN under strict:
    a derive in this set whose PSF/signal is missing is recorded in out['_skipped'] instead of
    raising (the rest stay strict). Used by import-finished to tolerate a missing COVERAGE extra
    (slew/I-V/dropout) -- those enrich the model but the core transfers stand alone -- without
    letting it abort the whole import. `progress(done, total, tag)` is called before each point
    (tag=None on the final tick) so a GUI can show read progress."""
    m = manifest
    points = _manifest.measurements(m)
    optional = set(optional_kinds)
    out = {}
    n = len(points)
    for i, pt in enumerate(points):
        if progress:
            progress(i, n, pt["tag"])
        alias = None
        if probe_aliases and pt["reads"] and pt["reads"][0][0] == "i":
            # the sink key is the 2nd token of the tag's probe; map via the i_out key
            for c in m["i_out"]:
                if _manifest._probe_name(m, c) == pt["reads"][0][1]:
                    alias = probe_aliases.get(c)
        fpath = None
        try:
            fpath = _resolve(root, psf_map, pt["tag"], pt["analysis"])
            d = psf.read_psf(fpath)
            out[f"{pt['key']}_{load}"] = _derive(pt, d, probe_alias=alias)
        except (FileNotFoundError, KeyError) as e:
            if strict and pt["derive"] not in optional:
                raise
            # name the PSF FILE we actually read, so a skipped point is diagnosable (e.g. a
            # transient whose node wasn't saved -> 'available: [time]' + which .tran it came from).
            where = f" [read {fpath}]" if fpath else ""
            out.setdefault("_skipped", []).append(f"{pt['tag']}{where}: {e}")
    if progress:
        progress(n, n, None)
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
        # carry this rail's transient load-step waveforms (tr_<o>_<label>, [t,V]) into the
        # single-port view as tr_<label> (drop the <o>_ the way z_<o>_<il> collapses to z_<il>).
        # The AC sweep never carries DC load-regulation; these steps do (pre-step tail =
        # Vout@from, post-step tail = Vout@to), and the voltage fit reads them to build a
        # vreg(iload) load-reg schedule. Absent on a small-signal-only run.
        for k in ref:
            if isinstance(k, str) and k.startswith(f"tr_{o}_"):
                sp["tr_" + k[len(f"tr_{o}_"):]] = ref[k]
        # Map each transient view-key -> its (i_from, i_to) load currents from the manifest's
        # coverage.transient step DECLARATION (the source of truth). The fit must NOT parse the
        # currents out of the key: the manifest tag is tr_<o>_<label> with a user label that may
        # be opaque ("2m"/"3m") and has NO _<load> suffix, so string-parsing silently dropped
        # every real step. {} when no transient decl -> no load-reg schedule (byte-identical).
        tr_steps = {}
        for st in ((((m.get("coverage") or {}).get("transient") or {}).get(o) or {})
                   .get("steps") or []):
            try:
                lbl = st.get("label") or f"{st['from']:g}_{st['to']:g}"
                vk = f"tr_{lbl}"
                if vk in sp:
                    tr_steps[vk] = (float(st["from"]), float(st["to"]))
            except (KeyError, TypeError, ValueError):
                continue
        res[o] = {"npz": sp, "supplies": supmap, "loads": loads,
                  "primary_supply": prim, "tr_steps": tr_steps}
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


# --------------------------------------------------------------- GUARDRAIL-3 cross-check
def check_zout_dc_consistency(ref, manifest, tol=0.25):
    """GUARDRAIL-3 (HANDOFF_MODELING_COVERAGE §5.3): Zout(s->0) <-> DC load-reg consistency.

    A CHEAP cross-derive sanity check the orchestration calls AFTER assemble. For each v_out o
    that carries BOTH an AC Zout array (z_<o>_<load>) AND a DC dropout array (dc_<o>...), it
    compares the AC small-signal output resistance at s->0 against the LOCAL DC load-regulation
    slope dVout/dIload measured straight off the dropout curve. A fabricated / mis-scaled DC
    curve shows up immediately as a large mismatch (HANDOFF: "catches a fabricated/mis-scaled DC
    curve immediately").

    Returns a list[str] of human-readable WARNINGS (it never raises -- a guardrail informs, it
    does not gate). Returns [] when either array is absent for an output (nothing to check).

      Zout(0)  = |z| at the LOWEST-frequency row of z_<o>_<load>  (sqrt(re^2+im^2); col1=re,col2=im).
      dVout/dIload = robust least-squares slope of Vout vs Iload over the dropout sweep
                     (dropout array is [Iload, Vout] from the 'dropout' derive).
    We compare MAGNITUDES: a real LDO droops under load so dVout/dIload is NEGATIVE, while Zout(0) is
    an impedance magnitude (>=0); the physical output resistance is |dVout/dIload|, and consistency
    means |Zout(0)| ~= |dVout/dIload|. A relative mismatch
    |zdc - |slope|| / max(|slope|, eps) > tol -> one warning naming o + numbers.

    Pure (no I/O); operates on the assembled multi-port `ref` dict + the loaded `manifest`."""
    m = manifest
    eps = 1e-30
    warns = []
    for o in m["v_out"]:
        # the AC Zout: any load corner present (z_<o>_<load>); take the first found.
        zkey = next((k for k in ref if k.startswith(f"z_{o}_")), None)
        # the DC dropout curve: any key starting "dc_<o>" (dc_<o>, dc_<o>_<load>, ...).
        dkey = next((k for k in ref if k.startswith(f"dc_{o}")), None)
        if zkey is None or dkey is None:
            continue                                      # one side absent -> nothing to check
        z = np.asarray(ref[zkey])
        drop = np.asarray(ref[dkey])
        if z.ndim != 2 or z.shape[0] < 1 or z.shape[1] < 3 or drop.ndim != 2 or drop.shape[0] < 2:
            continue                                      # malformed -> skip (be defensive, no raise)
        # Zout at s->0 = |z| at the lowest-frequency row (z is [f, re, im]; rows may be ascending
        # OR not, so pick the min-frequency row rather than assume row 0).
        i0 = int(np.argmin(z[:, 0].real))
        zdc = float(np.hypot(z[i0, 1].real, z[i0, 2].real))
        # local DC load-reg slope dVout/dIload: a least-squares line over the (Iload, Vout) sweep.
        Iload = drop[:, 0].real.astype(float)
        Vout = drop[:, 1].real.astype(float)
        if np.ptp(Iload) <= eps:
            continue                                      # degenerate axis -> can't slope it
        slope = float(np.polyfit(Iload, Vout, 1)[0])      # dVout/dIload (signed; <0 for a real LDO)
        rout = abs(slope)                                 # the DC output resistance magnitude (V/A)
        rel = abs(zdc - rout) / max(rout, eps)            # compare MAGNITUDES (see docstring)
        if rel > tol:
            warns.append(
                f"GUARDRAIL-3: v_out '{o}' Zout(s->0)={zdc:.4g} ohm disagrees with DC "
                f"load-reg |dVout/dIload|={rout:.4g} ohm (signed slope {slope:.4g}; rel mismatch "
                f"{rel:.2f} > tol {tol:.2f}) -- check the dropout DC curve (fabricated / mis-scaled?).")
    return warns


def load_multiport(path):
    d = np.load(path, allow_pickle=True)
    return {k: d[k] for k in d.files}
