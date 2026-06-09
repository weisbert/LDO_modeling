"""Thin ngspice subprocess driver + wrdata parser.

We talk to ngspice.exe in batch mode (-b). In batch mode the .control
block's print/op output is NOT echoed to stdout, so all data we want is
emitted via `wrdata <file> <exprs...>` and parsed back here.
"""
import os
import subprocess
import pathlib
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
# ngspice executable, resolved in order: $NGSPICE override -> bundled Windows binary
# (tools/ is platform-specific and git-ignored) -> 'ngspice' on PATH (Linux/macOS, e.g.
# `apt install ngspice`). Keeps the harness runnable after a fresh `git pull` on any OS.
_NG_WIN = ROOT / "tools" / "Spice64" / "bin" / "ngspice.exe"
NGSPICE = os.environ.get("NGSPICE") or (str(_NG_WIN) if _NG_WIN.exists() else "ngspice")
MODELS = [ROOT / "models" / "nmos_lv.mod", ROOT / "models" / "pmos_lv.mod"]
LDO_LIB = ROOT / "ground_truth" / "ldo_gt.lib"


def _read(p):
    return pathlib.Path(p).read_text()


def assemble(tb_text, libs=None):
    """Inline model cards + subckt libs ahead of the testbench so the deck is
    self-contained and free of .include path headaches. `libs` defaults to the
    ground-truth LDO; pass a candidate model lib to bench it instead."""
    if libs is None:
        libs = [LDO_LIB]
    parts = ["* === auto-assembled deck ==="]
    parts += [_read(m) for m in MODELS]
    parts += [_read(p) for p in libs]
    parts.append(tb_text)
    return "\n".join(parts) + "\n"


def run(deck_text, workdir, outputs=(), timeout=180):
    workdir = pathlib.Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "tb.cir").write_text(deck_text)
    for o in outputs:                      # clear stale outputs
        f = workdir / o
        if f.exists():
            f.unlink()
    r = subprocess.run([NGSPICE, "-b", "tb.cir"], cwd=str(workdir),
                       capture_output=True, text=True, timeout=timeout)
    out = {"_rc": r.returncode, "_stdout": r.stdout, "_stderr": r.stderr}
    for o in outputs:
        f = workdir / o
        out[o] = parse_wrdata(f) if f.exists() else None
    return out


def parse_wrdata(path):
    """Parse a wrdata ASCII file. Returns (names_or_None, ndarray[rows,cols]).
    Robust to an optional header line of vector names (set wr_vecnames)."""
    names, rows = None, []
    for line in pathlib.Path(path).read_text().splitlines():
        toks = line.split()
        if not toks:
            continue
        try:
            rows.append([float(t) for t in toks])
        except ValueError:
            names = toks
    return names, np.array(rows, dtype=float)


def complex_col(arr, ireal=1, iimag=2):
    """From a wr_singlescale array [freq, vr, vi, ...] -> (freq, complex)."""
    f = arr[:, 0]
    z = arr[:, ireal] + 1j * arr[:, iimag]
    return f, z


def amps(key):
    """Load-corner key string ('20u','250u','1m','100n',...) -> amps (float). The CANONICAL
    corner-key->current conversion, tolerant of p/n/u/m/k SI suffixes so corner keys need not
    be microamps (a mA-rated LDO uses '10m' etc.); bare numbers pass through. Replaces the old
    `float(il.replace('u','e-6'))` idiom (which raised on any non-'u' key) everywhere."""
    s = str(key).strip()
    suf = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3}
    return float(s[:-1]) * suf[s[-1]] if s and s[-1] in suf else float(s)
