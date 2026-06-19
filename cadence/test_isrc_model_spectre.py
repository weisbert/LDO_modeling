"""SPECTRE-GATED regression: the CURRENT-OUTPUT (bias) behavioral model transfers to Cadence
Spectre -- the emitted Verilog-A current-source model matches its MOS-transistor ground truth
in the SAME engine, self-consistently (char in Spectre -> fit -> emit VA -> validate in Spectre).

cadence/isrc_spectre.py already runs this whole flow across all 8 archetypes as a DRIVER SCRIPT
(`python cadence/isrc_spectre.py`), but it is not a gated regression and it leans on
work_isrc/*.npz from the ngspice characterization. This locks a fast 2-archetype subset as a
self-contained, spectre-only pytest:

  * char_spectre characterizes the MOS GT in Spectre (IV / AC Y / PSRR / Idc(T)); the ONLY thing
    it borrows from the ngspice npz is the current-NOISE arrays, which do NOT enter the
    deterministic pass/fail -- so we stub them and the test needs no ngspice and no committed npz.
  * fit_isrc fits, emit_pmu_va emits the behavioral VA, and iv()/psrr() validate it against the
    same Spectre GT card. The SAME probe (OUTP:p) measures both, so PSRR signs are directly
    comparable with no convention bookkeeping.

Two archetypes pin both polarities + both PSRR signs + the PTAT temperature path:
  * v4_pmos_simple (SOURCE): dId/dVdd > 0 (~+56 nA/V) -- a genuinely non-trivial PSRR, sign matched.
  * v6_ptat        (SINK):   dId/dVdd < 0 (~-145 nA/V) AND the PTAT Idc(125C)/Idc(-40C) ratio
                             tracks the GT (ptat_err < 0.05).
Measured locally: idc_err < 0.4%, iv_rms < 4%, signs matched, ptat_err ~0.001.

SKIP cleanly (reported skipped, not passed) when Spectre is absent; the guard honours a
SPECTRE_HOME env override so the skip path is testable:
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/test_isrc_model_spectre.py -q -> skipped

Run:  python3 -m pytest cadence/test_isrc_model_spectre.py -q
"""
import os
import pathlib
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent          # .../cadence
sys.path.insert(0, str(HERE))

import spectre_run as sr                                                    # noqa: E402
import isrc_spectre as ISP                                                  # noqa: E402


# ----------------------------------------------------------------- skip guard
def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


# (name, do_temp, expected sign of dId/dVdd) -- source>0, sink<0
CASES = [
    ("v4_pmos_simple", False, +1),
    ("v6_ptat", True, -1),
]


@pytest.fixture
def isolated(tmp_path):
    """Redirect every isrc_spectre work dir into tmp_path so the run is hermetic, and supply a
    stub for the noise arrays char_spectre borrows from the (uncommitted) ngspice npz -- noise
    does NOT enter the deterministic pass/fail, so a stub keeps the test spectre-only."""
    saved = (ISP.WORK, ISP.WORK_SP, ISP.VADIR, sr.WORK)
    ISP.WORK = tmp_path                          # where char_spectre reads the borrowed noise
    ISP.WORK_SP = tmp_path                        # where char_spectre writes its Spectre npz
    ISP.VADIR = tmp_path / "va"                    # emitted .va files
    sr.WORK = tmp_path / "sprun"                   # spectre run scratch
    try:
        yield tmp_path
    finally:
        ISP.WORK, ISP.WORK_SP, ISP.VADIR, sr.WORK = saved


@pytest.mark.parametrize("name,do_temp,gsign", CASES, ids=[c[0] for c in CASES])
def test_current_archetype_va_matches_mos_gt_in_spectre(isolated, name, do_temp, gsign):
    """The behavioral current-source VA reproduces its MOS GT in Spectre: DC current, the I-V
    plateau shape, the PSRR sign (and a non-trivial PSRR magnitude), and -- for the PTAT cell --
    the temperature ratio."""
    # stub the borrowed current-noise arrays (not used in the deterministic checks)
    np.savez(isolated / f"{name}.npz",
             nz_f=np.array([10.0, 1e3, 1e6]), nz_in=np.array([1e-12, 1e-12, 1e-12]))

    r = ISP.selfconsistent(name, do_temp=do_temp)

    assert r["idc_err"] < 0.03, f"{name}: DC current error {r['idc_err']*100:.2f}% (>3%)"
    assert r["iv_rms"] < 0.06, f"{name}: I-V plateau RMS {r['iv_rms']*100:.2f}% (>6%)"
    assert r["sign_ok"], f"{name}: PSRR sign mismatch (GT {r['g_glf']:.3e} vs MD {r['m_glf']:.3e})"
    # the PSRR is genuinely non-trivial here (not the abs<1e-12 trivial-pass branch), and both
    # the GT and the model carry the SAME, expected sign
    assert abs(r["m_glf"]) > 1e-11, f"{name}: model PSRR ~0 -> the sign check would be trivial"
    assert np.sign(r["g_glf"]) == gsign, f"{name}: GT dId/dVdd sign != expected {gsign}"
    assert np.sign(r["m_glf"]) == gsign, f"{name}: model dId/dVdd sign != expected {gsign}"
    # and the magnitudes track (the model is fit to the GT PSRR)
    rel_g = abs(r["m_glf"] - r["g_glf"]) / max(abs(r["g_glf"]), 1e-30)
    assert rel_g < 0.1, f"{name}: PSRR magnitude off: GT {r['g_glf']:.3e} vs MD {r['m_glf']:.3e}"

    if do_temp:
        assert r["ptat_err"] < 0.05, f"{name}: PTAT temp-ratio error {r['ptat_err']:.3f} (>0.05)"

    assert r["ok"], f"{name}: composite pass/fail failed: {r}"
    pe = "n/a" if np.isnan(r["ptat_err"]) else f"{r['ptat_err']:.3f}"
    print(f"{name}: idc_err={r['idc_err']*100:.2f}% iv_rms={r['iv_rms']*100:.2f}% "
          f"g_GT={r['g_glf']*1e9:.2f}n g_MD={r['m_glf']*1e9:.2f}n sign_ok={r['sign_ok']} ptat={pe}")
