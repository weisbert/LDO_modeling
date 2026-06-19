"""SPECTRE-GATED regression: lock in the local Spectre-18.1 validation of the COVERAGE
dc/tran/iv path (HANDOFF_MODELING_COVERAGE STAGE-1b kinds), end to end through the REAL
offline netlister into the REAL Spectre 18.1, read back through the REAL importmp firewall.

This is the simulator-backed counterpart to cluster/test_netlist_augment.py: that suite
proves the EMITTED TEXT is shaped right with NO simulator; this one proves the emitted text
actually CONVERGES in Spectre 18.1 and that importmp._derive on the resulting REAL PSF returns
the right shape + physics:

  * dropout (v_out 'pll' DC iload sweep)   -> [Iload, Vout], slope dVout/dIload == -20 (the DUT
        Rout). Doubles as GUARDRAIL-3: check_zout_dc_consistency does NOT warn when a matching
        z_<o> AC array (|Zout(0)|=20) is supplied, and DOES warn on a mismatched one (200).
  * iv      (i_out 'i500n_lpf' vdc I-V sweep) -> [Vsweep, I], current into the 800k sink == V/800k.
  * trans   (v_out 'pll' transient load step) -> [t, V], a genuine ascending time axis.

The DUT is an INLINE behavioral subckt 'WuR_PMU_TOP' (no external include -> Spectre converges
offline): each v_out is a 0.8 V ideal source behind Rout=20 (Vout = 0.8 - 20*Iload), each i_out
bias net is an 800k resistor to VSS. The designer-reused sources (V_AVDD supply, Iload_* load
isources, Vbias_* compliance vsources) are kept EXACTLY as test_netlist_augment._base_scs() ships
them, so the REAL factory's source-reuse pass drives the real Spectre run.

We also assert the per-engine PSF axis NAMES this session confirmed (dc-sweep _sweep=='dc',
tran _sweep=='time'), so importmp's box-PSF fallbacks are NOT exercised here, and that the
temp=55 coverage options line was emitted + accepted (covered by convergence).

SKIP cleanly (reported skipped, not passed) when Spectre is absent; the guard honours a
SPECTRE_HOME env override so the skip path is testable:
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/cluster/test_coverage_spectre.py -q
        -> the module reports as skipped.

Run:  python3 -m pytest cadence/cluster/test_coverage_spectre.py -q
"""
import json
import os
import pathlib
import re
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # .../cadence on sys.path (bare-import convention)

import spectre_run as sr                                                    # noqa: E402
from cluster import netlist_augment as NA                                   # noqa: E402
from insitu import manifest as M                                           # noqa: E402
from insitu import run as RUN                                              # noqa: E402
from insitu import importmp as IM                                          # noqa: E402

WUR = HERE.parent / "insitu" / "manifests" / "wur_pmu_top.json"


# ----------------------------------------------------------------- skip guard
def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


# ----------------------------------------------------------------- fixtures
# The behavioral DUT, defined INLINE so Spectre converges with NO external include. The pin
# order is the wur WuR_PMU_TOP order verbatim:
#   AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO IBP_PTAT_TUNE_1P5U_VCO VSS
# Each v_out (VDD0P8_PLL/VDD0P8_VCO) is a 0.8 V ideal source behind Rout=20 (so Vout = 0.8 -
# 20*Iload -> dropout slope EXACTLY -20). Each i_out bias net is an 800k resistor to VSS (so the
# I-V sweep reads I = Vsweep/800k into the sink).
_INLINE_DUT = (
    "subckt WuR_PMU_TOP (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF "
    "IBP_POLY_3P6U_VCO IBP_PTAT_TUNE_1P5U_VCO VSS)\n"
    "  Vpll_ideal (npll VSS) vsource dc=0.8\n"
    "  Rpll (npll VDD0P8_PLL) resistor r=20\n"
    "  Vvco_ideal (nvco VSS) vsource dc=0.8\n"
    "  Rvco (nvco VDD0P8_VCO) resistor r=20\n"
    "  Rsink_500n (IBP_POLY_500N_LPF VSS) resistor r=800k\n"
    "  Rsink_3p6u (IBP_POLY_3P6U_VCO VSS) resistor r=800k\n"
    "  Rsink_ptat (IBP_PTAT_TUNE_1P5U_VCO VSS) resistor r=800k\n"
    "ends WuR_PMU_TOP\n"
)

# Rout of each v_out ideal-source-behind-resistor (the dropout slope magnitude + the GUARDRAIL-3
# matching |Zout(0)|) and the i_out sink resistance (the I-V slope is 1/Rsink).
_ROUT = 20.0
_RSINK = 800e3


def _base_scs():
    """A self-contained behavioral base .tran TB. Mirrors test_netlist_augment._base_scs()
    EXACTLY -- the DUT instance + the designer's OWN source on every tagged pin (the named *_src
    instances the manifest reuses) + a .tran analysis to be stripped -- but with the external
    'include "models.scs"' REPLACED by the INLINE behavioral 'WuR_PMU_TOP' subckt (same 7
    pins/order) so Spectre converges offline. The DUT's 7th pin (VSS) is tied to global 0 so the
    behavioral sources/sink resistors have a return path."""
    return (
        "simulator lang=spectre\n"
        + _INLINE_DUT
        + "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO "
          "IBP_PTAT_TUNE_1P5U_VCO 0) WuR_PMU_TOP\n"
        + "V_AVDD (AVDD1P0 0) vsource dc=0.98\n"
        + "Iload_pll (VDD0P8_PLL 0) isource dc=500u\n"
        + "Iload_vco (VDD0P8_VCO 0) isource dc=2m\n"
        + "Vbias_500n_lpf (IBP_POLY_500N_LPF 0) vsource dc=1.28\n"
        + "Vbias_3p6u_vco (IBP_POLY_3P6U_VCO 0) vsource dc=1.28\n"
        + "Vbias_1p5u_ptat (IBP_PTAT_TUNE_1P5U_VCO 0) vsource dc=0.667\n"
        + "tt tran stop=1u\n"
    )


_TMP = []


def _write_tmp(obj):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(obj, f)
    f.close()
    _TMP.append(f.name)
    return f.name


def _resolved_coverage_manifest():
    """A RESOLVED wur manifest ('<net:X>' -> 'X') with COVERAGE params injected so the dc/tran/iv
    coverage groups appear: an iv sweep on the i500n_lpf sink, a dropout sweep on the pll v_out, a
    transient step on pll, the 2x lin-gate self-check. Mirrors
    test_netlist_augment._resolved_coverage_manifest(), but keeps the sweeps SMALL (few points) so
    the Spectre runs are fast."""
    raw = re.sub(r"<net:([^>]+)>", r"\1", WUR.read_text())
    d = json.loads(raw)
    d["coverage"] = {
        "tier": "T4",
        "iv": {"i500n_lpf": {"sweep": {"type": "lin", "start": 0.0, "stop": 1.0, "n": 4}}},
        "dropout": {"pll": {"sweep": {"type": "log", "start": 1e-4, "stop": 3e-3, "n": 4}}},
        "transient": {"pll": {"steps": [{"from": 5e-4, "to": 2e-3, "label": "step1"}],
                              "edge": 1e-9, "tstop": 1e-5, "tstep": 1e-7}},
        "lin_gate": True,
    }
    return M.load(_write_tmp(d))


def _base_dir(tmp_path):
    d = tmp_path / "base"
    d.mkdir()
    (d / "input.scs").write_text(_base_scs())
    return d


def _group(m, tag):
    return next(g for g in RUN.groups(m) if g["tag"] == tag)


def _point(m, tag):
    """The measurement POINT dict whose tag matches (carries derive/reads -- the importmp._derive
    contract)."""
    return next(p for p in M.measurements(m) if p["tag"] == tag)


# A module-level cache so the (slow) Spectre runs happen ONCE per group, not per assertion.
_RUN_CACHE = {}


def _run_group(tmp_path, m, group_tag, temp=55):
    """Build group `group_tag`'s netlist via the REAL offline factory (temp=55) and feed its TEXT
    to the REAL Spectre. Returns (emitted_input_scs_text, the parsed-analysis dict). Cached by
    group tag (one Spectre invocation per group across the whole module)."""
    if group_tag in _RUN_CACHE:
        return _RUN_CACHE[group_tag]
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out", temp=temp)
    netdir = gnl(_group(m, group_tag))
    scs_text = pathlib.Path(netdir, "input.scs").read_text()
    out = sr.run(scs_text, "covspectre_" + group_tag)
    _RUN_CACHE[group_tag] = (scs_text, out)
    return scs_text, out


def _analysis_dict(out):
    """The single non-'_log' analysis dict Spectre produced for a one-analysis coverage netlist."""
    names = [k for k in out if k != "_log"]
    assert len(names) == 1, f"expected exactly one analysis PSF, got {names}"
    return out[names[0]]


# =====================================================================================
# (1) the dc dropout group: converges, _sweep=='dc', importmp -> [Iload, Vout] slope == -20
# =====================================================================================
def test_dropout_group_converges_and_slope_is_minus_rout(tmp_path):
    m = _resolved_coverage_manifest()
    scs, out = _run_group(tmp_path, m, "g_dc_pll")
    # CONVERGED: the dc-sweep analysis dict is present (sr.run did not raise)
    ad = _analysis_dict(out)
    # the exact axis name this session confirmed -- importmp's box-PSF fallbacks must NOT be needed
    assert ad["_sweep"] == "dc"
    # importmp._derive on the REAL PSF returns the dropout [Iload, Vout] shape + physics
    arr = IM._derive(_point(m, "dc_pll"), ad)
    assert arr.ndim == 2 and arr.shape[1] == 2 and arr.shape[0] >= 2
    Iload, Vout = arr[:, 0], arr[:, 1]
    assert np.all(np.diff(Iload) > 0)                 # an ascending real load-current axis
    slope = float(np.polyfit(Iload, Vout, 1)[0])      # dVout/dIload
    assert slope == pytest.approx(-_ROUT, rel=1e-3)   # the DUT Rout = -20 exactly


def test_dropout_guardrail3_fires_on_matching_and_mismatched_zout(tmp_path):
    # GUARDRAIL-3: check_zout_dc_consistency cross-checks Zout(s->0) vs the DC load-reg slope.
    # Feed a ref carrying the REAL dc_<o> dropout array + a MATCHING z_<o>_<load> AC array
    # (|Zout(0)| == 20 == |slope|) -> NO warning; then a MISMATCHED z (|Zout(0)| == 200) -> it DOES
    # warn (prove the guardrail actually fires off the real Spectre dropout curve).
    m = _resolved_coverage_manifest()
    _, out = _run_group(tmp_path, m, "g_dc_pll")
    drop = IM._derive(_point(m, "dc_pll"), _analysis_dict(out))   # the REAL [Iload, Vout]

    def _z_ref(zout_mag):
        # a single-row AC Zout array [f, re, im] whose magnitude at f->0 is zout_mag (purely real)
        z = np.array([[1.0, zout_mag, 0.0]])
        return {"loads": np.array(["nom"]), "z_pll_nom": z, "dc_pll": drop}

    matched = IM.check_zout_dc_consistency(_z_ref(_ROUT), m)
    assert matched == [], f"matching Zout(0)=20 must NOT warn, got: {matched}"

    mismatched = IM.check_zout_dc_consistency(_z_ref(10 * _ROUT), m)
    assert any("pll" in w and "GUARDRAIL-3" in w for w in mismatched), \
        f"mismatched Zout(0)=200 vs DC slope must warn, got: {mismatched}"


# =====================================================================================
# (2) the dc I-V group: converges, _sweep=='dc', importmp -> [Vsweep, I], I == V/Rsink
# =====================================================================================
def test_iv_group_converges_and_current_is_v_over_rsink(tmp_path):
    m = _resolved_coverage_manifest()
    scs, out = _run_group(tmp_path, m, "g_iv_i500n_lpf")
    ad = _analysis_dict(out)
    assert ad["_sweep"] == "dc"                       # the confirmed DC-sweep axis name
    arr = IM._derive(_point(m, "iv_i500n_lpf"), ad)   # [Vsweep, I]
    assert arr.ndim == 2 and arr.shape[1] == 2 and arr.shape[0] >= 2
    Vsweep, I = arr[:, 0], arr[:, 1]
    assert np.all(np.diff(Vsweep) > 0)                # an ascending real DC voltage axis
    # the swept vdc drives the 800k sink: |I| == Vsweep/Rsink (the probe :p sign is into the
    # source, so I is negative as Vsweep rises; compare magnitudes against V/Rsink).
    expected = Vsweep / _RSINK
    assert np.allclose(np.abs(I), expected, atol=1e-12)
    # the slope of |I| vs V is the sink conductance 1/Rsink
    g = float(np.polyfit(Vsweep, np.abs(I), 1)[0])
    assert g == pytest.approx(1.0 / _RSINK, rel=1e-3)


# =====================================================================================
# (3) the transient group: converges, _sweep=='time', importmp -> [t, V] real time axis
# =====================================================================================
def test_tran_group_converges_and_has_real_time_axis(tmp_path):
    m = _resolved_coverage_manifest()
    scs, out = _run_group(tmp_path, m, "g_tr_pll_step1")
    ad = _analysis_dict(out)
    assert ad["_sweep"] == "time"                     # the confirmed tran axis name
    arr = IM._derive(_point(m, "tr_pll_step1"), ad)   # [t, V]
    assert arr.ndim == 2 and arr.shape[1] == 2 and arr.shape[0] >= 2
    t, V = arr[:, 0], arr[:, 1]
    assert np.all(np.diff(t) >= 0)                    # a non-decreasing time axis
    assert t[0] == pytest.approx(0.0, abs=1e-12)
    assert t[-1] == pytest.approx(1e-5, rel=1e-3)     # runs to tstop=1e-5
    # the v_out node sits near the 0.8 V ideal rail (it sags under the load step, never floats/NaN)
    assert np.all(np.isfinite(V))
    assert 0.7 < float(V[0]) < 0.81


# =====================================================================================
# (4) the temp=55 coverage options line was emitted + accepted by Spectre
# =====================================================================================
def test_covtemp_line_emitted_and_accepted(tmp_path):
    # the temp=55 options line is in the emitted netlist (and -- since the run converged in (1) --
    # Spectre accepted it; convergence is the real acceptance test, the grep just pins the text).
    m = _resolved_coverage_manifest()
    scs, out = _run_group(tmp_path, m, "g_dc_pll")
    tline = next(l for l in scs.splitlines()
                 if l.strip().startswith(NA.COVTEMP_NAME + " "))
    assert tline.split("//")[0].split() == [NA.COVTEMP_NAME, "options", "temp=55"]
    assert "fatal error" not in out["_log"]           # the run carrying that line did not fault


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
