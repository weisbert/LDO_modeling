"""STAGE 1b -- the importmp READ-side primitives for the coverage kinds (+ guardrail-3).

NO sim / Virtuoso / dsub: every test builds a FAKE parsed-PSF dict (the shape psf.read_psf
returns: {sweep-axis: ndarray, "<sig>": ndarray, "_sweep": name}) and drives the actual
importmp._derive / check_zout_dc_consistency directly. These pin:

  * the new DC `iv` derive (reused i_out vdc I-V sweep -> [Vsweep, I.real])
  * the new `dropout` derive (v_out load isource DC sweep -> [Iload, Vout])  (sibling of iv)
  * the new transient `trans` derive (slew waveform -> [t, V.real])
  * GUARDRAIL-2: current-PSRR SIGN preservation (PI = -I/Vsup kept signed, NOT |PI|)
  * GUARDRAIL-3: check_zout_dc_consistency (Zout(s->0) <-> DC load-reg slope) warnings
  * _EXT now resolves a .dc / .tran PSF file (_find_psf_file over a stub file)

Run:  python -m pytest cadence/insitu/test_importmp_derives.py -q
"""
import json
import pathlib
import re
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # .../cadence on sys.path (bare-import convention)

from insitu import importmp as IMP                                          # noqa: E402
from insitu import manifest as M                                            # noqa: E402

WUR = HERE / "manifests" / "wur_pmu_top.json"


def _resolved_wur():
    """A RESOLVED copy of the shipped wur manifest ('<net:X>' -> 'X'), loaded + validated."""
    import tempfile
    raw = re.sub(r"<net:([^>]+)>", r"\1", WUR.read_text())
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(json.loads(raw), f)
    f.close()
    return M.load(f.name)


# =====================================================================================
# (1) the new DC `iv` derive: reused i_out vdc I-V sweep -> [Vsweep, I.real]
# =====================================================================================
def test_iv_derive_returns_vsweep_and_signed_current():
    # the iv point reads ('i', <probe>) and carries derive='iv'; the swept vdc voltage is the DC
    # axis (named "dc" by `dc ... param=dc`), the probe current is <probe>:p (DC -> real).
    probe = "Vbias_500n_lpf"
    Vsweep = np.array([0.0, 0.5, 1.0, 1.28])
    Iprobe = np.array([1e-9, 2.0e-7, 4.5e-7, 5.0e-7])     # current INTO the sink, real (DC)
    d = {"dc": Vsweep, f"{probe}:p": Iprobe, "_sweep": "dc"}
    point = dict(tag="iv_i500n_lpf", derive="iv", reads=[("i", probe)])
    arr = IMP._derive(point, d)
    assert arr.shape == (4, 2)
    assert np.allclose(arr[:, 0], Vsweep)                 # col 0 = the swept voltage axis
    assert np.allclose(arr[:, 1], Iprobe)                 # col 1 = the raw probe current (into sink)


def test_iv_derive_resolves_cli_probe_alias():
    # under the CLI gold the probe is named Vb500 (not <manifest probe>:p); the alias resolves it
    probe = "Vprobe_i500n"
    Vsweep = np.array([0.0, 0.6, 1.2])
    Ial = np.array([0.0, 3e-7, 6e-7])
    d = {"dc": Vsweep, "Vb500:p": Ial, "_sweep": "dc"}    # foreign probe key
    point = dict(tag="iv_i500n", derive="iv", reads=[("i", probe)])
    arr = IMP._derive(point, d, probe_alias="Vb500:p")
    assert np.allclose(arr[:, 1], Ial)


def test_iv_sweep_axis_falls_back_when_named_differently():
    # box-pending robustness: a binary PSF may name the swept axis "sweep" (not "dc"); _sweep_axis
    # still finds it via the d["_sweep"] stamp / the kinds fallback list.
    probe = "Vbias_500n_lpf"
    Vsweep = np.array([0.0, 0.5, 1.0])
    d = {"sweep": Vsweep, f"{probe}:p": np.array([0.0, 1e-7, 2e-7]), "_sweep": "sweep"}
    point = dict(tag="iv_x", derive="iv", reads=[("i", probe)])
    arr = IMP._derive(point, d)
    assert np.allclose(arr[:, 0], Vsweep)


# =====================================================================================
# (1b) the `dropout` derive (sibling of iv): v_out load isource DC sweep -> [Iload, Vout]
# =====================================================================================
def test_dropout_derive_returns_iload_and_vout():
    onet = "VDD0P8_PLL"
    Iload = np.array([1e-4, 5e-4, 1e-3, 3e-3])
    Vout = np.array([0.802, 0.800, 0.798, 0.790])         # droops as load rises (load-reg)
    d = {"dc": Iload, onet: Vout, "_sweep": "dc"}
    point = dict(tag="dc_pll", derive="dropout", reads=[("v", onet)])
    arr = IMP._derive(point, d)
    assert arr.shape == (4, 2)
    assert np.allclose(arr[:, 0], Iload) and np.allclose(arr[:, 1], Vout)


# =====================================================================================
# (2) the new transient `trans` derive: slew waveform -> [t, V.real]
# =====================================================================================
def test_trans_derive_returns_time_and_voltage():
    onet = "VDD0P8_PLL"
    t = np.array([0.0, 1e-9, 2e-9, 3e-9])
    V = np.array([0.800, 0.799, 0.7985, 0.800])           # a small slew transient on Vout
    d = {"time": t, onet: V, "_sweep": "time"}
    point = dict(tag="tr_pll_step1", derive="trans", reads=[("v", onet)])
    arr = IMP._derive(point, d)
    assert arr.shape == (4, 2)
    assert np.allclose(arr[:, 0], t)                      # col 0 = the time axis
    assert np.allclose(arr[:, 1], V)                      # col 1 = the RAW node voltage (no fit)


def test_trans_time_axis_falls_back_to_sweep_stamp():
    # a box binary PSF surfacing the time column only via _sweep -> _time_axis still resolves it
    onet = "VDD0P8_PLL"
    t = np.array([0.0, 1e-9, 2e-9])
    d = {"time": t, onet: np.array([0.8, 0.8, 0.8]), "_sweep": "time"}
    point = dict(tag="tr_x", derive="trans", reads=[("v", onet)])
    arr = IMP._derive(point, d)
    assert np.allclose(arr[:, 0], t)


def test_trans_time_axis_rejects_foreign_axis_fails_loud():
    # a non-time axis (e.g. an AC PSF's freq mis-routed to a tran read) must FAIL LOUD, not be
    # silently returned as the time column.
    onet = "VDD0P8_PLL"
    d = {"freq": np.array([10.0, 100.0]), onet: np.array([0.8, 0.8]), "_sweep": "freq"}
    point = dict(tag="tr_bad", derive="trans", reads=[("v", onet)])
    with pytest.raises(KeyError):
        IMP._derive(point, d)


# =====================================================================================
# (3) GUARDRAIL-2: current-PSRR SIGN preservation (PI = -I/Vsup kept signed, NOT |PI|)
# =====================================================================================
def test_cpsrr_derive_preserves_sign_not_magnitude():
    # WHY the sign matters: shared-VREF current sinks superpose their ripple currents -- a PSRR
    # that collapses to |PI| loses the phase that determines whether ripple ADDS or CANCELS at the
    # shared node. importmp keeps the full complex PI = -I/Vsup.
    f = np.array([10.0, 1000.0])
    # choose I, Vsup so PI = -I/Vsup is NEGATIVE-real (a real check the sign is intact)
    Vsup = np.array([1.0 + 0j, 1.0 + 0j])
    I = np.array([0.5 + 0j, 2.0 + 0j])                    # PI = -I/Vsup = -0.5, -2.0  (negative!)
    d = {"freq": f, "Vbias_500n_lpf:p": I, "AVDD1P0": Vsup}
    point = dict(tag="pi_i500n_lpf_avdd1p0", derive="pi",
                 reads=[("i", "Vbias_500n_lpf"), ("v", "AVDD1P0")])
    arr = IMP._derive(point, d)
    # the real part is NEGATIVE (the sign is intact) -- NOT the magnitude (which would be +0.5/+2.0)
    assert np.allclose(arr[:, 1], [-0.5, -2.0])           # PI.real, signed
    assert (arr[:, 1] < 0).all(), "current-PSRR sign collapsed to magnitude (GUARDRAIL-2 break)"
    assert np.allclose(arr[:, 2], [0.0, 0.0])             # PI.imag


# =====================================================================================
# (3b) z-derive PASSIVITY sign-normalize (reused-LOAD-source injection draws from the node)
# =====================================================================================
def test_z_derive_passivity_sign_normalizes_negative_real():
    """A reused LOAD source DRAWS +1A from the rail (Iload '(out ground)') so the raw V/I is
    -Zout -> negative-real. The z derive must sign-normalize to a PASSIVE Zout (Re(s->0)>=0).
    An inject-orientation array (Iext/Iac '(ground out)', synthetic + insert path) is already
    positive-real and must be left BYTE-IDENTICAL (that is why the synthetic 220-suite is safe)."""
    f = np.logspace(1, 8, 40)
    Z = 0.1 + 1j * 1e-9 * f                              # the physical passive Zout (+real)
    pt = dict(tag="z_pll", derive="z", reads=[("v", "VOUT")])
    out_neg = IMP._derive(pt, {"freq": f, "VOUT": -Z})   # reuse-draw: raw = -Zout
    out_pos = IMP._derive(pt, {"freq": f, "VOUT": Z})    # inject:     raw = +Zout
    assert out_neg[0, 1] > 0 and out_pos[0, 1] > 0       # both come out positive-real at DC
    assert np.allclose(out_neg, out_pos)                 # the flip recovered the same physical Zout
    assert np.allclose(out_pos[:, 1], Z.real) and np.allclose(out_pos[:, 2], Z.imag)  # +real untouched


def test_z_derive_passivity_anchors_on_dc_not_hf():
    """Anchor is the LOW-FREQ real part: a physical Zout whose Re goes NEGATIVE at HF (phase wraps
    past +/-90 near the output resonance) must NOT be flipped -- only the DC floor decides. (The
    real WuR Zout's Re happens to stay negative across the band, but a band-average rule would
    misfire on a chip whose HF Re wraps positive while DC is the real discriminator -- guard it.)"""
    f = np.logspace(1, 8, 60)
    Zhf = (0.1 + 1j * 2e-9 * f).astype(complex)         # +real at DC
    Zhf.real[f > 3e7] = -50.0                            # force HF real strongly NEGATIVE
    pt = dict(tag="z_vco", derive="z", reads=[("v", "VOUT")])
    out = IMP._derive(pt, {"freq": f, "VOUT": Zhf})
    assert out[0, 1] > 0                                 # DC floor is +real -> NOT flipped
    assert np.allclose(out[:, 1], Zhf.real)             # left as-is despite HF Re<0


def test_couple_derive_not_passivity_normalized():
    """couple is a TRANSFER impedance (not constrained positive-real) -> left raw, NOT flipped."""
    f = np.logspace(1, 8, 20)
    Zneg = -(0.1 + 1j * 1e-9 * f)
    ptc = dict(tag="couple_pll_vco", derive="couple", reads=[("v", "VB")])
    out = IMP._derive(ptc, {"freq": f, "VB": Zneg})
    assert out[0, 1] < 0                                 # unchanged negative real (by design)


# =====================================================================================
# (4) GUARDRAIL-3: check_zout_dc_consistency (Zout(s->0) <-> DC load-reg slope)
# =====================================================================================
def _z_array(f, rout):
    """A flat Zout = rout (purely resistive) over freqs f -> rows [f, re, im]."""
    re_ = np.full_like(np.asarray(f, float), float(rout))
    im_ = np.zeros_like(re_)
    return np.c_[np.asarray(f, float), re_, im_]


def _dropout_array(iloads, rout, v0=0.8):
    """A dropout curve with slope dVout/dIload = -rout (Vout = v0 - rout*Iload)."""
    il = np.asarray(iloads, float)
    return np.c_[il, v0 - rout * il]


def test_zout_dc_consistent_pair_no_warning():
    m = _resolved_wur()
    rout = 12.0
    # PHYSICAL convention: a real LDO droops under load -> dVout/dIload = -rout (NEGATIVE). The check
    # compares MAGNITUDES |Zout(0)| ~= |dVout/dIload|, so Zout(0)=12 ohm and a -12 V/A load-reg slope
    # AGREE (|−12| == 12) and must NOT warn. (Pre-fix the check compared signed slope and would have
    # spuriously warned on this physically-correct curve.)
    f = np.array([10.0, 100.0, 1000.0])
    ref = {"z_pll_nom": _z_array(f, rout),
           "dc_pll": _dropout_array([1e-4, 5e-4, 1e-3, 3e-3], rout)}    # slope = -12; |slope| == zdc
    warns = IMP.check_zout_dc_consistency(ref, m, tol=0.25)
    assert warns == [], warns


def test_zout_dc_mismatch_pair_one_warning_names_output():
    m = _resolved_wur()
    f = np.array([10.0, 100.0, 1000.0])
    # Zout(0) = 12 ohm but the DC curve is a -3 V/A droop -> |slope|=3; rel mismatch (12-3)/3 = 3.0 > tol
    ref = {"z_pll_nom": _z_array(f, 12.0),
           "dc_pll": _dropout_array([1e-4, 5e-4, 1e-3, 3e-3], 3.0)}     # slope = -3, |slope| far from 12
    warns = IMP.check_zout_dc_consistency(ref, m, tol=0.25)
    assert len(warns) == 1
    w = warns[0]
    assert "pll" in w and "GUARDRAIL-3" in w
    assert "12" in w and "3" in w                          # both numbers reported
    # the OTHER output (vco) has no dropout array -> it is NOT warned about
    assert "vco" not in w


def test_zout_dc_missing_dropout_returns_empty():
    m = _resolved_wur()
    f = np.array([10.0, 100.0, 1000.0])
    # only the AC Zout present, no dc_<o> at all -> nothing to check, no warning
    ref = {"z_pll_nom": _z_array(f, 99.0), "z_vco_nom": _z_array(f, 5.0)}
    assert IMP.check_zout_dc_consistency(ref, m) == []


def test_zout_dc_missing_zout_returns_empty():
    m = _resolved_wur()
    # only the dropout present, no z_<o>_<load> -> nothing to check
    ref = {"dc_pll": _dropout_array([1e-4, 1e-3], -7.0)}
    assert IMP.check_zout_dc_consistency(ref, m) == []


# =====================================================================================
# (5) _EXT now resolves a .dc / .tran PSF file (_find_psf_file over a stub dir)
# =====================================================================================
def test_ext_has_dc_and_tran():
    assert IMP._EXT["dc"] == ".dc" and IMP._EXT["tran"] == ".tran"
    # the original analyses are untouched
    assert IMP._EXT["ac"] == ".ac" and IMP._EXT["noise"] == ".noise"


def test_find_psf_file_resolves_dc_file(tmp_path):
    # a stub .dc file directly -> returned as-is
    f = tmp_path / "dcswp.dc"
    f.write_text("// stub dc PSF\n")
    assert IMP._find_psf_file(f, "dc") == f
    # a dir holding raw/<x>.dc (the CLI layout) -> the .dc under it is found
    d = tmp_path / "work" / "iv_i500n_lpf"
    (d / "raw").mkdir(parents=True)
    (d / "raw" / "dcswp.dc").write_text("// stub\n")
    assert IMP._find_psf_file(d, "dc") == d / "raw" / "dcswp.dc"


def test_find_psf_file_resolves_tran_file(tmp_path):
    d = tmp_path / "work" / "tr_pll_step1"
    (d / "psf").mkdir(parents=True)                        # the ADE layout subdir
    (d / "psf" / "trn.tran").write_text("// stub tran PSF\n")
    assert IMP._find_psf_file(d, "tran") == d / "psf" / "trn.tran"


def test_find_psf_file_missing_dc_raises(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        IMP._find_psf_file(d, "dc")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
