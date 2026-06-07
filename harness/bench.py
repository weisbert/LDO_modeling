"""Canonical measurement bench, parameterized by DUT (subckt name + lib file).

Same stimuli are applied to the ground-truth LDO and to any candidate model,
so model-vs-GT comparison is apples-to-apples. This is the core of the
feedback loop: GT defines the target, models are scored against it.

    measure_zout : 1 A AC injected into vout -> Zout(f)            [complex]
    measure_psrr : 1 V AC on vin -> vout/vin                       [complex]
    measure_spur : pure 8 MHz load tone -> output amplitude spectrum
"""
import os
import numpy as np
import ng

WORK = ng.ROOT / os.environ.get("LDO_WORK", "work")   # per-process workdir for safe parallel runs
LOADS = ["20u", "121u", "250u"]      # op-point corners (nominal 121u)
SPUR_BANDS = [8e6, 16e6, 24e6]       # user's spur offsets of interest
AC = "ac dec 40 10 100meg"

# --- nonlinearity / spur probe params (coherent sampling) ---
FTONE, DT, TSTOP, TWIN = 8e6, 1e-9, 40e-6, (24e-6, 40e-6)

# --- transient load-step probe (req#1) ---
STEP_BASE = 121e-6
STEP_DT, STEP_TSTOP, STEP_T0, STEP_T1 = 2e-9, 25e-6, 5e-6, 15e-6
STEP_DI = {"lin": 50e-6, "big": 1e-3, "slew": 5e-3}   # linear / compression / slew
LIN_FRAC = 0.3        # linear-test step = LIN_FRAC*bias (small perturbation at every corner)
# --- noise probe (req#2) + HF AC extension ---
NOISE_CMD = "noise v(vout) Vin dec 20 10 100meg"
AC_HF = "ac dec 40 10 500meg"


def _run(tb, lib, tag, out="out.dat"):
    libs = list(lib) if isinstance(lib, (list, tuple)) else [lib]
    r = ng.run(ng.assemble(tb, libs=libs), WORK / tag, outputs=[out])
    if r[out] is None:
        raise RuntimeError(f"{tag}: ngspice produced no data:\n{r['_stderr'][-1800:]}")
    return r[out][1]


def measure_zout(lib, subckt, iload, xparams="", accmd=AC):
    tb = f"""* Zout
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05
Iload vout 0 DC {iload} AC 0
Iac 0 vout AC 1
.control
set wr_singlescale
{accmd}
wrdata out.dat vr(vout) vi(vout)
quit
.endc
.end
"""
    return ng.complex_col(_run(tb, lib, "z"))


def measure_psrr(lib, subckt, iload, xparams="", accmd=AC):
    tb = f"""* PSRR
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05 AC 1
Iload vout 0 DC {iload}
.control
set wr_singlescale
{accmd}
wrdata out.dat vr(vout) vi(vout)
quit
.endc
.end
"""
    return ng.complex_col(_run(tb, lib, "p"))


def measure_noise(lib, subckt, iload, xparams=""):
    """Output noise PSD at vout via .noise. Returns (f, Sv[V/sqrt(Hz)]). The
    intrinsic LDO self-noise (Vin is an ideal DC source -> contributes no noise),
    so this is exactly the target a Norton output-noise source must reproduce."""
    tb = f"""* output noise PSD
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05 AC 1
Iload vout 0 DC {iload}
.control
set wr_singlescale
{NOISE_CMD}
setplot noise1
wrdata out.dat onoise_spectrum
quit
.endc
.end
"""
    arr = _run(tb, lib, "n")
    return arr[:, 0], arr[:, 1]


def measure_loadstep(lib, subckt, dI, iload=STEP_BASE, xparams=""):
    """Load step iload -> iload+dI (1 ns edge) and back. Returns (t, v) resampled
    onto a uniform STEP_DT grid so GT and model are directly comparable."""
    b = iload
    tb = f"""* load step
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {b:g} {STEP_T0:g} {b:g} {STEP_T0+1e-9:g} {b+dI:g} {STEP_T1:g} {b+dI:g} {STEP_T1+1e-9:g} {b:g} {STEP_TSTOP:g} {b:g})
.control
set wr_singlescale
tran {STEP_DT} {STEP_TSTOP} 0 {STEP_DT}
wrdata out.dat v(vout)
quit
.endc
.end
"""
    arr = _run(tb, lib, "t")
    t, v = arr[:, 0], arr[:, 1]
    tg = np.arange(0.0, STEP_TSTOP, STEP_DT)
    return tg, np.interp(tg, t, v)


def measure_dc_loadreg(lib, subckt, xparams="", istop="500u", istep="2u"):
    tb = f"""* dc load reg
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05
Iload vout 0 DC 1u
.control
set wr_singlescale
dc Iload 1u {istop} {istep}
wrdata out.dat v(vout)
quit
.endc
.end
"""
    arr = _run(tb, lib, "dc")
    return arr[:, 0], arr[:, 1]


def measure_dc_linereg(lib, subckt, iload="121u", xparams=""):
    tb = f"""* dc line reg
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05
Iload vout 0 DC {iload}
.control
set wr_singlescale
dc Vin 0.9 1.3 0.01
wrdata out.dat v(vout)
quit
.endc
.end
"""
    arr = _run(tb, lib, "dc")
    return arr[:, 0], arr[:, 1]


def ring_freq(t, v, t0=STEP_T0, win=3e-6, fmin=3e5):
    """Edge-ring frequency: FFT a short window right after the step-up edge and
    pick the dominant peak ABOVE fmin (excludes the slow pulse envelope)."""
    m = (t >= t0 + 2e-9) & (t < t0 + win)
    n = int(m.sum())
    if n < 16:
        return float("nan")
    w = v[m] - v[m].mean()
    F = np.abs(np.fft.rfft(w))
    fr = np.fft.rfftfreq(n, t[1] - t[0])
    sel = fr > fmin
    return float(fr[sel][np.argmax(F[sel])]) if sel.any() else float("nan")


def measure_spur(lib, subckt, amp="500u", iload="121u", xparams=""):
    tb = f"""* spur / nonlinearity probe (pure {FTONE:g} Hz load tone)
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05
Iload vout 0 DC {iload}
Itone vout 0 DC 0 SIN(0 {amp} {FTONE})
.control
set wr_singlescale
tran {DT} {TSTOP} 0
linearize v(vout)
wrdata out.dat v(vout)
quit
.endc
.end
"""
    arr = _run(tb, lib, "s")
    t, v = arr[:, 0], arr[:, 1]
    m = (t >= TWIN[0]) & (t < TWIN[1])
    t, v = t[m], v[m]
    nper = int(round(1 / (FTONE * DT)))
    n = (len(t) // nper) * nper
    v = v[:n]
    V = np.fft.rfft(v - v.mean()) * (2.0 / n)
    return np.fft.rfftfreq(n, DT), np.abs(V)


def level_at(f, A, ftarget):
    return float(A[np.argmin(np.abs(f - ftarget))])
