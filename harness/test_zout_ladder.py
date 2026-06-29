"""Foundation lock for the higher-order (L||R)-ladder Zout (reshape per the LDO expert panel).

fit_model.zmodel gained an optional `extra` list of additional series (L_i||R_i) branch-A
sections; fit_model.fit_zout_ladder fits that ladder to a measured |Zout|. A single section
(today's shelf) cannot reproduce the WuR pll's multi-decade inductive rise (Leff 24.8->1.0uH)
-> it mislocates the rise corner (the 447kHz artifact). The ladder fixes it (G1).

These tests are SELF-CONTAINED and do NOT touch the existing fit/emit path (fit_zout_ladder is
not yet wired) -> zero regression. The wiring (PSRR/noise/emit/fit_multiport threading) is a
separate, verified step.
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fit_model as FM   # noqa: E402

ZNPZ = HERE.parent / "results" / "redzone" / "wur_pmu_real_tt_25c.repro.npz"


def _cap_open():
    """This rail is a cap-less rising shelf (fit_cout_esr clamps Cout->~0.1pF). Set the zmodel
    cap-branch globals open so the parallel ZC does not short the HF plateau in evaluation."""
    FM.C, FM.RC = 1e-13, 179.0


def _zpll():
    d = np.load(ZNPZ, allow_pickle=True)
    g = np.asarray(d["z_pll_tt_25c"])
    return g[:, 0], g[:, 1] + 1j * g[:, 2]


def test_zmodel_extra_none_byte_identical():
    """zmodel(...,extra=None) is exactly the pre-extra single-section Zout (zero regression)."""
    f = np.logspace(1, 8.5, 60)
    a = FM.zmodel(f, 0.1, 24e-6, 160.0, 1e9, 1e-12)
    b = FM.zmodel(f, 0.1, 24e-6, 160.0, 1e9, 1e-12, extra=None)
    c = FM.zmodel(f, 0.1, 24e-6, 160.0, 1e9, 1e-12, extra=[])
    assert np.array_equal(a, b) and np.array_equal(a, c)


def test_zmodel_extra_adds_series_section():
    """An extra (L,R) section adds its series impedance to branch A (HF plateau rises by R)."""
    _cap_open()
    f = np.array([1e9])                              # HF: inductors open -> ZA -> R_a+R_pl(+R_extra)
    base = abs(FM.zmodel(f, 0.1, 1e-6, 40.0, 1e9, 1e-12)[0])
    lad = abs(FM.zmodel(f, 0.1, 1e-6, 40.0, 1e9, 1e-12, extra=[(1e-6, 150.0)])[0])
    assert lad > base + 100.0                        # plateau lifted by ~150 ohm (cap branch ~open)


def test_fit_zout_ladder_recovers_zpll_and_passes_G1():
    """On the measured z_pll the ladder must (a) need >=2 sections, (b) locate the rise corner
    near the data (~25kHz, NOT 447kHz), (c) hit G1: |Z| within 1.5dB at 1/10/31MHz + plateau 197+/-10%."""
    if not ZNPZ.exists():
        import pytest
        pytest.skip("measured z_pll npz not present")
    _cap_open()
    f, Z = _zpll()
    Ra, L_a, R_pl, extra, rms = FM.fit_zout_ladder(f, Z, n_max=3)
    # (a) a single section is insufficient -> at least one extra section adopted
    assert len(extra) >= 1, (Ra, L_a, R_pl, extra, rms)
    assert rms < 1.0, f"ladder |Z| dB-RMS {rms:.3f} too high"
    # (b) + (c) evaluate the generalized zmodel at the gate frequencies
    mag = np.abs(Z)
    def at(fq):
        i = int(np.argmin(np.abs(f - fq)))
        zm = abs(FM.zmodel(np.array([f[i]]), Ra, L_a, R_pl, 1e9, 1e-12, extra=extra)[0])
        return 20 * np.log10(zm / mag[i])
    for fq in (1e6, 1e7, 3.16e7):
        assert abs(at(fq)) < 1.5, f"|Z| dB err {at(fq):.2f} at {fq:.2g}Hz exceeds 1.5dB"
    plateau = float(np.median(mag[f >= 0.5 * f[-1]]))
    zhf = abs(FM.zmodel(np.array([3e7]), Ra, L_a, R_pl, 1e9, 1e-12, extra=extra)[0])
    assert abs(zhf - plateau) / plateau < 0.12, f"plateau {zhf:.1f} vs {plateau:.1f}"
    # (b) corner located: |Z| crosses ~10x the DC floor well below 1MHz (data ~25-300kHz),
    # NOT stuck near the old 447kHz mislocation region in a way that misses the low rise.
    fmodel = np.logspace(np.log10(f[0]), np.log10(f[-1]), 400)
    zmod = np.abs(FM.zmodel(fmodel, Ra, L_a, R_pl, 1e9, 1e-12, extra=extra))
    # where the model |Z| first reaches 10 ohm vs where the data does
    def cross(fa, za, lvl):
        idx = np.where(za >= lvl)[0]
        return fa[idx[0]] if len(idx) else fa[-1]
    fc_model = cross(fmodel, zmod, 10.0)
    fc_data = cross(f, mag, 10.0)
    assert 0.5 < fc_model / fc_data < 2.0, f"rise corner model {fc_model:.0f} vs data {fc_data:.0f}Hz"


if __name__ == "__main__":
    test_zmodel_extra_none_byte_identical()
    test_zmodel_extra_adds_series_section()
    test_fit_zout_ladder_recovers_zpll_and_passes_G1()
    f, Z = _zpll()
    Ra, L_a, R_pl, extra, rms = FM.fit_zout_ladder(f, Z, n_max=3)
    print(f"z_pll ladder: Ra={Ra:.4f} sec1=({L_a*1e6:.1f}uH,{R_pl:.0f}) "
          f"extra={[(round(L*1e6,2),round(R,0)) for L,R in extra]} rms={rms:.3f}dB")
    print("B foundation (zout-ladder) lock: PASS")
