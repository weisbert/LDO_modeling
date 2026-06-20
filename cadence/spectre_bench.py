"""Canonical measurement bench, Spectre edition (mirror of harness/bench.py).

Same stimuli / same returned arrays as the ngspice bench, so a model fit to the
ngspice-derived references can be re-scored against Spectre, and a real LDO can be
characterized with the identical recipe. NET/PIN-parameterized from the start
(DutSpec carries the supply net + output net + how to place the DUT) so the 2-port
bench generalizes to Phase-4 in-situ extraction (point at any pins inside a top).

    measure_zout : 1 A AC into out, ideal supply  -> Zout(f)   [complex]
    measure_psrr : 1 V AC on supply               -> out/supply[complex]
    measure_noise: pnoise-style .noise at out     -> Sv(f)     [V/sqrt(Hz)]
    + load steps / dc regulation / dropout / spur, all matching bench.py.
"""
import sys
import pathlib
import numpy as np
_HARNESS = pathlib.Path(__file__).resolve().parents[1] / "harness"
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bench                      # reuse pure-numpy constants + helpers (no ngspice at import)
import spectre_run as sr

LOADS = bench.LOADS
VIN_DC = 1.05                     # nominal supply / PSRR reference (contract: model hardcodes 1.05)

# analysis statements (Spectre dec = points/decade; matches bench.py grids)
AC = "ac start=10 stop=100M dec=40"
AC_HF = "ac start=10 stop=500M dec=40"
NOISE = "noise start=10 stop=100M dec=20"


class DutSpec:
    """How to instantiate the DUT and where its 2 ports are.

    block(iload_A) -> spectre-lang text that DEFINES+PLACES the DUT instance
                      `Xdut` with external nets (supply, out). May include
                      `ahdl_include`/spice `.include` headers. iload lets the VA
                      model select its OP corner via the `iload` parameter.
    supply, out    -> net names the stimuli attach to (default vin / vout).
    """
    def __init__(self, block, supply="vin", out="vout", aux=()):
        self.block, self.supply, self.out, self.aux = block, supply, out, tuple(aux)


def va_dut(va_path, module="ldo_model", extra="slew_en=0", tbl=None):
    """DUT = a Verilog-A behavioral model (Phase 1 round-trip / emitted model scoring).

    `tbl` = path to the dropout table; the VA's `$table_model` hardcodes the name
    "ldo_model_dropout.tbl", so it is copied under that name into each run dir
    (only needed when extra contains slew_en=1 / large-signal steps)."""
    va_abs = str(pathlib.Path(va_path).resolve())     # ahdl_include resolves vs run dir
    def block(il):
        # the emitted model is 3-port `module ldo_model(vin, vout, gnd)`; tie gnd to 0.
        # (was 2-port `(vin vout)` -- stale since emit_va gained the explicit gnd port;
        # Spectre rejected it with "Xdut: Too few terminals given (2 < 3)").
        return (f'ahdl_include "{va_abs}"\n'
                f'Xdut (vin vout 0) {module} iload={il:g} {extra}\n')
    aux = [(str(pathlib.Path(tbl).resolve()), "ldo_model_dropout.tbl")] if tbl else []
    return DutSpec(block, aux=aux)


def spice_dut(model_files, subckt_files, subckt, xparams=""):
    """DUT = a SPICE subckt (Phase 2 transistor GT cross-sim).

    Two Spectre-spice gotchas handled here (both caught during Phase-2 bring-up):
      1. BSIM3 level map: ngspice uses `level=8` for BSIM3v3.3.0, but Spectre's
         spice reader maps 8 -> generic `mos8` (rejects BSIM3 params). Spectre's
         BSIM3v3 is `level=49`. We inline the model cards with 8->49 remapped, so
         the committed ngspice `.mod` ground-truth files stay untouched.
      2. Brace exprs: Spectre's spice reader does NOT substitute `{param}` subckt
         params (it errors `unknown parameter [param]`), but a BARE param name
         resolves fine (`r1 a b rr`). So we strip `{ident}` -> `ident` in the
         subckt body. (The GT uses only simple `{param}` refs; compound `{a*2}`
         exprs, if ever added, are left untouched and would need real handling.)
      3. Param scope: the DUT instance is emitted in SPICE lang (lowercase `xdut`)
         right after the includes, then we switch back to spectre lang for the
         stimuli; top-level nets vin/vout bridge the two languages.
    iload is ignored (GT load is set by Ild, not a model param)."""
    import re
    models = []
    for m in model_files:
        s = open(m).read()
        s = re.sub(r'([Ll]evel\s*=\s*)8\b', r'\g<1>49', s)   # BSIM3: ngspice 8 -> Spectre 49
        models.append(s)
    subs = []
    for p in subckt_files:
        s = open(p).read()
        s = re.sub(r'\{\s*([A-Za-z_]\w*)\s*\}', r'\1', s)    # {wp} -> wp (Spectre spice needs bare)
        subs.append(s)
    body = "\n".join(models) + "\n" + "\n".join(subs)

    def block(il):
        return ("simulator lang=spice\n" + body + "\n"
                f"xdut vin vout {subckt} {xparams}\n"
                "simulator lang=spectre\n")
    return DutSpec(block)


def measure_zout(dut, iload, accmd=AC, tag="z"):
    il = float(iload.replace("u", "e-6")) if isinstance(iload, str) else float(iload)
    scs = f"""// Zout: 1A AC into out, ideal supply
simulator lang=spectre
{dut.block(il)}
Vsup ({dut.supply} 0) vsource dc={VIN_DC} mag=0
Iac  (0 {dut.out})    isource mag=1
Ild  ({dut.out} 0)    isource dc={il:g}
acZ {accmd}
"""
    d = sr.run(scs, tag, aux=dut.aux)
    f = np.asarray(d["acZ"]["freq"]).real
    Z = np.asarray(d["acZ"][dut.out])           # 1A injected -> V(out) = Z
    return f, Z


def measure_psrr(dut, iload, accmd=AC, tag="p"):
    il = float(iload.replace("u", "e-6")) if isinstance(iload, str) else float(iload)
    scs = f"""// PSRR: 1V AC on supply
simulator lang=spectre
{dut.block(il)}
Vsup ({dut.supply} 0) vsource dc={VIN_DC} mag=1
Ild  ({dut.out} 0)    isource dc={il:g}
acP {accmd}
"""
    d = sr.run(scs, tag, aux=dut.aux)
    f = np.asarray(d["acP"]["freq"]).real
    H = np.asarray(d["acP"][dut.out]) / np.asarray(d["acP"][dut.supply])
    return f, H


def measure_noise(dut, iload, tag="n"):
    il = float(iload.replace("u", "e-6")) if isinstance(iload, str) else float(iload)
    scs = f"""// output noise PSD at out, ideal (noiseless) supply
simulator lang=spectre
{dut.block(il)}
Vsup ({dut.supply} 0) vsource dc={VIN_DC} mag=0
Ild  ({dut.out} 0)    isource dc={il:g}
nz ({dut.out} 0) {NOISE}
"""
    d = sr.run(scs, tag, aux=dut.aux)
    f = np.asarray(d["nz"]["freq"]).real
    Sv = np.asarray(d["nz"]["out"])             # Spectre gives V/sqrt(Hz) directly
    return f, Sv


def measure_loadstep(dut, dI, iload=bench.STEP_BASE, tag="t"):
    b = float(iload)
    T0, T1, DT, TST = bench.STEP_T0, bench.STEP_T1, bench.STEP_DT, bench.STEP_TSTOP
    wave = (f"0 {b:g} {T0:g} {b:g} {T0+1e-9:g} {b+dI:g} "
            f"{T1:g} {b+dI:g} {T1+1e-9:g} {b:g} {TST:g} {b:g}")
    scs = f"""// load step
simulator lang=spectre
{dut.block(b)}
Vsup ({dut.supply} 0) vsource dc={VIN_DC}
Ild  ({dut.out} 0)    isource type=pwl wave=[{wave}]
trn tran stop={TST:g} step={DT:g} maxstep={DT:g}
"""
    d = sr.run(scs, tag, aux=dut.aux)
    t = np.asarray(d["trn"]["time"]).real
    v = np.asarray(d["trn"][dut.out]).real
    tg = np.arange(0.0, TST, DT)
    return tg, np.interp(tg, t, v)


def _dc_sweep(dut, dev, param, start, stop, step, iload, tag):
    il = float(iload.replace("u", "e-6")) if isinstance(iload, str) else float(iload)
    extra = "" if dev == "Ild" else f"\nIld ({dut.out} 0) isource dc={il:g}"
    load = f"\nIld ({dut.out} 0) isource dc=1u" if dev == "Ild" else ""
    sup = f"Vsup ({dut.supply} 0) vsource dc={VIN_DC}"
    if dev == "Vsup":
        sweepdev = "Vsup"
    else:
        sweepdev = "Ild"
    scs = f"""// dc sweep {dev}.{param}
simulator lang=spectre
{dut.block(il)}
{sup}{load}{extra}
swp dc dev={sweepdev} param={param} start={start} stop={stop} step={step}
"""
    d = sr.run(scs, tag, aux=dut.aux)
    x = np.asarray(d["swp"][d["swp"]["_sweep"]]).real
    y = np.asarray(d["swp"][dut.out]).real
    return x, y


def measure_dc_loadreg(dut, istop=500e-6, istep=2e-6, tag="dcl"):
    return _dc_sweep(dut, "Ild", "dc", "1u", f"{istop:g}", f"{istep:g}", "121u", tag)


def measure_dc_linereg(dut, iload="121u", tag="dcv"):
    return _dc_sweep(dut, "Vsup", "dc", "0.9", "1.3", "0.01", iload, tag)


def measure_spur(dut, amp="500u", iload="121u", tag="s"):
    il = float(iload.replace("u", "e-6")) if isinstance(iload, str) else float(iload)
    a = float(amp.replace("u", "e-6")) if isinstance(amp, str) else float(amp)
    FT, DT, TST, TW = bench.FTONE, bench.DT, bench.TSTOP, bench.TWIN
    scs = f"""// spur / nonlinearity probe (pure {FT:g} Hz load tone)
simulator lang=spectre
{dut.block(il)}
Vsup ({dut.supply} 0) vsource dc={VIN_DC}
Ild  ({dut.out} 0)    isource dc={il:g}
Iton ({dut.out} 0)    isource type=sine sinedc=0 ampl={a:g} freq={FT:g}
trn tran stop={TST:g} step={DT:g} maxstep={DT:g}
"""
    d = sr.run(scs, tag, aux=dut.aux)
    t = np.asarray(d["trn"]["time"]).real
    v = np.asarray(d["trn"][dut.out]).real
    # uniform resample then coherent-window FFT (mirror bench.measure_spur)
    tg = np.arange(0.0, TST, DT)
    v = np.interp(tg, t, v)
    t = tg
    m = (t >= TW[0]) & (t < TW[1])
    t, v = t[m], v[m]
    nper = int(round(1 / (FT * DT)))
    n = (len(t) // nper) * nper
    v = v[:n]
    V = np.fft.rfft(v - v.mean()) * (2.0 / n)
    return np.fft.rfftfreq(n, DT), np.abs(V)
