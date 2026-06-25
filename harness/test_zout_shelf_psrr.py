"""Lock the #2a/#2b real-silicon fixes against a SYNTHETIC stand-in for the WuR PMU rails
(the real silicon GT stays local -- never committed -- so this exercises the same code
paths on a fabricated shelf rather than the npz). Two fixes are pinned:

  #2a  fit_model._is_shelf / fit_zout / fit_cout_esr -- a loop-ACTIVE output impedance is a
       rising shelf (Re Z<0, |Z|->HF plateau, no LC tank). The positive-real zmodel fakes an
       LC peak on it; the shelf branch fits |Z| magnitude-only with the cap held open.
  #2b  fit_model.fit_psrr -- the 3-real-section min-phase polish (`bank3`) is now ALWAYS a
       keep-best candidate (was gated to CFT>0), so a loop-active rail (CFT=0) whose PSRR the
       1-section shelf misses still fits.

The real-silicon acceptance (pll/vco PSRR 5.7->0.2 dB, Zout 7->1.7 dB) is validated locally
against /tmp/repro_wur.npz; it cannot live in the suite because silicon GT must stay local.
"""
import pathlib
import sys
import unittest.mock as mock

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fit_model as FM  # noqa: E402

TWO_PI = 2 * np.pi
F = np.logspace(1, np.log10(5e8), 150)


def _shelf_z(f, R0=0.1, Rpl=200.0, fz=3e6):
    """A loop-active output-impedance SHELF: |Z| rises monotonically R0 -> R0+Rpl with a
    corner at fz, and Re(Z)<0 across the band (the regulator loop sources current)."""
    x = f / fz
    mag = R0 + Rpl * x / np.sqrt(1 + x ** 2)          # monotone R0 -> R0+Rpl
    return mag * (-1 - 0.1j) / np.hypot(1, 0.1)       # |Z|~mag, Re<0 (third quadrant)


def _lc_z(f, R0=0.5, Rp=200.0, L=2e-7, C=4e-10):
    """A PASSIVE parallel-RLC tank: |Z| peaks at resonance then ROLLS OFF, Re(Z)>=0
    everywhere. The shelf gate must NOT fire on this (resonant path stays byte-identical)."""
    s = 1j * TWO_PI * f
    Y = 1.0 / Rp + s * C + 1.0 / (s * L)
    return R0 + 1.0 / Y


def _zrms(Zm, Zg):
    return float(np.sqrt(np.mean((20 * np.log10(np.abs(Zm) / np.abs(Zg))) ** 2)))


# ----------------------------------------------------------------- gate discrimination
def test_is_shelf_discriminates_shelf_from_resonance_and_flat():
    assert FM._is_shelf(F, _shelf_z(F)) is True
    assert FM._is_shelf(F, _lc_z(F)) is False             # peak then rolloff, Re>=0
    assert FM._is_shelf(F, np.full_like(F, 5.0) + 0j) is False   # flat R
    # a HIGH-FREQUENCY resonance (peak near band top) is still rejected: Re Z>=0
    fhi = np.logspace(1, np.log10(5e8), 150)
    assert FM._is_shelf(fhi, _lc_z(fhi, L=4e-9, C=2e-11)) is False


# ----------------------------------------------------------------- #2a Zout shelf branch
def test_shelf_fit_recovers_magnitude_and_beats_resonant(tmp_path):
    Z = _shelf_z(F)
    p = tmp_path / "sh.npz"
    np.savez(p, loads=np.array(["x"]), z_x=np.c_[F, Z.real, Z.imag])

    keep = (FM.ref, FM.LOADS, FM.NOMINAL, FM.C, FM.RC, FM.CFT)
    try:
        FM.ref = np.load(p, allow_pickle=True)
        FM.LOADS, FM.NOMINAL, FM.CFT = ["x"], "x", 0.0
        # shelf ON: cap held open + magnitude-only shelf fit recovers |Z|
        FM.C, FM.RC = FM.fit_cout_esr()
        assert FM.C <= 1e-12, "cap branch must be held open on a shelf rail"
        zf = FM.fit_zout(F, Z)
        zrms_on = _zrms(FM.zmodel(F, *zf), Z)
        assert zrms_on < 2.0, f"shelf fit should track |Z|, got {zrms_on:.2f} dB"

        # shelf OFF (force the gate false): the resonant path fakes an LC peak -> far worse
        with mock.patch.object(FM, "_is_shelf", return_value=False):
            FM.C, FM.RC = FM.fit_cout_esr()
            zf2 = FM.fit_zout(F, Z)
            zrms_off = _zrms(FM.zmodel(F, *zf2), Z)
    finally:
        FM.ref, FM.LOADS, FM.NOMINAL, FM.C, FM.RC, FM.CFT = keep
    assert zrms_off > zrms_on + 2.0, (
        f"shelf gate must materially beat the resonant path: on={zrms_on:.2f} off={zrms_off:.2f}")


def test_resonant_rail_unchanged_by_shelf_gate(tmp_path):
    """A real LC tank does NOT trip the gate, so fit_zout takes the resonant path and the
    fitted cap is physical (not the open-cap clamp)."""
    Z = _lc_z(F)
    p = tmp_path / "lc.npz"
    np.savez(p, loads=np.array(["x"]), z_x=np.c_[F, Z.real, Z.imag])
    keep = (FM.ref, FM.LOADS, FM.NOMINAL, FM.C, FM.RC, FM.CFT)
    try:
        FM.ref = np.load(p, allow_pickle=True)
        FM.LOADS, FM.NOMINAL, FM.CFT = ["x"], "x", 0.0
        FM.C, FM.RC = FM.fit_cout_esr()
        assert FM.C > 1e-12, "resonant rail keeps its physical/envelope cap (gate off)"
        zf = FM.fit_zout(F, Z)
        assert _zrms(FM.zmodel(F, *zf), Z) < 3.0
    finally:
        FM.ref, FM.LOADS, FM.NOMINAL, FM.C, FM.RC, FM.CFT = keep


# ----------------------------------------------------------------- #2b PSRR ungated bank
def test_psrr_bank3_is_ungated_at_cft_zero():
    """The 3-real-section polish (`bank3`) must be a candidate when CFT=0 (the loop-active
    rail). To pin the UNGATE specifically -- not just "some multi-section path works" -- we
    DISABLE the SK and complex-bank candidates, leaving only {shelf, bank3}. The PSRR target
    is generated from a TRUE 3-real-section i_c, so bank3 can recover it (prms->0) while the
    1-section shelf cannot. If someone re-adds the old `if CFT>0` gate, bank3 never runs, the
    shelf wins, and this test fails."""
    Z = _shelf_z(F)
    keep = (FM.C, FM.RC, FM.CFT)
    try:
        FM.C, FM.RC, FM.CFT = 1e-13, 1e-3, 0.0           # loop-active rail, cap open
        zf = FM.fit_zout(F, Z)
        # PSRR = psrr_model of a true 3-real-section coupling current -> bank3 can recover it
        G_true = [0.05, 0.15, TWO_PI * 1e4, 0.08, TWO_PI * 3e5, 0.03, TWO_PI * 1e7]
        H = FM.psrr_model(F, *zf, G_true)
        sel = F >= 1e3

        # only {shelf, bank3} live: SK + complex disabled so bank3 alone can fit the 3 sections
        with mock.patch.object(FM, "_sk_fit", return_value=None), \
             mock.patch.object(FM, "_bank_fit", return_value=None):
            G, Q = FM.fit_psrr(F, H, *zf)
        prms = _zrms(FM.psrr_model(F, *zf, G, Q)[sel], H[sel])
        assert prms < 1.0, f"ungated bank3 should recover the 3-section PSRR, got {prms:.2f} dB"

        # the single shelf section alone cannot -> proves bank3 (not the shelf) carried it
        Gs, _ = FM._shelf(F, H, *zf)
        prms_shelf = _zrms(FM.psrr_model(F, *zf, Gs)[sel], H[sel])
        assert prms_shelf > prms + 1.0, (
            f"1-section shelf={prms_shelf:.2f} should be far worse than bank3={prms:.2f}")
    finally:
        FM.C, FM.RC, FM.CFT = keep
