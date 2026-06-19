"""CROSS-ENGINE NOISE regression (needs BOTH Spectre 18.1 and ngspice): the emitted model's
OUTPUT-NOISE spectrum is identical in the Cadence Verilog-A (Spectre `noise`) and the SPICE
subckt (ngspice `noise`).

Noise is a headline LDO/PMU spec, and the model carries a decoupled Norton noise core (white +
R||C-Lorentzian sections transconducted into vout). harness/test_coverage_ngspice.py exercises the
small-signal/dropout paths but NOT noise; cadence/test_va_compile_spectre.py compiles the VA but
checks impedance/dropout, not noise. This test pins the noise core ACROSS the two engines:

  * fit v2_capless once -> emit BOTH flavors (emit_va -> .va for Spectre, emit -> .lib for ngspice).
  * Spectre `noise` on the Verilog-A vs ngspice `noise` on the SPICE subckt, same OP.
  * the output-noise spectral densities (V/rtHz) must agree over 10 Hz .. 1 MHz to a tight
    relative tolerance -- the Verilog-A noise block reproduces the validated SPICE noise.

Measured locally: worst rel ~9e-6 over 10 Hz..1 MHz (the two engines compute the SAME spectrum).

SKIP cleanly when EITHER engine is absent; both guards honour env overrides so the skip path is
testable (SPECTRE_HOME=/nonexistent or NGSPICE=/nonexistent -> the module reports as skipped):
    NGSPICE=/nonexistent python3 -m pytest cadence/test_va_noise_xengine.py -q   -> skipped
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/test_va_noise_xengine.py -q -> skipped

Run:  python3 -m pytest cadence/test_va_noise_xengine.py -q
"""
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent          # .../cadence
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "harness")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spectre_run as sr                                                    # noqa: E402
import fit_model as FM                                                      # noqa: E402
import ng                                                                  # noqa: E402

VARIANT = "v2_capless"
# the band over which the two engines must agree tightly (avoid the very top decade, where the
# noise-corner roll-off meets each solver's numerical floor and they diverge to ~1e-2).
BAND = (10.0, 1e6)
XTOL = 2e-3


# ----------------------------------------------------------------- skip guard (BOTH engines)
def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


def _have_ngspice():
    try:
        return subprocess.run([ng.NGSPICE, "--version"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (_have_spectre() and _have_ngspice()), reason="needs both spectre and ngspice")


# ----------------------------------------------------------------- fixture: fit + emit both once
@pytest.fixture(scope="module")
def model(tmp_path_factory):
    """Fit v2_capless once; emit the Verilog-A (.va, for Spectre) AND the SPICE subckt (.lib, for
    ngspice) from the SAME FitResult. Returns (va, lib, op_amps, vdd)."""
    tmp = tmp_path_factory.mktemp("va_noise")
    res = FM.fit_variant(VARIANT)
    va = tmp / "ldo_model.va"
    lib = tmp / "ldo_model.lib"
    FM.emit_va(res.P, va, tmp / "ldo_model_dropout.tbl")
    FM.emit(res.P, lib)
    return va, lib, FM._amps(res.nominal), float(res.vref)


def _spectre_onoise(va, op_amps, vdd):
    """Spectre `noise` on the Verilog-A -> (freq, output-noise V/rtHz)."""
    scs = (f"// VA output noise\nsimulator lang=spectre\n"
           f'ahdl_include "{va.resolve()}"\n'
           f"Vin (vin 0) vsource dc={vdd:g}\n"
           f"Ild (vout 0) isource dc={op_amps:.6e}\n"
           f"Xdut (vin vout 0) ldo_model iload={op_amps:.6e} slew_en=0 vdd={vdd:g}\n"
           f"save vout\nnz (vout 0) noise start=10 stop=10M dec=10\n")
    d = sr.run(scs, "va_noise_xeng")
    return np.asarray(d["nz"]["freq"]).real, np.asarray(d["nz"]["out"]).real


def _ngspice_onoise(lib, tmp, op_amps, vdd):
    """ngspice `noise` on the SPICE subckt -> (freq, output-noise V/rtHz). The onoise_spectrum
    vector lives in the noise1 plot (setplot before wrdata)."""
    deck = f"""* cross-engine output noise
Xdut vin vout 0 ldo_model iload={op_amps:.6e} slew_en=0 vdd={vdd:g}
Vin vin 0 DC {vdd:g} AC 1
Ild vout 0 DC {op_amps:.6e}
.control
set wr_singlescale
noise v(vout) Vin dec 10 10 10meg 1
setplot noise1
wrdata ng_noise.dat onoise_spectrum
quit
.endc
.end
"""
    r = ng.run(ng.assemble(deck, libs=[str(lib)]), tmp / "ngnz", outputs=["ng_noise.dat"])
    assert r["ng_noise.dat"] is not None, f"ngspice noise failed (rc={r['_rc']}):\n{r['_stderr'][-1500:]}"
    a = np.atleast_2d(r["ng_noise.dat"][1])
    return a[:, 0], a[:, -1]


def test_va_output_noise_matches_ngspice(model):
    """The Verilog-A output-noise spectrum (Spectre) == the SPICE-subckt one (ngspice) across
    10 Hz..1 MHz. Same noise core, two engines."""
    va, lib, op_amps, vdd = model
    tmp = lib.parent
    fs, sp = _spectre_onoise(va, op_amps, vdd)
    fn, npg = _ngspice_onoise(lib, tmp, op_amps, vdd)

    lo, hi = BAND
    worst, wf = 0.0, None
    n_pts = 0
    for ft in fs:
        if not (lo <= ft <= hi):
            continue
        s = float(sp[int(np.argmin(np.abs(fs - ft)))])
        nval = float(npg[int(np.argmin(np.abs(fn - ft)))])
        rel = abs(s - nval) / max(s, 1e-30)
        n_pts += 1
        if rel > worst:
            worst, wf = rel, ft
    assert n_pts >= 10, f"too few comparison points in band ({n_pts})"
    assert worst < XTOL, f"cross-engine output noise mismatch: worst rel {worst:.2e} @ {wf:.3g} Hz"
    # sanity: a real LDO-like spectrum -- microvolt-class 1/f at low f, falling with frequency
    lf = float(sp[int(np.argmin(np.abs(fs - lo)))])
    hf = float(sp[int(np.argmin(np.abs(fs - hi)))])
    assert 1e-7 < lf < 1e-4, f"implausible LF output noise {lf} V/rtHz"
    assert hf < lf, "output noise should fall from LF to HF (1/f + roll-off)"
    print(f"CROSS-ENGINE noise: worst rel {worst:.2e} @ {wf:.3g} Hz over {n_pts} pts "
          f"({lo:.0f}-{hi:.0g} Hz); LF={lf:.3e} HF={hf:.3e} V/rtHz")
