"""SPECTRE-GATED regression: a Spectre `vsource noisefile=` value is POWER spectral
density [V^2/Hz] -- NOT amplitude [V/rtHz].

This pins a tool fact the whole supply-noise methodology depends on: when we hand the
model a supply noise file (or a Verilog-A `noise_table`), the numbers are V^2/Hz, and
the `.noise` `out` trace comes back as amplitude V/rtHz. Get this wrong and every
supply-noise spec is off by a square root.

Mechanism (measured locally, definitive): drive a node DIRECTLY with an ideal vsource
carrying a FLAT noise file of value V; probe that node's output noise. Spectre's noise
`out` trace is amplitude density [V/rtHz], so:
    out == sqrt(V)   <=>  the file value is POWER     [V^2/Hz]   (this is the truth)
    out == V         <=>  the file value is AMPLITUDE [V/rtHz]
A small series resistor (r=1) only provides a DC path; its thermal noise (~1e-10 V/rtHz)
is negligible against the test levels.

SKIP cleanly (reported skipped, not passed) when Spectre is absent; the guard honours a
SPECTRE_HOME env override so the skip path is testable:
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/test_noisefile_units_spectre.py -q -> skipped

Run:  python3 -m pytest cadence/test_noisefile_units_spectre.py -q
"""
import os
import pathlib
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent          # .../cadence
sys.path.insert(0, str(HERE))

import spectre_run as sr                                                    # noqa: E402


def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


def _node_onoise(flat_psd, tmp_path):
    """Drive node n with an ideal vsource carrying a flat noise file = flat_psd; return the
    median output-noise amplitude [V/rtHz] probed at n."""
    nf = tmp_path / "flat_nf.dat"
    nf.write_text(f"10 {flat_psd:e}\n1e9 {flat_psd:e}\n")
    scs = (f"// noisefile unit probe\nsimulator lang=spectre\n"
           f'Vn (n 0) vsource dc=0 noisefile="{nf.resolve()}"\n'
           f"Rp (n 0) resistor r=1\nsave n\n"
           f"nz (n 0) noise start=100 stop=1k dec=1\n")
    saved = sr.WORK
    sr.WORK = tmp_path / "sprun"
    try:
        d = sr.run(scs, "nf_units")
    finally:
        sr.WORK = saved
    return float(np.median(np.asarray(d["nz"]["out"]).real))


@pytest.mark.parametrize("flat_psd", [1e-14, 4e-12])
def test_noisefile_value_is_power_v2_per_hz(flat_psd, tmp_path):
    """out == sqrt(file value): the file is V^2/Hz power, and the .noise out trace is V/rtHz."""
    out = _node_onoise(flat_psd, tmp_path)
    expect_power = np.sqrt(flat_psd)        # if file is V^2/Hz
    expect_amp = flat_psd                    # if file were V/rtHz
    # it must match the POWER interpretation, and clearly NOT the amplitude one
    assert abs(out - expect_power) / expect_power < 0.02, (
        f"noisefile={flat_psd:e}: out={out:e}, sqrt={expect_power:e} "
        f"(rel {abs(out-expect_power)/expect_power:.2e})")
    assert abs(out - expect_amp) / max(expect_amp, 1e-30) > 1.0, (
        f"out unexpectedly close to the raw file value -> would mean V/rtHz, not V^2/Hz")
    print(f"noisefile {flat_psd:e} V^2/Hz -> out {out:e} V/rtHz == sqrt (POWER confirmed)")
