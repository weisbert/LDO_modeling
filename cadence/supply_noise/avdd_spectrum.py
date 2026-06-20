"""AVDD1P0 supply-noise stimulus spectrum (alignment artifact).

A realistic 1.0 V analog-supply noise profile for testing whether the LDO model
propagates supply noise -> output (= input PSD shaped by |PSRR(f)|).

It is ONE curve = a broadband floor (white + 1/f) with 8 discrete spurs riding on
top (DC-DC switcher fundamental + harmonics, control-loop tone, ref-clock comb).

Outputs (this dir):
  avdd_noise_spectrum.png   the curve (V/rtHz vs freq, log-log), 8 spurs labelled
  avdd_noise_table.dat      freq, PSD[V^2/Hz] pairs -> Verilog-A noise_table input
  avdd_spurs.csv            the 8 spur tones (freq, V/rtHz, Q) for AC/PAC injection
"""
import numpy as np

HERE = __import__("pathlib").Path(__file__).resolve().parent

# ------------------------------------------------------------------ floor model
SW = 5.0e-8        # white floor [V/rtHz]  = 50 nV/rtHz
FC = 5.0e4         # 1/f corner [Hz]: below this the floor rises as 1/sqrt(f)


def floor(f):
    """Broadband supply-noise floor [V/rtHz]: white + 1/f, corner at FC."""
    return np.sqrt(SW**2 * (1.0 + FC / f))


# ------------------------------------------------------------------ spur comb
# (label, f0[Hz], peak[V/rtHz], Q)   -- Lorentzian bumps on the floor
SPURS = [
    ("PWM / loop",       1.0e5, 2.0e-6,  800),   # control-loop / audible spur
    ("DC-DC f0",         2.0e6, 8.0e-6, 1200),   # buck switcher fundamental (biggest)
    ("DC-DC 2f0",        4.0e6, 4.0e-6, 1200),
    ("DC-DC 3f0",        6.0e6, 2.0e-6, 1200),
    ("DC-DC 4f0",        8.0e6, 1.2e-6, 1200),
    ("ref clk",         1.92e7, 3.0e-6, 2000),   # 19.2 MHz reference
    ("2x ref",          3.84e7, 1.5e-6, 2000),
    ("4x ref",          7.68e7, 8.0e-7, 2000),
]


def spur_psd2(f):
    """Sum of spur power densities [V^2/Hz] (Lorentzian, HWHM = f0/(2Q))."""
    s2 = np.zeros_like(f)
    for _, f0, pk, q in SPURS:
        hwhm = f0 / (2.0 * q)
        s2 += pk**2 / (1.0 + ((f - f0) / hwhm) ** 2)
    return s2


def total(f):
    """Total supply-noise amplitude density [V/rtHz] = sqrt(floor^2 + spurs^2)."""
    return np.sqrt(floor(f) ** 2 + spur_psd2(f))


# ------------------------------------------------------------------ spur sampling RULE
# Multiples of the spur HWHM (=f0/(2Q)) to sample either side of a spur center. The tightest
# steps (0.05..0.25 HWHM) sit right on the peak; the wider ones trace the skirt.
SPUR_MULTS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0)


def spur_brackets(f0, q, mults=SPUR_MULTS):
    """Frequencies that BRACKET a spur for a Cadence `.noise` (or AC) sweep: the center
    f0 plus very-short-step points either side, in units of the HWHM = f0/(2Q).

    THIS IS MANDATORY, NOT cosmetic. `.noise` evaluates only AT the frequencies you
    request and reads the supply PSD by PWL-interpolating the noisefile there. A spur is
    sub-kHz wide (HWHM ~ f0/2Q), so a coarse log/dec sweep steps clean OVER it, samples the
    floor, and the spur simply does not appear in the result ("doesn't simulate"). Sampling
    f0 itself + tight (0.05..0.25 HWHM) steps captures the peak amplitude AND resolves the
    narrow shape. Q-aware: the bracket auto-narrows for higher-Q (narrower) spurs."""
    h = f0 / (2.0 * q)
    pts = [f0]
    for m in mults:
        pts += [f0 - m * h, f0 + m * h]
    return sorted(p for p in pts if p > 0)


def analysis_freqs(n_floor=160, fmin=10.0, fmax=1e8, spurs=None):
    """A full Cadence noise/AC sweep frequency list: a log floor grid PLUS each spur's
    mandatory bracket (see spur_brackets). Use this for `noise values=[...]` / `ac values=[...]`
    so every spur is actually sampled."""
    g = list(np.logspace(np.log10(fmin), np.log10(fmax), n_floor))
    for _, f0, _, q in (spurs or SPURS):
        g += spur_brackets(f0, q)
    return np.array(sorted(x for x in set(g) if fmin <= x <= fmax))


# ------------------------------------------------------------------ freq grid
# log grid + a fine linear cluster around each spur so the Lorentzians render clean. NOTE:
# this is for REPRESENTING the spectrum (plot + the noisefile PWL table), a different need
# from analysis_freqs() above which is for SAMPLING the spectrum in a Spectre sweep.
def build_grid():
    g = list(np.logspace(np.log10(10.0), np.log10(1e8), 4000))
    for _, f0, _, q in SPURS:
        hwhm = f0 / (2.0 * q)
        g += list(np.linspace(f0 - 8 * hwhm, f0 + 8 * hwhm, 400))
    return np.array(sorted(x for x in g if 10.0 <= x <= 1e8))


def main():
    f = build_grid()
    s = total(f)

    # ---- plot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.loglog(f, s, lw=1.0, color="#1f4e8c", label="AVDD1P0 noise (floor + spurs)")
    ax.loglog(f, floor(f), lw=1.0, ls="--", color="#999999", label="broadband floor")
    for lab, f0, pk, q in SPURS:
        peak = total(np.array([f0]))[0]
        ax.annotate(f"{lab}\n{f0/1e6:g}M" if f0 >= 1e6 else f"{lab}\n{f0/1e3:g}k",
                    xy=(f0, peak), xytext=(f0, peak * 1.6),
                    ha="center", va="bottom", fontsize=7.5, color="#a02020")
        ax.plot([f0], [peak], "o", ms=3, color="#a02020")
    ax.set_xlim(10, 1e8)
    ax.set_ylim(2e-8, 3e-5)
    ax.set_xlabel("frequency [Hz]")
    ax.set_ylabel("supply-noise amplitude density [V/$\\sqrt{\\mathrm{Hz}}$]")
    ax.set_title("AVDD1P0 (1.0 V) supply-noise stimulus — broadband floor + 8 spurs")
    ax.grid(True, which="both", alpha=0.25)
    ax.legend(loc="upper right", fontsize=8)
    # secondary axis in nV/rtHz tick labels
    fig.tight_layout()
    png = HERE / "avdd_noise_spectrum.png"
    fig.savefig(png, dpi=130)
    print(f"wrote {png}")

    # ---- noise_table data file (freq, PSD V^2/Hz) for Verilog-A noise_table()
    tbl = HERE / "avdd_noise_table.dat"
    psd = s**2
    with open(tbl, "w") as fh:
        fh.write("# AVDD1P0 supply noise PSD  freq[Hz]  PSD[V^2/Hz]\n")
        for fi, pi in zip(f, psd):
            fh.write(f"{fi:.6e} {pi:.6e}\n")
    print(f"wrote {tbl}  ({len(f)} pts)")

    # ---- spur list (deterministic tones)
    csv = HERE / "avdd_spurs.csv"
    with open(csv, "w") as fh:
        fh.write("# label,f0_Hz,amp_Vrt,Q,floor_Vrt,ratio_dB\n")
        for lab, f0, pk, q in SPURS:
            fl = floor(np.array([f0]))[0]
            fh.write(f"{lab},{f0:.6e},{pk:.3e},{q},{fl:.3e},{20*np.log10(pk/fl):.1f}\n")
    print(f"wrote {csv}")

    # ---- console spec
    print("\n=== AVDD1P0 supply-noise spec (alignment) ===")
    print(f"floor: white {SW*1e9:.0f} nV/rtHz, 1/f corner {FC/1e3:.0f} kHz "
          f"(LF rises as 1/sqrt(f); ~{floor(np.array([10.0]))[0]*1e6:.1f} uV/rtHz @ 10 Hz)")
    print(f"{'spur':<12}{'freq':>10}{'peak V/rtHz':>14}{'over floor':>12}{'Q':>7}")
    for lab, f0, pk, q in SPURS:
        fl = floor(np.array([f0]))[0]
        fr = f"{f0/1e6:g} MHz" if f0 >= 1e6 else f"{f0/1e3:g} kHz"
        print(f"{lab:<12}{fr:>10}{pk*1e6:>12.2f}u{20*np.log10(pk/fl):>10.0f} dB{q:>7}")


if __name__ == "__main__":
    main()
