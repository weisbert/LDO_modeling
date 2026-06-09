"""Cadence/ADE export -> harness reference npz  (the Import half of the GUI modeler).

This converts hand-exported testbench data (CSV, or best-effort PSF-ASCII) for a REAL
transistor-level LDO into the single `results/ref/<name>.npz` the behavioral-model harness
consumes. The npz schema is the contract in repo `CADENCE_EXTRACTION.md` (this module is its
executable mirror). After a successful assemble(), modeling the real LDO is just:

    fit_model.fit_variant("<name>")  ->  fit_model.emit(...) / emit_va(...)

------------------------------------------------------------------------------------------
CSV LAYOUT (defined here; finalize against the real export column order at integration):
  Every CSV is plain text, comma- OR whitespace-delimited, with an OPTIONAL header row of
  column names (used to auto-detect the complex format). One file per (quantity, corner).

  * complex transfer (Zout `z`, PSRR `p`, and their `_hf` extensions, optional `ibp`):
        freq, <complex>            where <complex> is one of, auto-detected by header:
        - real, imag               (header tokens re/real, im/imag)         fmt="reim"
        - mag,  phase_deg          (header mag + phase, degrees)            fmt="magdeg"
        - mag,  phase_rad          (header mag + phase_rad)                 fmt="magrad"
        - mag_db, phase_deg        (header db/mag_db + phase)               fmt="dbdeg"
      Zout `z`  := V(vout)/I  (1 A AC into vout, vin ideal-DC).  COMPLEX, ohms.
      PSRR `p`  := V(vout)/V(vin) transfer H (NOT attenuation-in-dB).  COMPLEX.
  * noise `noise` : freq, Sv         Sv in V/sqrt(Hz)  (set sv_is_psd2=True if V^2/Hz).
  * transient `trans_lin`/`trans_big`/`trans_slew` : time[s], vout[V].
  * dc `dc_loadreg`/`dc_dropout` : iload[A], vout[V].   `dc_linereg` : vin[V], vout[V].
  * spurs `spurs` : freq[Hz], amp[V], phase[rad]   (intrinsic vout tones; no stimulus).
  * spurs_raw `spurs_raw` : time[s], vout[V] -- a RAW intrinsic transient of v(vout) (NO external
        stimulus), ONE file per corner. When the pre-made spurs_<corner> tables are absent,
        assemble() auto-FFTs these via spur_char (coherent-window FFT -> peak-pick -> fundamental
        classification) to build spurs_<corner> + spur_F/spur_twin0/spur_binhz. Export the plain
        waveform from Cadence; no calculator FFT needed. Window/fmax via profile spur_twin/spur_fmax.
  * `spur_500u` (optional linearity gate) : freq[Hz], amp[V].
------------------------------------------------------------------------------------------
PROGRAMMATIC USE (what the GUI calls):
    prof  = dict(name="myldo", loads=["20u","121u","250u"], nominal="121u",
                 cout=1e-9, esr=0.5, vref=1.05, spur_twin0=0.0, spur_binhz=15625.0)
    files = {("z","20u"): "...z_20u.csv", ("p","20u"): "...", ("noise","20u"): "...", ...
             ("z_hf","121u"): "...", ("dc_loadreg",None): "...", ("spurs","121u"): "..."}
    path  = assemble(prof, files)            # -> results/ref/myldo.npz
    warns = validate(load_npz(path))         # guardrail messages for the GUI
"""
import argparse
import pathlib
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFDIR = ROOT / "results" / "ref"

# quantity keys that are per-corner complex transfers vs other shapes
CPLX_KINDS = {"z", "p", "z_hf", "p_hf", "ibp"}
PERCORNER = {"z", "p", "noise", "trans_lin", "spurs", "ibp"}      # one file per corner
NOMINAL_ONLY = {"z_hf", "p_hf", "trans_big", "trans_slew", "spur_500u"}   # nominal corner
GLOBAL = {"dc_loadreg", "dc_linereg", "dc_dropout"}              # one file total


# ----------------------------------------------------------------------------- readers
def _read_table(path):
    """Read a CSV/whitespace table -> (header_tokens|None, float ndarray[rows,cols]).
    A leading non-numeric row is taken as the header. Blank lines / '#'/'*' comments
    and a trailing unit row (all non-numeric) are skipped."""
    header, rows = None, []
    for raw in pathlib.Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line[0] in "#*;":
            continue
        toks = [t for t in line.replace(",", " ").split() if t]
        try:
            rows.append([float(t) for t in toks])
        except ValueError:
            if header is None and not rows:
                header = [t.lower() for t in toks]      # first non-numeric row = header
            # else: a stray non-numeric row (e.g. units) -> skip
    if not rows:
        raise ValueError(f"{path}: no numeric rows parsed")
    w = max(len(r) for r in rows)
    arr = np.array([r + [np.nan] * (w - len(r)) for r in rows], dtype=float)
    return header, arr


def _detect_fmt(header):
    """Infer complex format from header TOKENS; None -> caller default ('reim'). Raises on an
    ambiguous header (one complex component present without a valid pairing) so a mislabeled
    export fails loudly instead of being silently misread."""
    if not header:
        return None
    has_db = any(t == "db" or t.endswith("_db") or t.endswith("db") for t in header)
    has_phase = any(t.startswith("phase") or t in ("ph", "deg", "rad", "angle", "degrees") for t in header)
    # radians ONLY when a token actually denotes radians (not any substring containing 'rad')
    has_rad = any(t.endswith("rad") or t in ("rad", "radians") for t in header)
    has_imag = any(t in ("im", "imag", "imaginary") or t.startswith("imag") for t in header)
    has_real = any(t in ("re", "real") or t.startswith("real") for t in header)
    if has_real and has_imag:
        return "reim"
    if has_db and has_phase:
        return "dbrad" if has_rad else "dbdeg"
    if has_phase and not has_db:
        return "magrad" if has_rad else "magdeg"
    # a lone complex component with no pairing -> ambiguous; do not guess
    if (has_real != has_imag) or (has_db and not has_phase):
        raise ValueError(f"ambiguous complex format from header {header}: need re+im, mag+phase, "
                         "or db+phase. Pass fmt= explicitly (reim/magdeg/magrad/dbdeg/dbrad).")
    return None


def _to_complex(arr, fmt):
    """[freq, a, b] + fmt -> (freq, complex). fmt: reim|magdeg|magrad|dbdeg|dbrad."""
    if arr.shape[1] < 3:
        raise ValueError(f"complex transfer needs 3 columns (freq + 2), got shape {arr.shape}. "
                         "Check the CSV layout / delimiter, or that this file is really complex.")
    f, a, b = arr[:, 0], arr[:, 1], arr[:, 2]
    if fmt == "reim":
        return f, a + 1j * b
    mag = 10.0 ** (a / 20.0) if fmt in ("dbdeg", "dbrad") else a
    ph = np.radians(b) if fmt in ("magdeg", "dbdeg") else b
    return f, mag * np.exp(1j * ph)


def from_csv(path, kind, fmt=None, sv_is_psd2=False):
    """Read one CSV into its npz-ready array.
      complex kinds (z/p/z_hf/p_hf/ibp) -> [N,3] = f, Re, Im  (fmt auto from header).
      noise -> [M,2] = f, Sv[V/rtHz]   (sv_is_psd2=True: input is V^2/Hz -> sqrt taken).
      trans_* -> [T,2] (t,v); dc_* -> [L,2] (x,v); spurs -> [K,3] (f,amp,phase_rad);
      spur_500u -> [K,2] (f,amp)."""
    header, arr = _read_table(path)
    if kind in CPLX_KINDS:
        f, z = _to_complex(arr, fmt or _detect_fmt(header) or "reim")
        return np.c_[f, z.real, z.imag]
    if kind == "noise":
        f, Sv = arr[:, 0], arr[:, 1]
        if sv_is_psd2:
            Sv = np.sqrt(np.abs(Sv))
        return np.c_[f, Sv]
    if kind == "spurs":
        return arr[:, :3]                  # f, amp, phase_rad
    # trans_*, dc_*, spur_500u : first two columns as-is
    return arr[:, :2]


def from_psf(path, kind, fmt=None, sv_is_psd2=False):
    """Best-effort Cadence PSF-ASCII reader. Pulls the VALUE section's numeric tuples and
    routes through the same interpreters as from_csv. Binary PSF is NOT supported here
    (export 'PSF ASCII' from ADE, or CSV). Raises if no VALUE block is found."""
    txt = pathlib.Path(path).read_text(errors="ignore")
    if "VALUE" not in txt:
        raise ValueError(f"{path}: not PSF-ASCII (no VALUE section). Export ASCII or use CSV.")
    body = txt.split("VALUE", 1)[1].split("END", 1)[0]
    nums = []
    for line in body.splitlines():
        toks = [t.strip('"') for t in line.replace(",", " ").split()]
        vals = []
        for t in toks:
            try:
                vals.append(float(t))
            except ValueError:
                pass
        if vals:
            nums.append(vals)
    if not nums:
        raise ValueError(f"{path}: PSF VALUE section parsed no numbers")
    w = max(len(r) for r in nums)
    arr = np.array([r + [np.nan] * (w - len(r)) for r in nums], dtype=float)
    # reuse the CSV interpreters on the parsed array
    if kind in CPLX_KINDS:
        f, z = _to_complex(arr, fmt or "reim")
        return np.c_[f, z.real, z.imag]
    if kind == "noise":
        Sv = np.sqrt(np.abs(arr[:, 1])) if sv_is_psd2 else arr[:, 1]
        return np.c_[arr[:, 0], Sv]
    if kind == "spurs":
        return arr[:, :3]
    return arr[:, :2]


def _read_any(path, kind, fmt=None, sv_is_psd2=False):
    p = str(path).lower()
    reader = from_psf if p.endswith((".psf", ".psfascii")) else from_csv
    return reader(path, kind, fmt=fmt, sv_is_psd2=sv_is_psd2)


def _read_wave(path):
    """Read a raw transient export -> (t, v) float arrays (first two columns). Powers the
    spurs_raw_<corner> auto-FFT path. Tolerates header/comment/unit rows via _read_table."""
    _, arr = _read_table(path)
    return arr[:, 0], arr[:, 1]


# --------------------------------------------------------------------------- assemble
def assemble(profile, files, outpath=None, fmt=None, sv_is_psd2=False):
    """Combine per-(quantity,corner) export files into one results/ref/<name>.npz that
    matches CADENCE_EXTRACTION.md. `profile` = dict(name, loads[3], nominal, cout, esr,
    [vref, spur_twin0, spur_binhz]). `files` maps (quantity, corner|None) -> filepath; the
    hf arrays are stored under the harness's nominal-corner names (z_<nom>_hf, p_<nom>_hf).
    Returns the written path. Missing optional quantities are simply omitted."""
    name = profile["name"]
    loads = [str(x) for x in profile["loads"]]
    nom = str(profile.get("nominal") or loads[len(loads) // 2])
    ref = {"loads": np.array(loads),
           "meta_cout": np.array(float(profile.get("cout", np.nan))),
           "meta_esr": np.array(float(profile.get("esr", np.nan)))}

    def rd(kind, corner=None, store_kind=None):
        key = (kind, corner)
        if key not in files:
            return False
        ref_kind = store_kind or kind
        ref_key = f"{ref_kind}_{corner}" if corner is not None else ref_kind
        ref[ref_key] = _read_any(files[key], kind, fmt=fmt, sv_is_psd2=sv_is_psd2)
        return True

    for il in loads:
        rd("z", il); rd("p", il); rd("noise", il); rd("trans_lin", il)
        rd("ibp", il, store_kind="ibp_xfer"); rd("spurs", il)
    # hf extensions: stored under the nominal-corner names the harness reads
    if ("z_hf", nom) in files:
        ref[f"z_{nom}_hf"] = _read_any(files[("z_hf", nom)], "z_hf", fmt=fmt)
    if ("p_hf", nom) in files:
        ref[f"p_{nom}_hf"] = _read_any(files[("p_hf", nom)], "p_hf", fmt=fmt)
    # nominal-only transients
    if ("trans_big", nom) in files:
        ref[f"trans_big_{nom}"] = _read_any(files[("trans_big", nom)], "trans_big")
    if ("trans_slew", nom) in files:
        ref[f"trans_slew_{nom}"] = _read_any(files[("trans_slew", nom)], "trans_slew")
    # globals
    for g in GLOBAL:
        rd(g, None)
    if ("spur_500u", None) in files:
        ref["spur_500u"] = _read_any(files[("spur_500u", None)], "spur_500u")

    # --- intrinsic spurs: prefer pre-made (f,amp,phase) tables; else AUTO-FFT raw v(vout)
    #     waveforms (spurs_raw_<corner>) so the Cadence side exports a plain transient and the
    #     harness does the FFT / peak-pick / fundamental-classification itself. ---
    have_tables = any(f"spurs_{il}" in ref for il in loads)
    raw = {il: files[("spurs_raw", il)] for il in loads if ("spurs_raw", il) in files}
    if raw and not have_tables:
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "harness"))
        import spur_char
        waves = {il: _read_wave(p) for il, p in raw.items()}
        sc = spur_char.characterize_corners_from_waves(
            waves, loads, nominal=nom,
            fmax=float(profile.get("spur_fmax", 30e6)),
            twin=profile.get("spur_twin"))     # (t0,t1)/(t0,None); None -> auto (skip first 20%)
        for il in loads:
            if il in sc["per"]:
                ref[f"spurs_{il}"] = sc["per"][il]
        ref["spur_F"] = np.array(sc["F"])
        ref["spur_twin0"] = np.array(float(sc["twin0"]))
        ref["spur_binhz"] = np.array(float(sc["binhz"]))
    else:
        # pre-made-table path: take the NOMINAL corner's tone freqs (fit_model.fit_spurs reads the
        # nominal corner's spur table for phase and indexes spur_F against it -> they must align).
        # Fall back to the first non-empty corner only if the nominal corner has no spur table.
        spur_f = []
        if f"spurs_{nom}" in ref and ref[f"spurs_{nom}"].size:
            spur_f = [float(x) for x in ref[f"spurs_{nom}"][:, 0]]
        else:
            for il in loads:
                k = f"spurs_{il}"
                if k in ref and ref[k].size:
                    spur_f = [float(x) for x in ref[k][:, 0]]
                    break
        ref["spur_F"] = np.array(spur_f)
        ref["spur_twin0"] = np.array(float(profile.get("spur_twin0", 0.0)))
        ref["spur_binhz"] = np.array(float(profile.get("spur_binhz", 15625.0)))

    REFDIR.mkdir(parents=True, exist_ok=True)
    out = pathlib.Path(outpath) if outpath else (REFDIR / f"{name}.npz")
    np.savez(out, **ref)
    return out


def load_npz(path):
    """Load an assembled npz into a plain dict (for validate / inspection)."""
    d = np.load(path, allow_pickle=True)
    return {k: d[k] for k in d.files}


# --------------------------------------------------------------------------- guardrails
def validate(ref):
    """Detect the 'silent mismatch' export errors from CADENCE_EXTRACTION.md. Returns a
    list of dict(level, quantity, msg) -- the GUI Import tab surfaces these as warnings.
    Pure-numeric checks; no simulator. `ref` = dict from load_npz()."""
    warns = []
    loads = [str(x) for x in ref.get("loads", [])]
    nom = loads[len(loads) // 2] if loads else None

    def add(level, q, msg):
        warns.append(dict(level=level, quantity=q, msg=msg))

    for il in loads:
        # --- PSRR stored as attenuation-dB instead of complex transfer H ---
        pk = f"p_{il}"
        if pk in ref:
            H = ref[pk][:, 1] + 1j * ref[pk][:, 2]
            mag = np.abs(H)
            if np.median(mag) > 5.0 or np.all(ref[pk][:, 2] == 0):
                add("warn", pk, f"PSRR |H| median={np.median(mag):.2g} (>1 or imag all-zero): "
                    "looks like attenuation-in-dB or magnitude-only, not the complex transfer "
                    "H=vout/vin. The harness takes -20log10|H| itself; re-export complex.")
            if np.median(20 * np.log10(mag + 1e-30)) > -3 and np.median(mag) <= 5.0:
                add("info", pk, "PSRR is near 0 dB across band -- confirm this is vout/vin "
                    "(a good LDO rejects supply, so |H| should be well below 1).")
        # --- noise given as V^2/Hz instead of V/rtHz (PSD vs amplitude PSD). Two-tier so a
        #     NOISY part (Sv up to ~uV/rtHz, whose V^2/Hz is ~1e-12) is still flagged: a fixed
        #     1e-12 floor alone misses it. <1e-12 = almost certainly V^2/Hz (warn); <1e-9 =
        #     suspiciously low, could be V^2/Hz for a low-noise part (info, no false-hard-fail). ---
        nk = f"noise_{il}"
        if nk in ref:
            med = float(np.nanmedian(ref[nk][:, 1]))
            if med < 1e-12:
                add("warn", nk, f"noise median={med:.2g}: implausibly small for V/rtHz -- almost "
                    "certainly V^2/Hz. Enable 'sqrt (V^2/Hz -> V/rtHz)' on import.")
            elif med < 1e-9:
                add("info", nk, f"noise median={med:.2g}: low for V/rtHz; if this is a noisy part "
                    "it may be V^2/Hz -- confirm units (use the sqrt option if so).")
        # --- Zout sign/direction: nominal corner should show an output resonance bump ---
        zk = f"z_{il}"
        if zk in ref:
            Z = np.abs(ref[zk][:, 1] + 1j * ref[zk][:, 2])
            if np.any(Z < 0) or np.all(ref[zk][:, 1] < 0):
                add("warn", zk, "Zout has negative real part across band -- check the AC "
                    "injection sign (1 A INTO vout, Z=V/I) and direction.")
    # nominal Zout should peak (resonance) above its LF floor
    if nom and f"z_{nom}" in ref:
        Z = np.abs(ref[f"z_{nom}"][:, 1] + 1j * ref[f"z_{nom}"][:, 2])
        if Z.size and Z.max() < 1.05 * Z[0]:
            add("info", f"z_{nom}", "nominal Zout shows no resonance peak above the LF floor "
                "-- fine for an over-damped LDO, but confirm the sweep covers the output pole.")
    # hf arrays present? (needed for Cout/ESR auto-extraction + RF carrier bound)
    if nom and f"z_{nom}_hf" not in ref:
        add("info", "z_hf", f"no z_{nom}_hf (HF Zout) -- Cout/ESR auto-extract falls back to "
            f"z_{nom}; provide the 500 MHz sweep for a robust cap/ESR extraction.")
    # intrinsic spur summary (esp. useful when auto-FFT'd from spurs_raw waveforms)
    sf = ref.get("spur_F")
    if sf is not None and np.size(sf):
        lines = ", ".join(f"{x/1e6:.4f}MHz" for x in np.ravel(sf)[:6])
        more = " ..." if np.size(sf) > 6 else ""
        binhz = float(ref["spur_binhz"]) if "spur_binhz" in ref else float("nan")
        add("info", "spur_F", f"{np.size(sf)} intrinsic spur fundamental(s): {lines}{more} "
            f"(bin={binhz/1e3:.2f}kHz, twin0={float(ref.get('spur_twin0', 0))*1e6:.2f}us). "
            "Confirm these are independent sources (clock/charge-pump), not harmonic/IM products.")
    return warns


def match_dir(directory, loads, nominal=None):
    """Scan a folder for files matching the contract naming and return a files dict for
    assemble(). Powers both the CLI `--dir` and the GUI 'Import from folder' button, so the
    engineer drops all exports in one folder instead of picking ~30 files by hand.
      per-corner:  <q>_<corner>.<ext>  for q in z/p/noise/trans_lin/spurs/ibp
      nominal:     z_hf|z_<nom>_hf|zhf ; p_hf|p_<nom>_hf|phf ; trans_big[_<nom>] ; trans_slew[_<nom>]
      global:      dc_loadreg ; dc_linereg ; dc_dropout ; spur_500u
    ext in .csv/.txt/.psf/.psfascii."""
    d = pathlib.Path(directory)
    nom = str(nominal) if nominal else loads[len(loads) // 2]
    exts = (".csv", ".txt", ".psf", ".psfascii")

    def find(*stems):
        for s in stems:
            for e in exts:
                p = d / f"{s}{e}"
                if p.exists():
                    return p
        return None

    files = {}
    for il in loads:
        for q in ("z", "p", "noise", "trans_lin", "spurs", "spurs_raw", "ibp"):
            p = find(f"{q}_{il}")
            if p:
                files[(q, il)] = p
    for q, aliases in (("z_hf", (f"z_hf", f"z_{nom}_hf", "zhf")),
                       ("p_hf", (f"p_hf", f"p_{nom}_hf", "phf"))):
        p = find(*aliases)
        if p:
            files[(q, nom)] = p
    for q in ("trans_big", "trans_slew"):
        p = find(f"{q}_{nom}", q)
        if p:
            files[(q, nom)] = p
    for g in ("dc_loadreg", "dc_linereg", "dc_dropout", "spur_500u"):
        p = find(g)
        if p:
            files[(g, None)] = p
    return files


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Assemble Cadence exports -> results/ref/<name>.npz")
    ap.add_argument("--name", required=True)
    ap.add_argument("--loads", default="20u,121u,250u", help="comma-separated corner keys")
    ap.add_argument("--nominal", default=None)
    ap.add_argument("--cout", type=float, default=float("nan"))
    ap.add_argument("--esr", type=float, default=float("nan"))
    ap.add_argument("--vref", type=float, default=1.05)
    ap.add_argument("--dir", default=None, help="directory of <quantity>_<corner>.csv files")
    ap.add_argument("--spur_twin0", type=float, default=0.0,
                    help="phase-ref window start [s] for a PRE-MADE spur table (spurs_<corner>)")
    ap.add_argument("--spur_binhz", type=float, default=15625.0,
                    help="FFT bin width [Hz] for a PRE-MADE spur table (spurs_<corner>)")
    ap.add_argument("--spur_fmax", type=float, default=30e6,
                    help="max spur freq to detect [Hz], RAW-waveform path (spurs_raw_<corner>)")
    ap.add_argument("--spur_tstart", type=float, default=None,
                    help="settling skip / FFT window start [s], RAW path (default: skip first 20%%)")
    a = ap.parse_args()
    loads = a.loads.split(",")
    prof = dict(name=a.name, loads=loads, nominal=a.nominal, cout=a.cout, esr=a.esr, vref=a.vref,
                spur_twin0=a.spur_twin0, spur_binhz=a.spur_binhz, spur_fmax=a.spur_fmax)
    if a.spur_tstart is not None:
        prof["spur_twin"] = (a.spur_tstart, None)
    files = match_dir(a.dir, loads, a.nominal) if a.dir else {}
    out = assemble(prof, files)
    print(f"wrote {out}")
    for w in validate(load_npz(out)):
        print(f"  [{w['level']}] {w['quantity']}: {w['msg']}")
