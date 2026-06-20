"""SPECTRE-GATED, NON-CIRCULAR regression: the behavioral model reproduces a REAL
transistor-level LDO's supply-noise -> output behavior.

cadence/test_supply_noise_prop_spectre.py only checks the model against ITSELF (output ==
input * the model's own PSRR) -- a simulator tautology, not evidence the model behaves like
a real LDO. THIS test injects the SAME spurry supply onto BOTH:
  * GT = the v2_capless TRANSISTOR netlist (PMOS pass + 5T-OTA + feedback,
         ground_truth/ldo_v2_capless.lib), via spectre_bench.spice_dut, and
  * MD = the behavioral Verilog-A model (fit_model.emit_va), via a 3-port DutSpec,
both in the SAME local Spectre engine, then compares their supply-induced output noise.
The agreement is the HONEST number -- bounded by the PSRR fit quality, not self-consistency.

Measured locally (full 8-spur script gt_vs_model_supply_noise.py): typical 1-3%, worst
~12% at the 8 MHz harmonic that sits on the steep PSRR-notch recovery (PSRR there is only
~3 dB and hardest to fit). This test uses 2 spurs (2 MHz, 6 MHz) for speed and asserts the
GT-vs-model supply-induced output agrees to <10%, and the PSRR transfers track within ~2 dB.

SKIP cleanly when Spectre is absent (SPECTRE_HOME env-overridable guard):
    SPECTRE_HOME=/nonexistent python3 -m pytest cadence/test_supply_noise_gt_vs_model_spectre.py -q -> skipped

Run:  python3 -m pytest cadence/test_supply_noise_gt_vs_model_spectre.py -q
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
import spectre_bench as SB                                                  # noqa: E402
import fit_model as FM                                                      # noqa: E402

MODELS = [ROOT / "models" / "nmos_lv.mod", ROOT / "models" / "pmos_lv.mod"]
GT_LIB = ROOT / "ground_truth" / "ldo_v2_capless.lib"
VDD = SB.VIN_DC                                  # 1.05
ILOAD = 121e-6
SW = 1.0e-15                                     # flat floor PSD [V^2/Hz]
SPURS = [(2.0e6, 6.0e-12, 1200), (6.0e6, 1.5e-12, 1200)]   # (f0, peak PSD, Q)


def _have_spectre():
    home = os.environ.get("SPECTRE_HOME") or sr.SPECTRE_HOME
    return pathlib.Path(home).is_dir()


pytestmark = pytest.mark.skipif(not _have_spectre(), reason="spectre not available")


def _psd(f):
    s = np.full_like(np.asarray(f, float), SW)
    for f0, pk, q in SPURS:
        h = f0 / (2.0 * q)
        s = s + pk / (1.0 + ((np.asarray(f, float) - f0) / h) ** 2)
    return s


def _eval_freqs():
    # MANDATORY spur sampling: center + very-short steps (HWHM units) so the spur is resolved
    g = list(np.logspace(np.log10(1e3), np.log10(5e7), 70))
    for f0, _, q in SPURS:
        h = f0 / (2.0 * q)
        g.append(f0)
        for m in (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0):
            g += [f0 - m * h, f0 + m * h]
    return np.array(sorted(x for x in set(g) if 1e3 <= x <= 5e7))


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gt_vs_md")
    res = FM.fit_variant("v2_capless")
    va = tmp / "ldo_model.va"
    FM.emit_va(res.P, va, tmp / "ldo_model_dropout.tbl")
    # dense noisefile [V^2/Hz] so the Lorentzian spurs are represented in the PWL
    dense = np.unique(np.concatenate([
        np.logspace(np.log10(10.0), np.log10(1e8), 2000),
        np.concatenate([np.linspace(f0 - 8 * f0 / (2 * q), f0 + 8 * f0 / (2 * q), 200)
                        for f0, _, q in SPURS])]))
    dense = dense[(dense >= 10.0) & (dense <= 1e8)]
    nf = tmp / "nf.dat"
    nf.write_text("".join(f"{fi:.6e} {pi:.6e}\n" for fi, pi in zip(dense, _psd(dense))))

    GT = SB.spice_dut(MODELS, [GT_LIB], "ldo_v2_capless")
    md_aux = [(str((tmp / "ldo_model_dropout.tbl").resolve()), "ldo_model_dropout.tbl")]

    def md_block(il):
        return (f'ahdl_include "{va.resolve()}"\n'
                f"Xdut (vin vout 0) ldo_model iload={il:g} slew_en=0 vdd={VDD:g}\n")
    MD = SB.DutSpec(md_block, aux=md_aux)
    return tmp, GT, MD, nf


def _supply_noise(dut, freqs, nf_path, tag, tmp):
    nfattr = f' noisefile="{nf_path.resolve()}"' if nf_path else ""
    vl = "[" + " ".join(f"{x:.6e}" for x in freqs) + "]"
    scs = (f"// GT/model under injected supply noise\nsimulator lang=spectre\n"
           f"{dut.block(ILOAD)}"
           f"Vsup ({dut.supply} 0) vsource dc={VDD:g}{nfattr}\n"
           f"Ild  ({dut.out} 0)    isource dc={ILOAD:g}\n"
           f"nz ({dut.out} 0) noise values={vl}\n")
    saved = sr.WORK
    sr.WORK = tmp / f"sp_{tag}"
    try:
        d = sr.run(scs, tag, aux=dut.aux)
    finally:
        sr.WORK = saved
    return np.asarray(d["nz"]["freq"]).real, np.asarray(d["nz"]["out"]).real


def test_model_matches_transistor_gt_under_supply_noise(setup):
    tmp, GT, MD, nf = setup

    fg, Hg = SB.measure_psrr(GT, ILOAD, tag="psrr_gt")
    fm, Hm = SB.measure_psrr(MD, ILOAD, tag="psrr_md")

    freqs = _eval_freqs()
    fG, gON = _supply_noise(GT, freqs, nf, "gt_on", tmp)
    _, gOFF = _supply_noise(GT, freqs, None, "gt_off", tmp)
    fM, mON = _supply_noise(MD, freqs, nf, "md_on", tmp)
    _, mOFF = _supply_noise(MD, freqs, None, "md_off", tmp)
    gSup = np.sqrt(np.clip(gON**2 - gOFF**2, 0.0, None))
    mSup = np.sqrt(np.clip(mON**2 - mOFF**2, 0.0, None))

    worst = 0.0
    for f0, _, _ in SPURS:
        i = int(np.argmin(np.abs(fG - f0)))
        # the GT (real transistor) supply spur must be a real, non-trivial contribution
        assert gSup[i] > 1e-9, f"GT supply spur vanished at {f0:g} Hz ({gSup[i]:e})"
        rel = abs(mSup[i] - gSup[i]) / max(gSup[i], 1e-30)
        worst = max(worst, rel)
        # PSRR transfers track within ~2 dB at the spur
        pg = -20 * np.log10(max(np.interp(f0, fg, np.abs(Hg)), 1e-30))
        pm = -20 * np.log10(max(np.interp(f0, fm, np.abs(Hm)), 1e-30))
        assert abs(pg - pm) < 2.0, f"PSRR GT vs model off by {abs(pg-pm):.2f} dB @ {f0:g} Hz"
        print(f"  {f0/1e6:g}MHz: PSRR gt={pg:.1f} md={pm:.1f} dB | out gt={gSup[i]*1e9:.1f} "
              f"md={mSup[i]*1e9:.1f} nV/rtHz | rel {rel:.1%}")
    # honest end-to-end: the model reproduces the REAL LDO's supply-noise output to <10%
    assert worst < 0.10, f"model vs transistor-GT supply-noise output off by {worst:.1%} (>10%)"
    print(f"GT(transistor) vs MODEL supply-noise output: worst rel {worst:.1%} over spurs")
