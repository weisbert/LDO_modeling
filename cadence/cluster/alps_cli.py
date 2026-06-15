"""Assemble the per-corner engine invocation (the command Donau wraps).

Two engines, mirroring ADE's "Use ALPS" checkbox (ALPS_DONAU_NOTES §0, §8). Both read the
same Spectre-syntax netlist (`input.scs`, POSITIONAL first arg) and both must emit CLASSIC
PSF (the layout binpsf.py reads -- NEVER psfxl).

ALPS (Empyrean), validated cmdline (§1c / §7):
    <wrapper>/bin/alps input.scs -format ps -o ../psf \
        -I <model_dir>/alps -ahdllibdir <ahdldir> -mt N -ade
  - WRAPPER, not the raw binary: `which alps` is a bash wrapper that sets LD_LIBRARY_PATH
    then exec's the raw binary; the raw binary fails `libsvadv.so: cannot open shared object
    file` on the compute node (§9). We always invoke `<wrapper>/bin/alps`.
  - `-format ps` is the (hidden) classic-PSF flag; ADE's psfxl is downgraded to ps for ALPS.
  - `-ade` => ADE-style output names (ac.ac / noise.noise) + a 0-byte `.simDone` sentinel
    in the -o dir (the completion marker run_corner polls). Without it native names
    (`*.ac0.ac`) and no sentinel (§9 Stage-1).
  - `-mt N` must equal Donau `cpu=N` (§1c).
  - model `-I` = the engine's PDK tree ONLY (the alps tree), so the bare
    `include "toplevel.scs"` can only resolve to the .alps selector -> unambiguous models;
    anything missing errors loudly (§1d).

Spectre (Cadence +APS) fallback (§8) -- ⚠️ UNVERIFIED: these flags are INFERRED, NOT yet
confirmed against a real company spectre.out (ALPS_DONAU_NOTES §8 is still open). The ALPS
path is byte-validated (§9); the spectre branch must be checked against a captured spectre.out
(esp. `+aps` turbo, `-format psfascii` vs `psfbin`, and `-raw` dir) BEFORE relying on it:
    spectre input.scs -format psfascii -raw ../psf \
        -I <model_dir>/spectre -ahdllibdir <ahdldir> +mt=N +aps
  - output dir flag is `-raw` (not `-o`); format is `psfascii` (or `psfbin`) -- NOT psfxl;
    threads are `+mt=N`; turbo is `+aps`; model tree is the spectre tree.
"""

# The bash WRAPPER (not the raw binary) -- it sets LD_LIBRARY_PATH then exec's the binary.
# Absolute, version-pinned to the build in ALPS_DONAU_NOTES (Empyrean 2026.03.hf1).
ALPS_WRAPPER_DEFAULT = "/software/empyrean/alps/2026.03.hf1"


def _alps_exe(alps_wrapper):
    """The wrapper's launcher: <wrapper>/bin/alps (§9). Accepts either the install root
    (.../2026.03.hf1) or a path already ending in /bin/alps; normalises to .../bin/alps."""
    w = str(alps_wrapper).rstrip("/")
    if w.endswith("/bin/alps"):
        return w
    if w.endswith("/bin"):
        return w + "/alps"
    return w + "/bin/alps"


def build_sim_cmd(engine, input_scs, out_psf, model_dir, ahdllibdir, mt=8,
                  alps_wrapper=ALPS_WRAPPER_DEFAULT, ade=True):
    """Return the engine command as an argv list (the payload Donau's dsub wraps).

    engine        'alps' | 'spectre'
    input_scs     the Spectre-syntax netlist (positional first arg, e.g. 'input.scs')
    out_psf       output dir (alps: -o ; spectre: -raw). Relative is fine -- it is resolved
                  against the node cwd (dsub -EP <netlistdir>), matching ADE's `-o ../psf`.
    model_dir     PDK model ROOT containing the per-engine subtree {alps,spectre}; the
                  engine's own subtree is appended to -I (§1d). If model_dir already ends in
                  the engine name it is used as-is (caller passed the leaf tree directly).
    ahdllibdir    compiled AHDL/VA model DB (-ahdllibdir), e.g. .../input.ahdlSimDB
    mt            sim threads; MUST equal Donau cpu=N (§1c)
    alps_wrapper  ALPS install root (-> <root>/bin/alps); ignored for spectre
    ade           ALPS only: add -ade (ADE-style names + .simDone sentinel). Default True.
    """
    if engine not in ("alps", "spectre"):
        raise ValueError(f"unknown engine {engine!r} (expected 'alps' or 'spectre')")
    model_tree = _engine_model_tree(model_dir, engine)
    if engine == "alps":
        cmd = [
            _alps_exe(alps_wrapper),
            str(input_scs),                 # positional netlist (§1b)
            "-format", "ps",                # classic PSF (hidden flag; NOT psfxl) (§1b)
            "-o", str(out_psf),             # output dir, relative to node cwd (§1c)
            "-I", model_tree,               # alps PDK tree ONLY -> unambiguous models (§1d)
            "-ahdllibdir", str(ahdllibdir),  # compiled AHDL/VA DB (§1c)
            "-mt", str(int(mt)),            # threads == Donau cpu=N (§1c)
        ]
        if ade:
            cmd.append("-ade")             # ADE names ac.ac/noise.noise + .simDone (§9)
        return cmd
    # spectre fallback (§8) -- ⚠️ UNVERIFIED flags; confirm against a real company spectre.out
    # (uncheck "Use ALPS", run, capture) before relying on this branch (esp. +aps / psf format).
    return [
        "spectre",
        str(input_scs),                     # positional netlist
        "-format", "psfascii",              # classic PSF (ascii); NOT psfxl (§8)
        "-raw", str(out_psf),               # spectre output-dir flag is -raw (§8)
        "-I", model_tree,                   # spectre PDK tree (§8)
        "-ahdllibdir", str(ahdllibdir),
        f"+mt={int(mt)}",                   # spectre threads syntax (§8)
        "+aps",                             # APS turbo (§8)
    ]


def _engine_model_tree(model_dir, engine):
    """Append the engine's PDK subtree ({alps,spectre}) to the model ROOT, unless the caller
    already passed the leaf tree (path ends in the engine name)."""
    d = str(model_dir).rstrip("/")
    if d.rsplit("/", 1)[-1] == engine:
        return d
    return f"{d}/{engine}"
