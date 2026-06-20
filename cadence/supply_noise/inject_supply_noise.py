"""Inject the AVDD1P0 supply-noise spectrum into the LDO model and verify it propagates
to the output, shaped by the model's PSRR.

Flow (all in LOCAL Spectre 18.1 on the emitted Verilog-A model):
  1. fit v2_capless -> emit ldo_model.va.
  2. AC: drive supply vin with mag=1 -> Hsup(f) = |vout| = the model's supply->output
     transfer (PSRR_dB = -20log10|Hsup|).
  3. NOISE with the full AVDD1P0 noise_table on vin -> total output noise (supply + intrinsic).
  4. NOISE with a quiet supply -> intrinsic model noise only.
  5. supply-induced output = sqrt(out_with^2 - out_intrinsic^2)  (quadrature isolation).
  6. cross-check: supply-induced output(f) == input_ASD(f) * Hsup(f)  at the 8 spur freqs.
  7. plot: input spectrum, |PSRR|, output spectrum (the spurs survive at vout, attenuated).
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                  # avdd_spectrum
sys.path.insert(0, str(HERE.parent))           # cadence/  -> spectre_run
sys.path.insert(0, str(HERE.parent.parent / "harness"))   # fit_model

import spectre_run as sr                                                    # noqa: E402
import fit_model as FM                                                      # noqa: E402
import avdd_spectrum as AV                                                  # noqa: E402

VARIANT = "v2_capless"
TBL = HERE / "avdd_noise_table.dat"            # V^2/Hz, written by avdd_spectrum.py


# ----------------------------------------------------------------- freq eval list
def eval_freqs():
    """Log floor grid + a small cluster at each spur so the narrow Lorentzians are sampled."""
    g = list(np.logspace(np.log10(10.0), np.log10(1e8), 160))
    for _, f0, _, q in AV.SPURS:
        h = f0 / (2.0 * q)
        g += [f0 - 2 * h, f0 - h, f0, f0 + h, f0 + 2 * h]
    return np.array(sorted(x for x in g if 10.0 <= x <= 1e8))


def _vlist(fs):
    return "[" + " ".join(f"{x:.6e}" for x in fs) + "]"


def main():
    res = FM.fit_variant(VARIANT)
    va = HERE / "ldo_model.va"
    FM.emit_va(res.P, va, HERE / "ldo_model_dropout.tbl")
    vdd = float(res.vref)
    op = FM._amps(res.nominal)
    print(f"fit {VARIANT}: vdd={vdd:g} op_load={op:.3e} A")

    fs = eval_freqs()
    vl = _vlist(fs)
    head = (f'simulator lang=spectre\nahdl_include "{va.resolve()}"\n'
            f"Ild (vout 0) isource dc={op:.6e}\n"
            f"Xdut (vin vout 0) ldo_model iload={op:.6e} slew_en=0 vdd={vdd:g}\nsave vout\n")

    # 2. AC supply->output transfer
    ac = head + f"Vsup (vin 0) vsource dc={vdd:g} mag=1\nac ac values={vl}\n"
    d = sr.run(ac, "sn_ac")
    fac = np.asarray(d["ac"]["freq"]).real
    Hsup = np.abs(np.asarray(d["ac"]["vout"]))           # |vout|/1  (supply->out gain)

    # 3. noise WITH the AVDD noise_table on the supply
    nz = (head + f'Vsup (vin 0) vsource dc={vdd:g} noisefile="{TBL.resolve()}"\n'
          f"nz (vout 0) noise values={vl}\n")
    d = sr.run(nz, "sn_nz_on")
    fn = np.asarray(d["nz"]["freq"]).real
    out_with = np.asarray(d["nz"]["out"]).real

    # 4. noise with a QUIET supply (intrinsic model noise only)
    nz0 = (head + f"Vsup (vin 0) vsource dc={vdd:g}\nnz (vout 0) noise values={vl}\n")
    d = sr.run(nz0, "sn_nz_off")
    out_intr = np.asarray(d["nz"]["out"]).real

    # 5. quadrature-isolate the supply-induced output noise
    supply_out = np.sqrt(np.clip(out_with**2 - out_intr**2, 0.0, None))

    # 6. predicted from input ASD * |Hsup|
    in_asd = AV.total(fn)                                  # V/rtHz at the eval freqs
    Hsup_i = np.interp(fn, fac, Hsup)                      # align AC grid to noise grid
    predicted = in_asd * Hsup_i

    print("\n=== supply-noise propagation at the 8 spurs ===")
    print(f"{'spur':<12}{'freq':>10}{'PSRR dB':>9}{'in V/rt':>11}{'out(sim)':>11}{'out(pred)':>11}{'rel':>8}")
    rows = []
    for lab, f0, pk, q in AV.SPURS:
        i = int(np.argmin(np.abs(fn - f0)))
        psrr_db = -20 * np.log10(max(Hsup_i[i], 1e-30))
        so, pr = supply_out[i], predicted[i]
        rel = abs(so - pr) / max(pr, 1e-30)
        rows.append((lab, f0, psrr_db, in_asd[i], so, pr, rel))
        fr = f"{f0/1e6:g}M" if f0 >= 1e6 else f"{f0/1e3:g}k"
        print(f"{lab:<12}{fr:>10}{psrr_db:>9.1f}{in_asd[i]*1e6:>9.2f}u"
              f"{so*1e9:>9.2f}n{pr*1e9:>9.2f}n{rel:>8.2%}")
    worst = max(r[6] for r in rows)
    print(f"\nworst sim-vs-predicted rel over the spurs: {worst:.2%}")

    # 7. plot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.loglog(fn, in_asd, lw=1.1, color="#1f4e8c", label="AVDD1P0 input  [V/√Hz]")
    ax.loglog(fn, out_with, lw=1.2, color="#a02020", label="vout total noise (sim)")
    ax.loglog(fn, supply_out, lw=1.0, ls="--", color="#d06000",
              label="vout supply-induced (isolated)")
    ax.loglog(fn, out_intr, lw=0.9, ls=":", color="#888888", label="vout intrinsic noise")
    ax.set_xlim(10, 1e8)
    ax.set_xlabel("frequency [Hz]")
    ax.set_ylabel("noise amplitude density [V/√Hz]")
    ax.set_title(f"AVDD1P0 supply noise -> LDO model output ({VARIANT}, Spectre VA)")
    ax.grid(True, which="both", alpha=0.25)
    ax.legend(loc="lower left", fontsize=8)
    ax2 = ax.twinx()
    ax2.semilogx(fac, -20 * np.log10(np.clip(Hsup, 1e-30, None)),
                 lw=0.9, color="#2a7a2a", alpha=0.8)
    ax2.set_ylabel("PSRR [dB]  (green)", color="#2a7a2a")
    ax2.tick_params(axis="y", colors="#2a7a2a")
    fig.tight_layout()
    png = HERE / "supply_noise_propagation.png"
    fig.savefig(png, dpi=130)
    print(f"wrote {png}")


if __name__ == "__main__":
    main()
