"""SPECTRE-GATED regression: the emitted LDO model PROPAGATES supply (AVDD) noise to its
output, shaped by the model's PSRR -- i.e. a noise file on the supply pin shows up at vout
as input_ASD(f) * |H_supply->out(f)|.

This is the headline supply-noise question: "if AVDD1P0 carries a noise file, does the LDO
model simulate its effect on the output?" Answer pinned here, in LOCAL Cadence Spectre on the
emitted Verilog-A, against the model's OWN measured supply->output transfer:

  1. fit v2_capless -> emit ldo_model.va (it carries a PSRR coupling path i_couple(s)*Zout).
  2. AC: drive supply vin with mag=1 -> Hsup(f) = |vout| (the supply->output transfer).
  3. NOISE with a floor+spurs noise_table [V^2/Hz] on vin -> total output noise.
  4. NOISE with a quiet supply -> intrinsic model noise only.
  5. quadrature-isolate the supply-induced part: sqrt(out_with^2 - out_intrinsic^2).
  6. assert supply_out(f) == input_ASD(f) * Hsup(f) at the spur freqs (tight), and that at
     the dominant spur the supply term actually rises above the intrinsic floor (it really
     propagates, not just numerically agrees with ~0).

Measured locally: agreement < 1% at every spur. Self-contained -- the noise_table is built
inline (flat floor + 2 Lorentzian spurs), no research artifact needed.

SKIP cleanly when Spectre is absent (SPECTRE_HOME env-overridable guard):
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/test_supply_noise_prop_spectre.py -q -> skipped

Run:  python3 -m pytest cadence/test_supply_noise_prop_spectre.py -q
"""
import os
import pathlib
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

VARIANT = "v2_capless"
SW = 1.0e-15                       # flat floor PSD [V^2/Hz]  (~3.16e-8 V/rtHz)
SPURS = [(2.0e6, 6.0e-12, 1200), (6.0e6, 1.5e-12, 1200)]   # (f0, peak PSD V^2/Hz, Q)


def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


def _psd(f):
    """Supply noise PSD [V^2/Hz]: flat floor + Lorentzian spurs (power adds)."""
    s = np.full_like(np.asarray(f, float), SW)
    for f0, pk, q in SPURS:
        h = f0 / (2.0 * q)
        s = s + pk / (1.0 + ((np.asarray(f, float) - f0) / h) ** 2)
    return s


# MANDATORY spur sampling for a `.noise` sweep: include each spur center + very-short-step
# points either side (in HWHM=f0/(2Q) units). A coarse log sweep steps over the sub-kHz spur
# and Spectre returns the floor -- the spur "doesn't simulate". The tightest steps sit on the
# peak; the wider ones trace the skirt. Q-aware so it auto-narrows for higher-Q spurs.
SPUR_MULTS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0)


def _eval_freqs():
    g = list(np.logspace(np.log10(100.0), np.log10(5e7), 80))
    for f0, _, q in SPURS:
        h = f0 / (2.0 * q)
        g.append(f0)
        for m in SPUR_MULTS:
            g += [f0 - m * h, f0 + m * h]
    return np.array(sorted(x for x in set(g) if 100.0 <= x <= 5e7))


def _vlist(fs):
    return "[" + " ".join(f"{x:.6e}" for x in fs) + "]"


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sn_prop")
    res = FM.fit_variant(VARIANT)
    va = tmp / "ldo_model.va"
    FM.emit_va(res.P, va, tmp / "ldo_model_dropout.tbl")
    # noise_table file the supply vsource reads (V^2/Hz), sampled fine enough for the spurs
    fs = _eval_freqs()
    dense = np.unique(np.concatenate([
        np.logspace(np.log10(10.0), np.log10(1e8), 2000),
        np.concatenate([np.linspace(f0 - 8 * f0 / (2 * q), f0 + 8 * f0 / (2 * q), 200)
                        for f0, _, q in SPURS])]))
    dense = dense[(dense >= 10.0) & (dense <= 1e8)]
    tbl = tmp / "nf.dat"
    tbl.write_text("".join(f"{fi:.6e} {pi:.6e}\n" for fi, pi in zip(dense, _psd(dense))))
    return va, tbl, FM._amps(res.nominal), float(res.vref), fs


def _run_noise(scs, tag, tmp):
    saved = sr.WORK
    sr.WORK = tmp / f"sp_{tag}"
    try:
        return sr.run(scs, tag)
    finally:
        sr.WORK = saved


def test_supply_noise_propagates_as_input_times_psrr(model):
    va, tbl, op, vdd, fs = model
    tmp = tbl.parent
    vl = _vlist(fs)
    head = (f'simulator lang=spectre\nahdl_include "{va.resolve()}"\n'
            f"Ild (vout 0) isource dc={op:.6e}\n"
            f"Xdut (vin vout 0) ldo_model iload={op:.6e} slew_en=0 vdd={vdd:g}\nsave vout\n")

    # AC supply->out transfer
    d = _run_noise(head + f"Vsup (vin 0) vsource dc={vdd:g} mag=1\nac ac values={vl}\n",
                   "sn_ac", tmp)
    fac = np.asarray(d["ac"]["freq"]).real
    Hsup = np.abs(np.asarray(d["ac"]["vout"]))

    # noise with the supply noise_table, and with a quiet supply
    d = _run_noise(head + f'Vsup (vin 0) vsource dc={vdd:g} noisefile="{tbl.resolve()}"\n'
                   f"nz (vout 0) noise values={vl}\n", "sn_on", tmp)
    fn = np.asarray(d["nz"]["freq"]).real
    out_with = np.asarray(d["nz"]["out"]).real
    d = _run_noise(head + f"Vsup (vin 0) vsource dc={vdd:g}\nnz (vout 0) noise values={vl}\n",
                   "sn_off", tmp)
    out_intr = np.asarray(d["nz"]["out"]).real

    supply_out = np.sqrt(np.clip(out_with**2 - out_intr**2, 0.0, None))
    in_asd = np.sqrt(_psd(fn))
    Hsup_i = np.interp(fn, fac, Hsup)
    predicted = in_asd * Hsup_i

    worst, dominant_ok = 0.0, False
    for f0, _, _ in SPURS:
        i = int(np.argmin(np.abs(fn - f0)))
        rel = abs(supply_out[i] - predicted[i]) / max(predicted[i], 1e-30)
        worst = max(worst, rel)
        # at the spur the supply-induced output must be a real, non-trivial contribution
        assert supply_out[i] > 1e-9, f"supply term vanished at {f0:g} Hz ({supply_out[i]:e})"
        if f0 == 2.0e6:
            dominant_ok = supply_out[i] > 0.3 * out_intr[i]   # rises toward / above the floor
    assert worst < 0.05, f"supply_out != input*PSRR: worst rel {worst:.2%} over the spurs"
    assert dominant_ok, "the 2 MHz supply spur did not rise above ~0.3x the intrinsic floor"

    # and the model's PSRR is finite/sane (a real transfer, not 0 or 1)
    i2 = int(np.argmin(np.abs(fac - 2.0e6)))
    psrr_db = -20 * np.log10(max(Hsup[i2], 1e-30))
    assert 0.0 < psrr_db < 80.0, f"implausible PSRR @2MHz: {psrr_db:.1f} dB"
    print(f"supply-noise propagation: worst rel {worst:.2%} over spurs; PSRR@2MHz {psrr_db:.1f} dB")


def test_coarse_sweep_misses_the_spur(model):
    """NEGATIVE CONTROL — the spur-sampling rule, locked: in ONE noise run that contains both
    a coarse log grid AND the f0=2 MHz bracket, the bracketed points reveal the spur while the
    neighbouring coarse points (kept >=20*HWHM away) sit at the floor. Drop the bracket from a
    real sweep and the sub-kHz spur is stepped over -- it 'doesn't simulate'."""
    va, tbl, op, vdd, _ = model
    tmp = tbl.parent
    f0, _, q = SPURS[0]                      # the 2 MHz spur
    h = f0 / (2.0 * q)

    # coarse log grid with a guard band: NO coarse point within 20*HWHM of the spur
    coarse = [x for x in np.logspace(np.log10(1e5), np.log10(1e7), 15)
              if abs(x - f0) > 20 * h]
    bracket = [f0 - h, f0, f0 + h]          # the mandatory on-spur samples
    fs = np.array(sorted(set(coarse) | set(bracket)))
    vl = _vlist(fs)
    head = (f'simulator lang=spectre\nahdl_include "{va.resolve()}"\n'
            f"Ild (vout 0) isource dc={op:.6e}\n"
            f"Xdut (vin vout 0) ldo_model iload={op:.6e} slew_en=0 vdd={vdd:g}\nsave vout\n")

    d = _run_noise(head + f'Vsup (vin 0) vsource dc={vdd:g} noisefile="{tbl.resolve()}"\n'
                   f"nz (vout 0) noise values={vl}\n", "sn_coarse_on", tmp)
    fn = np.asarray(d["nz"]["freq"]).real
    out_with = np.asarray(d["nz"]["out"]).real
    d = _run_noise(head + f"Vsup (vin 0) vsource dc={vdd:g}\nnz (vout 0) noise values={vl}\n",
                   "sn_coarse_off", tmp)
    out_intr = np.asarray(d["nz"]["out"]).real
    supply_out = np.sqrt(np.clip(out_with**2 - out_intr**2, 0.0, None))

    i_peak = int(np.argmin(np.abs(fn - f0)))                       # the bracketed peak
    peak = supply_out[i_peak]
    # coarse neighbours: the two coarse points straddling the spur
    below = [j for j, x in enumerate(fn) if x < f0 - 10 * h]
    above = [j for j, x in enumerate(fn) if x > f0 + 10 * h]
    neigh = max(supply_out[below[-1]], supply_out[above[0]])

    # the bracketed peak must tower over the coarse neighbours -> the spur lives ONLY at f0
    assert peak > 5.0 * neigh, (
        f"spur not resolved by the bracket: peak {peak:.2e} vs coarse-neighbour {neigh:.2e}")
    # and the input at f0 really is a spur (peak) vs floor at the neighbours
    assert np.sqrt(_psd(np.array([f0]))[0]) > 5.0 * np.sqrt(SW), "test spur too weak vs floor"
    print(f"coarse-sweep miss check: on-spur peak {peak*1e9:.1f} n vs coarse-neighbour "
          f"{neigh*1e9:.1f} n (ratio {peak/max(neigh,1e-30):.1f}x) -> spur lives only at f0")
