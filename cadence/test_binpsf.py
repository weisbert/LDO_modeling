"""Self-test for cadence/binpsf.py -- the binary-PSF reader for the ade/cluster path.

Self-contained: two committed fixtures (testdata/binpsf/) captured from ONE ade AC run --
the BINARY PSF that Maestro wrote, and the SAME netlist re-emitted with `-format psfascii`.
No spectre / live session needed at test time.

What it proves:
  1. binpsf.read_binpsf parses the binary fixture: freq 10Hz->500MHz, 51 pts, 35 traces,
     complex AC values, bus-pin names un-escaped to match psfascii.
  2. The binary reader agrees with the trusted psfascii reader (cadence/psf.py) to within
     psfascii's OWN precision. psfascii rounds to ~6 significant figures (it literally
     writes `(0.125000 -9.81748e-10)`), so the floor is ~1e-5 relative -- the binary
     reader is the more accurate of the two (full double precision).
  3. psf.read_psf auto-dispatches on the bytes: binary->binpsf, text->ascii grammar.

Run:  python cadence/test_binpsf.py     (exit 0 = pass)
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import binpsf                                            # noqa: E402
import psf                                               # noqa: E402

BIN = HERE / "testdata" / "binpsf" / "ac_ade_binary.ac"
ASC = HERE / "testdata" / "binpsf" / "ac_psfascii.ac"

# psfascii prints ~6 significant figures; relative to a port's own magnitude that is a
# ~1e-5 floor. We assert the binary reader matches the ascii reference within 3e-5.
ASCII_PRECISION = 3e-5


def _worst_rel(a, b):
    """Max over common signals of max|a-b| / max|a| (magnitude-aware relative error)."""
    common = sorted(set(k for k in a if k != "_sweep") & set(k for k in b if k != "_sweep"))
    worst, wkey = 0.0, None
    for k in common:
        va = np.asarray(a[k]).astype(complex)
        vb = np.asarray(b[k]).astype(complex)
        assert va.shape == vb.shape, f"shape mismatch {k}: {va.shape} vs {vb.shape}"
        scale = max(float(np.max(np.abs(va))), 1e-30)
        err = float(np.max(np.abs(va - vb))) / scale
        if err > worst:
            worst, wkey = err, k
    return worst, wkey, len(common)


def main():
    assert BIN.exists() and ASC.exists(), f"fixtures missing under {BIN.parent}"

    # 1. structural invariants on the binary parse
    d = binpsf.read_binpsf(BIN)
    assert d["_sweep"] == "freq", d["_sweep"]
    f = np.asarray(d["freq"]).real
    assert f.size == 51, f"expected 51 sweep points, got {f.size}"
    assert abs(f[0] - 10.0) < 1e-9 and abs(f[-1] - 500e6) < 1.0, (f[0], f[-1])
    traces = [k for k in d if k not in ("_sweep", "freq")]
    assert len(traces) == 34, f"expected 34 traces, got {len(traces)}"
    assert "PLL_CTRL<0>" in d, "bus-pin name not un-escaped"          # not PLL_CTRL\<0\>
    vdd = np.asarray(d["VDD0P8_PLL"])                                  # the injected-node Zout
    assert vdd.dtype == complex and np.count_nonzero(vdd) == 51, "AC node should be complex/nonzero"
    assert abs(vdd[0].real - 0.125) < 1e-3, vdd[0]                     # DC Zout ~ 0.125 ohm

    # 2. cross-validate vs psfascii within psfascii's own precision
    a = psf.read_psf(ASC)
    worst, wkey, ncommon = _worst_rel(a, d)
    assert ncommon == 35, f"expected 35 common signals (incl freq), got {ncommon}"
    assert worst <= ASCII_PRECISION, f"binary vs ascii worst={worst:.2e} @ {wkey} > {ASCII_PRECISION:.0e}"

    # 3. psf.read_psf dispatches by content (binary file -> binpsf path)
    via_dispatch = psf.read_psf(str(BIN))
    w2, k2, _ = _worst_rel(via_dispatch, d)
    assert w2 == 0.0, f"psf.read_psf binary dispatch differs from binpsf ({w2:.2e} @ {k2})"

    print(f"PASS  binpsf: 51 pts x 34 traces, freq 10Hz->500MHz; "
          f"vs psfascii worst={worst:.2e} @ {wkey} (<= {ASCII_PRECISION:.0e}, psfascii 6-sigfig floor); "
          f"psf.read_psf auto-dispatch exact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
