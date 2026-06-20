"""NON-CIRCULAR validation: drive the SAME spurry AVDD onto (a) the real transistor-level
GT LDO and (b) the behavioral model, then compare their OUTPUT noise.

The earlier inject_supply_noise.py check was self-referential: it confirmed the model's
noise output == input * the MODEL'S OWN measured PSRR -- a tautology about Spectre, not
evidence the model behaves like a real LDO. This compares the model against an INDEPENDENT
ground truth: the v2_capless transistor netlist (PMOS pass + 5T-OTA + feedback,
ground_truth/ldo_v2_capless.lib), run in the SAME Spectre engine with the SAME injected
supply noise. The match here is the HONEST number -- it is bounded by the PSRR fit quality,
not by simulator self-consistency.

Flow (all local Spectre 18.1, via cadence/spectre_bench.py DutSpecs):
  GT  = spice_dut(transistor netlist)      MD = behavioral VA (fit_model.emit_va)
  1. AC PSRR of each: H(f) = vout/vsupply  -> PSRR_gt vs PSRR_md (the real comparison).
  2. NOISE with the AVDD noise_table on the supply, and with a quiet supply, for EACH dut.
  3. quadrature-isolate the supply-induced output: sqrt(out_noisy^2 - out_quiet^2).
  4. compare GT vs MODEL supply-induced output at the 8 spurs (+ plot total/intrinsic).
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                       # avdd_spectrum
sys.path.insert(0, str(HERE.parent))                # cadence/  -> spectre_bench, spectre_run
sys.path.insert(0, str(HERE.parent.parent / "harness"))   # fit_model, ng

import spectre_bench as SB                                                  # noqa: E402
import spectre_run as sr                                                    # noqa: E402
import fit_model as FM                                                      # noqa: E402
import avdd_spectrum as AV                                                  # noqa: E402

ROOT = HERE.parent.parent
MODELS = [ROOT / "models" / "nmos_lv.mod", ROOT / "models" / "pmos_lv.mod"]
GT_LIB = ROOT / "ground_truth" / "ldo_v2_capless.lib"
VDD = SB.VIN_DC                                       # 1.05 (contract supply / PSRR ref)
ILOAD = 121e-6                                        # nominal load corner


def _write_noisefile(path):
    """AVDD1P0 noise PSD [V^2/Hz] over a dense grid (so the spur Lorentzians are represented)."""
    g = AV.build_grid()
    psd = AV.total(g) ** 2
    path.write_text("".join(f"{fi:.6e} {pi:.6e}\n" for fi, pi in zip(g, psd)))


def _supply_noise(dut, freqs, nf_path, tag):
    """output noise PSD at `out` with the supply either carrying nf_path or quiet."""
    nfattr = f' noisefile="{nf_path.resolve()}"' if nf_path else ""
    vl = "[" + " ".join(f"{x:.6e}" for x in freqs) + "]"
    scs = (f"// supply-noise: GT/model under injected AVDD\nsimulator lang=spectre\n"
           f"{dut.block(ILOAD)}"
           f"Vsup ({dut.supply} 0) vsource dc={VDD:g}{nfattr}\n"
           f"Ild  ({dut.out} 0)    isource dc={ILOAD:g}\n"
           f"nz ({dut.out} 0) noise values={vl}\n")
    d = sr.run(scs, tag, aux=dut.aux)
    return np.asarray(d["nz"]["freq"]).real, np.asarray(d["nz"]["out"]).real


def main():
    res = FM.fit_variant("v2_capless")
    va = HERE / "ldo_model.va"
    FM.emit_va(res.P, va, HERE / "ldo_model_dropout.tbl")
    nf = HERE / "avdd_nf.dat"
    _write_noisefile(nf)

    GT = SB.spice_dut(MODELS, [GT_LIB], "ldo_v2_capless")
    md_aux = [(str((HERE / "ldo_model_dropout.tbl").resolve()), "ldo_model_dropout.tbl")]

    def md_block(il):
        return (f'ahdl_include "{va.resolve()}"\n'
                f"Xdut (vin vout 0) ldo_model iload={il:g} slew_en=0 vdd={VDD:g}\n")
    MD = SB.DutSpec(md_block, aux=md_aux)

    # 1. PSRR transfer of each (smooth -> dec=40 ok, interpolate to spur freqs)
    fg, Hg = SB.measure_psrr(GT, ILOAD, tag="psrr_gt")
    fm, Hm = SB.measure_psrr(MD, ILOAD, tag="psrr_md")

    # 2-3. supply-noise injection (+ quiet) for each, quadrature-isolate supply part
    freqs = AV.analysis_freqs()
    fG, gON = _supply_noise(GT, freqs, nf, "gt_on")
    _, gOFF = _supply_noise(GT, freqs, None, "gt_off")
    fM, mON = _supply_noise(MD, freqs, nf, "md_on")
    _, mOFF = _supply_noise(MD, freqs, None, "md_off")
    gSup = np.sqrt(np.clip(gON**2 - gOFF**2, 0.0, None))
    mSup = np.sqrt(np.clip(mON**2 - mOFF**2, 0.0, None))

    # 4. compare at the spurs
    print(f"\n=== GT (transistor) vs MODEL: supply-induced output at the 8 spurs (load {ILOAD*1e6:g}uA) ===")
    print(f"{'spur':<12}{'freq':>9}{'PSRR_gt':>9}{'PSRR_md':>9}{'gt out':>10}{'md out':>10}{'rel':>8}")
    worst = 0.0
    for lab, f0, pk, q in AV.SPURS:
        i = int(np.argmin(np.abs(fG - f0)))
        pg = -20 * np.log10(max(np.interp(f0, fg, np.abs(Hg)), 1e-30))
        pm = -20 * np.log10(max(np.interp(f0, fm, np.abs(Hm)), 1e-30))
        rel = abs(mSup[i] - gSup[i]) / max(gSup[i], 1e-30)
        worst = max(worst, rel)
        fr = f"{f0/1e6:g}M" if f0 >= 1e6 else f"{f0/1e3:g}k"
        print(f"{lab:<12}{fr:>9}{pg:>8.1f}{pm:>9.1f}{gSup[i]*1e9:>8.1f}n{mSup[i]*1e9:>8.1f}n{rel:>8.1%}")
    print(f"\nworst GT-vs-MODEL supply-induced output rel over the spurs: {worst:.1%}")

    # plot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 5.2))
    a1.loglog(fG, gSup, lw=1.3, color="#1f4e8c", label="GT (transistor) supply-induced")
    a1.loglog(fM, mSup, lw=1.1, ls="--", color="#a02020", label="MODEL supply-induced")
    a1.loglog(fG, gON, lw=0.8, color="#1f4e8c", alpha=0.35, label="GT total")
    a1.loglog(fM, mON, lw=0.8, ls="--", color="#a02020", alpha=0.35, label="MODEL total")
    a1.set_xlim(1e4, 1e8)
    a1.set_xlabel("frequency [Hz]"); a1.set_ylabel("output noise [V/√Hz]")
    a1.set_title("supply-noise -> output: GT vs MODEL"); a1.grid(True, which="both", alpha=0.25)
    a1.legend(fontsize=7.5, loc="lower left")
    a2.semilogx(fg, -20 * np.log10(np.clip(np.abs(Hg), 1e-30, None)), lw=1.3,
                color="#1f4e8c", label="PSRR GT (transistor)")
    a2.semilogx(fm, -20 * np.log10(np.clip(np.abs(Hm), 1e-30, None)), lw=1.1,
                ls="--", color="#a02020", label="PSRR MODEL")
    for _, f0, _, _ in AV.SPURS:
        a2.axvline(f0, color="#cccccc", lw=0.6, zorder=0)
    a2.set_xlim(10, 1e8); a2.set_xlabel("frequency [Hz]"); a2.set_ylabel("PSRR [dB]")
    a2.set_title("PSRR: GT vs MODEL (spurs marked)"); a2.grid(True, which="both", alpha=0.25)
    a2.legend(fontsize=8)
    fig.tight_layout()
    png = HERE / "gt_vs_model_supply_noise.png"
    fig.savefig(png, dpi=130)
    print(f"wrote {png}")


if __name__ == "__main__":
    main()
