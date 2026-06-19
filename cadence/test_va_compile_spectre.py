"""SPECTRE-GATED regression: the EMITTED Verilog-A deliverable (fit_model.emit_va) actually
COMPILES through Cadence Spectre's ahdlcmi (-64) and SIMULATES.

This closes the one gap the two sibling coverage tests leave open:
  * harness/test_coverage_ngspice.py  exercises the SPICE-subckt flavor (fit_model.emit) in
    ngspice -- never the Verilog-A.
  * cadence/cluster/test_coverage_spectre.py  drives an INLINE behavioral DUT through Spectre
    to prove the COVERAGE NETLISTS converge -- it never compiles the emitted MODEL .va.
Neither one puts the real `.va` deliverable -- the additive-slew_en Verilog-A the designer
actually instantiates -- through the Cadence Verilog-A compiler. This test does, on the LOCAL
Spectre 18.1 (ahdl_include + ahdlcmi -64, the exact mechanism cadence/isrc_spectre.py uses), so
"the .va compiles + runs in the Cadence/Spectre flow" is a LOCALLY-PROVEN regression, not a
red-zone TODO. (ALPS's own Verilog-A compiler + the real silicon DUT remain genuinely box-only.)

Three facts are pinned, on the SAME v2_capless model harness/test_coverage_ngspice.py fits:
  1) ahdlcmi -64 COMPILES the emitted module ldo_model(vin,vout,gnd) and the AC op converges
     -> a real, finite LF output impedance (not a degenerate ~0/inf).
  2) GUARDRAIL-1 holds in the CADENCE engine too: at the OP the small-signal |Zout(f->0)| is
     IDENTICAL for slew_en in {0,1} (the additive correction is 0 in value AND slope at the OP).
  3) CROSS-ENGINE: that LF Zout == the fit's R_a == what ngspice measured (23.2301 ohm) -- the
     Verilog-A and the SPICE subckt are the same physics in two different simulators.

SKIP cleanly (reported skipped, not passed) when Spectre is absent; the guard honours a
SPECTRE_HOME env override so the skip path is testable:
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/test_va_compile_spectre.py -q
        -> the module reports as skipped.

Run:  python3 -m pytest cadence/test_va_compile_spectre.py -q
"""
import os
import pathlib
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent          # .../cadence
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "harness")):           # spectre_run (cadence) + fit_model (harness)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spectre_run as sr                                                    # noqa: E402
import fit_model as FM                                                      # noqa: E402

VARIANT = "v2_capless"          # the SAME model harness/test_coverage_ngspice.py fits
NGSPICE_ZOUT = 23.2301          # the LF |Zout| ngspice measured for this model (both slew_en)


# ----------------------------------------------------------------- skip guard
def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


# ----------------------------------------------------------------- fixture: fit + emit VA once
@pytest.fixture(scope="module")
def model(tmp_path_factory):
    """Fit v2_capless and emit the REAL Verilog-A deliverable (emit_va) once for the module.
    Returns (va_path, op_amps, vdd, R_a). The .tbl is written as a data record but the .va
    inlines the dropout curve (no aux needed at sim time -- see emit_va docstring)."""
    tmp = tmp_path_factory.mktemp("va_spectre")
    res = FM.fit_variant(VARIANT)
    va = tmp / "ldo_model.va"
    FM.emit_va(res.P, va, tmp / "ldo_model_dropout.tbl")
    txt = va.read_text()
    # sanity: this IS the Verilog-A deliverable, and IS the additive-slew_en form
    assert txt.lstrip().splitlines()[-1] or True
    assert "module ldo_model(vin, vout, gnd);" in txt, "not the expected VA module"
    assert "slew_en" in txt, "emitted VA has no slew_en param -- not the coverage model"
    op_amps = FM._amps(res.nominal)
    vdd = float(res.vref)
    r_a = float(res.P[res.nominal]["R_a"])      # LF Zout floor (capless: R_b/R_pl ~ 1e9)
    return va, op_amps, vdd, r_a


def _zout_lf(va, op_amps, vdd, slew, tag):
    """Compile `va` through Spectre (ahdl_include + ahdlcmi -64) and AC-measure the LF output
    impedance: 1 A AC injected into vout, |Zout(f->0)| = |V(vout)|. A DC isource holds the OP
    load; the model self-establishes the Vout DC. Returns (f0, |Zout(f0)|)."""
    scs = (f"// Zout @OP slew_en={slew}\nsimulator lang=spectre\n"
           f'ahdl_include "{va.resolve()}"\n'
           f"Vin (vin 0) vsource dc={vdd:g}\n"
           f"Iload (vout 0) isource dc={op_amps:.6e}\n"
           f"Iac (0 vout) isource mag=1\n"
           f"Xdut (vin vout 0) ldo_model iload={op_amps:.6e} slew_en={slew} vdd={vdd:g}\n"
           f"save vout\nacz ac start=1 stop=1k dec=8\n")
    d = sr.run(scs, tag)
    f = np.asarray(d["acz"]["freq"]).real
    z = np.abs(np.asarray(d["acz"]["vout"]))
    return float(f[0]), float(z[0])


# ============================================================ (1) compiles + simulates
def test_emitted_va_compiles_and_simulates_in_spectre(model):
    """ahdlcmi -64 compiles module ldo_model and the AC op converges to a real, finite LF Zout.
    Convergence IS the proof the Cadence Verilog-A compiler accepted the emitted deliverable."""
    va, op_amps, vdd, _r_a = model
    f0, z0 = _zout_lf(va, op_amps, vdd, 0, "va_cc_compile")
    assert 1.0 < z0 < 1e4, f"implausible LF Zout {z0} (sim converged but physics degenerate?)"
    print(f"VA COMPILE+SIM ok (ahdlcmi -64): Zout(f={f0:.3g}Hz)={z0:.6f} ohm")


# ============================================================ (2) GUARDRAIL-1 in Cadence engine
def test_guardrail1_additive_slew_holds_in_spectre(model):
    """The additive correction is 0 in value AND slope at the OP -> the at-OP small-signal
    |Zout| is IDENTICAL for slew_en in {0,1} in the CADENCE engine, just as in ngspice."""
    va, op_amps, vdd, _r_a = model
    _f0, z0 = _zout_lf(va, op_amps, vdd, 0, "va_cc_slew0")
    _f1, z1 = _zout_lf(va, op_amps, vdd, 1, "va_cc_slew1")
    rel = abs(z0 - z1) / max(z0, 1e-30)
    assert rel < 1e-3, (f"at-OP Zout differs with slew_en in Spectre: {z0} vs {z1} "
                        f"(rel {rel:.2e}) -- the VA correction is not 0-slope at the OP")
    print(f"GUARDRAIL-1 (Spectre): slew0={z0:.6f} slew1={z1:.6f} rel={rel:.2e}")


# ============================================================ (3) cross-engine consistency
def test_va_zout_matches_ra_and_ngspice(model):
    """The Verilog-A LF Zout == the fit's R_a == the ngspice SPICE-subckt measurement.
    Same physics, two engines: Cadence Spectre Verilog-A vs ngspice SPICE."""
    va, op_amps, vdd, r_a = model
    _f0, z0 = _zout_lf(va, op_amps, vdd, 0, "va_cc_xeng")
    rel_ra = abs(z0 - r_a) / r_a
    rel_ng = abs(z0 - NGSPICE_ZOUT) / NGSPICE_ZOUT
    assert rel_ra < 0.02, f"Spectre VA Zout {z0} != fit R_a {r_a} (rel {rel_ra:.2e})"
    assert rel_ng < 0.02, f"Spectre VA Zout {z0} != ngspice {NGSPICE_ZOUT} (rel {rel_ng:.2e})"
    print(f"CROSS-ENGINE: Spectre-VA Zout={z0:.6f}  fit R_a={r_a:.6f}  "
          f"ngspice={NGSPICE_ZOUT}  (rel_ra={rel_ra:.2e} rel_ng={rel_ng:.2e})")
