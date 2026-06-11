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

# HF (*_hf) characterization ceiling. This is a characterization-RECIPE parameter, not a model
# constant: it bounds Zout/PSRR up to the supplied circuit's carrier (see modeling-bandwidth notes
# -- *_hf cutoff != system max freq). Default 500 MHz brackets the Target-A ~304 MHz carrier; a real
# GHz part (e.g. Target B ~5.8 GHz) overrides it per-variant via variants[..]["hf_stop"]. Decide the
# real ceiling from an exploratory 6-10 GHz sweep (don't default to 500 MHz).
HF_STOP = 500e6      # default *_hf ceiling [Hz]


def _fmt_hz(f):
    """Format a frequency for an ngspice .control sweep as an SI-suffixed literal
    (ngspice: g=1e9, meg=1e6, k=1e3). Keeps existing decks byte-identical
    (_fmt_hz(500e6) == '500meg') and reads cleanly at GHz (_fmt_hz(10e9) == '10g')."""
    f = float(f)
    if f >= 1e9:
        return f"{f / 1e9:g}g"
    if f >= 1e6:
        return f"{f / 1e6:g}meg"
    if f >= 1e3:
        return f"{f / 1e3:g}k"
    return f"{f:g}"


def ac_hf_cmd(fstop=None, dec=40, fstart=10):
    """Wideband AC sweep command up to the *_hf ceiling (`fstop`, default HF_STOP)."""
    return f"ac dec {dec} {fstart} {_fmt_hz(HF_STOP if fstop is None else fstop)}"


AC_HF = ac_hf_cmd()   # back-compat default == "ac dec 40 10 500meg"


def _run(tb, lib, tag, out="out.dat"):
    libs = list(lib) if isinstance(lib, (list, tuple)) else [lib]
    r = ng.run(ng.assemble(tb, libs=libs), WORK / tag, outputs=[out])
    if r[out] is None:
        raise RuntimeError(f"{tag}: ngspice produced no data:\n{r['_stderr'][-1800:]}")
    return r[out][1]


_PORTS_CACHE = {}


def subckt_ports(lib, subckt):
    """Port count of `.subckt <subckt> ...` in the lib file(s): node tokens before the
    first param=value token. The emitted model gained an explicit gnd port (R2) while
    the GT subckts remain 2-port -- one bench serves both via this detection.
    Defaults to 2 when the definition is not found (legacy behavior)."""
    import re
    libs = tuple(str(p) for p in (lib if isinstance(lib, (list, tuple)) else [lib]))
    key = None
    try:
        key = (libs, subckt.lower(),
               tuple(os.path.getmtime(p) for p in libs if os.path.exists(p)))
        if key in _PORTS_CACHE:
            return _PORTS_CACHE[key]
    except OSError:
        pass
    n = 2
    pat = re.compile(rf"^\s*\.subckt\s+{re.escape(subckt)}\s+(.*)$", re.IGNORECASE)
    for p in libs:
        try:
            for line in open(p, encoding="utf-8", errors="replace"):
                m = pat.match(line)
                if m:
                    toks = m.group(1).split()
                    n = sum(1 for t in toks if "=" not in t)
                    break
            else:
                continue
            break
        except OSError:
            continue
    if key is not None:
        _PORTS_CACHE[key] = n
    return n


def xline(lib, subckt, xparams="", inst="Xdut"):
    """DUT instantiation line for the standard vin/vout bench. A 3-port DUT (the
    emitted model's explicit gnd, R2) gets its ground tied to node 0."""
    gndtie = " 0" if subckt_ports(lib, subckt) >= 3 else ""
    return f"{inst} vin vout{gndtie} {subckt} {xparams}"


def measure_zout(lib, subckt, iload, xparams="", accmd=AC):
    tb = f"""* Zout
{xline(lib, subckt, xparams)}
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
{xline(lib, subckt, xparams)}
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
{xline(lib, subckt, xparams)}
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
{xline(lib, subckt, xparams)}
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
{xline(lib, subckt, xparams)}
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
{xline(lib, subckt, xparams)}
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
{xline(lib, subckt, xparams)}
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
