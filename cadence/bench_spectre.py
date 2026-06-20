"""Adapter: expose harness/bench.py's measurement API, but backed by Spectre.

score.py calls `bench.measure_zout(lib, subckt, il, xparams=...)` etc. and the
pure-numpy helpers (ring_freq, level_at, SPUR_BANDS, LIN_FRAC, STEP_DI). This
module presents that exact surface so `score.bench` can be monkeypatched to it,
letting the existing scorer grade an emitted Verilog-A model under Spectre 18.1
with zero metric drift (same formulas, same composite).

`lib` here is the candidate's .va path; `subckt` the module name (emitter uses
`ldo_model`). slew_en is honored from xparams (large-signal steps); the dropout
table `<stem>_dropout.tbl` is auto-attached when present.
"""
import pathlib
import bench as _b
import spectre_bench as sb

# pure-numpy passthroughs (no simulator)
SPUR_BANDS = _b.SPUR_BANDS
LIN_FRAC = _b.LIN_FRAC
STEP_DI = _b.STEP_DI
STEP_BASE = _b.STEP_BASE
ring_freq = _b.ring_freq
level_at = _b.level_at
SUPPLY_SPURS = _b.SUPPLY_SPURS
supply_spur_atten = _b.supply_spur_atten


def _slew(xparams):
    for t in (xparams or "").split():
        if t.startswith("slew_en="):
            return t
    return "slew_en=0"


def _dut(lib, subckt, xparams):
    p = pathlib.Path(lib)
    tbl = p.parent / (p.stem + "_dropout.tbl")
    return sb.va_dut(str(lib), module=subckt, extra=_slew(xparams),
                     tbl=str(tbl) if tbl.exists() else None)


def ac_hf_cmd(fmax=500e6):
    """HF AC sweep command, SPECTRE syntax (bench.ac_hf_cmd is ngspice syntax -> can't be
    re-exported; score._hf_metrics builds the command via bench.ac_hf_cmd and passes it to
    measure_zout/psrr, so the Spectre backend must produce a Spectre-lang command)."""
    return f"ac start=10 stop={fmax:g} dec=40"


def measure_zout(lib, subckt, iload, xparams="", accmd=None):
    return sb.measure_zout(_dut(lib, subckt, xparams), iload, accmd=accmd or sb.AC)


def measure_psrr(lib, subckt, iload, xparams="", accmd=None):
    return sb.measure_psrr(_dut(lib, subckt, xparams), iload, accmd=accmd or sb.AC)


def measure_noise(lib, subckt, iload, xparams=""):
    return sb.measure_noise(_dut(lib, subckt, xparams), iload)


def measure_loadstep(lib, subckt, dI, iload=STEP_BASE, xparams=""):
    return sb.measure_loadstep(_dut(lib, subckt, xparams), dI, iload=iload)


def measure_spur(lib, subckt, amp="500u", iload="121u", xparams=""):
    return sb.measure_spur(_dut(lib, subckt, xparams), amp=amp, iload=iload)
