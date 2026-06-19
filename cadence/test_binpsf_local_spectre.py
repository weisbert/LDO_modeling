"""SPECTRE-GATED regression: the BINARY-PSF reader (cadence/binpsf.py) parses a binary PSF
PRODUCED LOCALLY by Spectre 18.1 (`-format psfbin`), and agrees with the trusted psfascii
reader to within psfascii's precision floor.

Why this exists: cadence/test_binpsf.py validates binpsf against ONE committed fixture captured
from the box (a Maestro/ADE binary AC + a noise PSF). That proves the reader handles THAT
producer. This test adds a SECOND, independent producer -- the local Spectre 18.1 with
`-format psfbin` -- and round-trips it against `-format psfascii` of the SAME deck. It shows the
binary grammar (sections, headers, f64 traces, complex AC, the noise output trace) is read
correctly across producers, not memorized from a single capture. ("the .va compiling" was wrongly
shelved as box-only before; this is the binary-PSF analogue -- local Spectre exercises it too.)

Scope honesty: local `spectre -format psfbin` and the cluster's Maestro/ALPS writer are different
PSF producers; this covers the COMMON binary grammar (the AC sweep + the noise output PSD). The
per-contributor STRUCT noise traces and any ALPS-specific quirk stay covered by the committed box
fixture in test_binpsf.py -- the two tests are complementary, neither replaces the other.

SKIP cleanly (reported skipped, not passed) when Spectre is absent; the guard honours a
SPECTRE_HOME env override so the skip path is testable:
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/test_binpsf_local_spectre.py -q
        -> the module reports as skipped.

Run:  python3 -m pytest cadence/test_binpsf_local_spectre.py -q
"""
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent          # .../cadence
sys.path.insert(0, str(HERE))

import spectre_run as sr                                                    # noqa: E402
import binpsf                                                              # noqa: E402
import psf                                                                 # noqa: E402

# psfascii prints ~6 sig figs (a ~1e-5 relative floor); the binary reader is full double
# precision, so the cross-check floor is psfascii's, not the binary reader's.
ASCII_PRECISION = 3e-5


# ----------------------------------------------------------------- skip guard
def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


# ----------------------------------------------------------------- local runner (both formats)
def _run(scs, fmt, wd):
    """Run `spectre -64 -format <fmt>` on `scs` in `wd`; return {analysis_basename: path}.
    Reuses spectre_run's env (SPECTRE181 on PATH, node-locked license) -- the only difference
    from sr.run is the -format knob (psfbin vs psfascii) we are deliberately exercising."""
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "input.scs").write_text(scs)
    raw = wd / "raw"
    r = subprocess.run(
        ["spectre", "-64", "input.scs", "-format", fmt, "-raw", "raw",
         "+log", "spectre.log", "-E"],
        cwd=str(wd), env=sr._env(), capture_output=True, text=True, timeout=240)
    log = (wd / "spectre.log").read_text() if (wd / "spectre.log").exists() else r.stdout
    if "fatal error" in log or r.returncode != 0:
        raise RuntimeError(f"spectre -format {fmt} failed (rc={r.returncode}):\n{log[-2000:]}")
    return {p.name.split(".")[0]: p for p in sorted(raw.iterdir())
            if p.is_file() and p.name != "logFile"}


def _worst_rel(a, b):
    """max|a-b|/max|a| over signals common to both dicts (magnitude-aware relative error)."""
    common = sorted(set(k for k in a if k != "_sweep") & set(k for k in b if k != "_sweep"))
    assert common, "no common signals between binary and ascii reads"
    worst, wkey = 0.0, None
    for k in common:
        va = np.asarray(a[k]).astype(complex)
        vb = np.asarray(b[k]).astype(complex)
        assert va.shape == vb.shape, f"shape mismatch {k}: {va.shape} vs {vb.shape}"
        scale = max(float(np.max(np.abs(vb))), 1e-30)
        err = float(np.max(np.abs(va - vb))) / scale
        if err > worst:
            worst, wkey = err, k
    return worst, wkey, len(common)


# --------------------------------------------------------------------- decks
_AC_DECK = """// binpsf local AC
simulator lang=spectre
Vac (n1 0) vsource dc=0 mag=1
R1  (n1 n2) resistor r=1k
C1  (n2 0)  capacitor c=1n
save n2 Vac:p
acx ac start=10 stop=500M dec=10
"""

_NOISE_DECK = """// binpsf local noise
simulator lang=spectre
Vac (n1 0) vsource dc=0
R1 (n1 0) resistor r=10k
save n1
nz (n1 0) noise start=10 stop=1M dec=10
"""


# ============================================================ AC binary PSF
def test_local_psfbin_ac_reads_and_matches_ascii(tmp_path):
    """Local `spectre -format psfbin` AC -> binpsf._is_binary True, read_binpsf parses a freq
    sweep, and the binary read matches `-format psfascii` to psfascii's precision."""
    files_b = _run(_AC_DECK, "psfbin", tmp_path / "ac_bin")
    files_a = _run(_AC_DECK, "psfascii", tmp_path / "ac_asc")
    fb, fa = files_b["acx"], files_a["acx"]

    assert binpsf._is_binary(fb), f"local psfbin AC not sniffed as binary: head={fb.read_bytes()[:8]!r}"
    assert not binpsf._is_binary(fa), "psfascii AC wrongly sniffed as binary"

    db = binpsf.read_binpsf(fb)
    assert db["_sweep"] == "freq", db["_sweep"]
    assert np.asarray(db["freq"]).size > 1, "no freq points parsed"
    assert np.iscomplexobj(np.asarray(db["n2"]).astype(complex)), "AC trace should be complex"

    da = psf.read_psf(fa)                       # auto-dispatch -> ascii grammar
    worst, wkey, n = _worst_rel(db, da)
    assert worst < ASCII_PRECISION, f"binary vs ascii AC worst rel {worst:.2e} on {wkey} (n={n})"
    print(f"AC psfbin: {np.asarray(db['freq']).size} pts, {n} signals, worst rel {worst:.2e} ({wkey})")


# ============================================================ noise binary PSF
def test_local_psfbin_noise_reads_and_matches_ascii(tmp_path):
    """Local `spectre -format psfbin` NOISE -> binpsf reads the output-noise PSD trace and it
    matches the psfascii read. (Per-contributor struct traces stay covered by test_binpsf.py's
    box fixture; this pins the common noise-output binary grammar on a local producer.)"""
    files_b = _run(_NOISE_DECK, "psfbin", tmp_path / "nz_bin")
    files_a = _run(_NOISE_DECK, "psfascii", tmp_path / "nz_asc")
    fb, fa = files_b["nz"], files_a["nz"]

    assert binpsf._is_binary(fb), "local psfbin noise not sniffed as binary"
    db = binpsf.read_binpsf(fb)
    assert db["_sweep"] == "freq", db["_sweep"]
    assert "out" in db, f"no 'out' noise trace in binary read; keys={[k for k in db if k!='_sweep']}"
    out = np.asarray(db["out"]).real
    assert out.size > 1 and np.all(out >= 0), "output-noise PSD should be a real non-negative spectrum"

    da = psf.read_psf(fa)
    worst, wkey, n = _worst_rel(db, da)
    assert worst < ASCII_PRECISION, f"binary vs ascii noise worst rel {worst:.2e} on {wkey} (n={n})"
    print(f"noise psfbin: {out.size} pts, {n} signals, worst rel {worst:.2e} ({wkey})")
