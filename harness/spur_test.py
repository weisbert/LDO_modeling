"""Nonlinearity probe: inject a PURE single tone (8 MHz) load current into the
GT LDO and FFT the output. Harmonics at 16/24/32 MHz can ONLY come from the
LDO's nonlinearity (a pure tone in + LTI = pure tone out). This decides whether
the behavioral model must be nonlinear or an accurate linear multiport suffices.

Coherent sampling: dt=1ns -> 125 samples / 8MHz period (integer). FFT window
[24us,40us] = 128 periods after the high-Q 1.78MHz ring has decayed.
"""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import ng

FTONE = 8e6
DT = 1e-9
TSTOP = 40e-6
TWIN = (24e-6, 40e-6)        # 128 tone periods, ring decayed
RESULTS = ng.ROOT / "results"


def tb(amp, iload="121u"):
    return f"""* spur / nonlinearity testbench (pure {FTONE:g} Hz load tone)
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC {iload}
Itone vout 0 DC 0 SIN(0 {amp} {FTONE})
.control
set wr_singlescale
tran {DT} {TSTOP} 0
linearize v(vout)
wrdata trans.dat v(vout)
quit
.endc
.end
"""


def run_one(amp):
    r = ng.run(ng.assemble(tb(amp)), ng.ROOT / "work" / "spur",
               outputs=["trans.dat"])
    if r["trans.dat"] is None:
        raise RuntimeError("tran produced no data:\n" + r["_stderr"][-2000:])
    _, arr = r["trans.dat"]
    t, v = arr[:, 0], arr[:, 1]
    m = (t >= TWIN[0]) & (t < TWIN[1])
    t, v = t[m], v[m]
    # enforce exact integer number of samples = integer tone periods
    n_per = int(round(1 / (FTONE * DT)))         # 125
    nper_tot = len(t) // n_per
    n = nper_tot * n_per
    v = v[:n]
    V = np.fft.rfft(v - v.mean()) * (2.0 / n)    # single-sided amplitude
    f = np.fft.rfftfreq(n, DT)
    return f, np.abs(V)


def level_at(f, A, ftarget):
    i = np.argmin(np.abs(f - ftarget))
    return A[i]


if __name__ == "__main__":
    amps = ["20u", "50u", "100u", "200u", "500u"]
    print(f"{'amp':>6} | {'V@8M[mV]':>9} {'h2(16M)':>9} {'h3(24M)':>9} {'h4(32M)':>9}"
          f"   (dBc rel. fundamental)")
    spectra = {}
    for amp in amps:
        f, A = run_one(amp)
        spectra[amp] = (f, A)
        a1 = level_at(f, A, 8e6)
        a2, a3, a4 = (level_at(f, A, k) for k in (16e6, 24e6, 32e6))
        dbc = lambda x: 20 * np.log10(x / a1 + 1e-30)
        print(f"{amp:>6} | {a1*1e3:9.3f} {dbc(a2):9.1f} {dbc(a3):9.1f} {dbc(a4):9.1f}")

    # spectrum plot for largest drive
    f, A = spectra["500u"]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    band = f <= 60e6
    ax.semilogy(f[band] / 1e6, A[band] + 1e-12)
    for k, lab in [(8, "f0"), (16, "2f0"), (24, "3f0"), (32, "4f0")]:
        ax.axvline(k, color="r", ls=":", alpha=.4)
        ax.text(k, A.max(), lab, color="r", fontsize=8)
    ax.set(title="GT output spectrum, pure 8MHz load tone (amp=500u)",
           xlabel="MHz", ylabel="|V(vout)| (V)")
    ax.grid(True, which="both", alpha=.3)
    fig.tight_layout()
    fig.savefig(RESULTS / "gt_spur_spectrum.png", dpi=110)
    print(f"saved {RESULTS / 'gt_spur_spectrum.png'}")
