"""Emit the BEHAVIORAL current-source model as a ngspice subckt -- the offline
twin of the Cadence-side Verilog-A `_current_block` (harness/emit_pmu_model.py).
Same large-signal math, so what validates here transfers to the VA emit.

  .subckt isrc_model_<name> vdd out          (global 0 = gnd)
    I_pin = ( Idc(temper) + g0*(V(out)-vc) + gdd*(V(vdd)-Vdd0) ) * GATE(V(out))
            Idc(temper) = idc55 + didt*(temper-55)      [G1/G2]
            GATE = tanh( (V(out)/vk)^kp )      sink      [G5 compliance knee]
                 = tanh( ((Vdd0-V(out))/vk)^kp ) source
    + Cpar (output cap, the Y=g0+sCp imaginary part)    [G7]
  sink   -> B-source out->0   (draws I_pin from out)
  source -> B-source vdd->out (injects I_pin into out)
"""
import pathlib

from fit_isrc import VDD0


def emit_isrc(p):
    """p = fit_isrc.fit_isrc(...) dict -> ngspice subckt text."""
    name, pol = p["name"], p["pol"]
    vk, kp, vc = p["vknee"], p["knee_p"], p["vc"]
    g0, gdd, idc55, didt = p["g0"], p["gdd"], p["idc55"], p["didt"]
    d2 = p.get("d2", 0.0)                                 # 2nd-order Idc(T) curvature [A/degC^2]
    cp = max(p["cp"], 1e-18)
    # sqrt-floored base -> the gate Jacobian kp*arg^(kp-1) stays finite at arg=0
    # (a bare (V/vk)**kp blows up the OP solver when kp<1 and the sweep hits 0).
    # DRIVE follows pol; the compliance KNEE follows the data-detected side (a sink can have a
    # high-side ceiling knee -- the real WuR refs; a flat reference has no knee in range). See
    # fit_isrc.gate / _detect_knee. vhi is the FITTED ceiling (not assumed = VDD0).
    side = p.get("knee_side") or ("lo" if pol == "sink" else "hi")
    vhi = float(p.get("vhi", VDD0))
    drive = "out 0" if pol == "sink" else "vdd out"
    if side == "none":
        gate_factor = "1"
    elif side == "hi":
        garg = f"(sqrt(({vhi:g}-V(out))*({vhi:g}-V(out))+1e-12)/{vk:.6g})"
        gate_factor = f"tanh({garg}**{kp:.6g})"
    else:                                                 # 'lo'
        garg = f"(sqrt(V(out)*V(out)+1e-12)/{vk:.6g})"
        gate_factor = f"tanh({garg}**{kp:.6g})"
    # gdd was fit from the measured probe transfer dI(vout)/dVdd. For a sink the
    # probe reads i(vout) = -I_pin, so the coefficient inside I_pin is -gdd; for a
    # source (B drives vdd->out) i(vout) = +I_pin, so it is +gdd.
    gdd_eff = -gdd if pol == "sink" else gdd
    idc_t = f"({idc55:.6e}+({didt:.6e})*(temper-55))"      # linear; d2 tail appended only if != 0
    if d2 != 0.0:
        idc_t = (f"({idc55:.6e}+({didt:.6e})*(temper-55)"
                 f"+({d2:.6e})*(temper-55)*(temper-55))")
    core = (f"({idc_t}+({g0:.6e})*(V(out)-{vc:g})"
            f"+({gdd_eff:.6e})*(V(vdd)-{VDD0:g}))")
    iexpr = f"{core}*{gate_factor}"
    return (f".subckt isrc_model_{name} vdd out\n"
            f"Bout {drive} I = {iexpr}\n"
            f"Cpar out 0 {cp:.6g}\n"
            f".ends isrc_model_{name}\n")


def emit_all(params, path):
    """Write a combined .lib of all model subckts."""
    txt = ("* behavioral current-source models (emitted from MOS-GT fits)\n"
           "* offline twin of the Cadence Verilog-A _current_block\n")
    txt += "\n".join(emit_isrc(p) for p in params)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(txt)
    return path


if __name__ == "__main__":
    import sys
    from fit_isrc import fit_isrc, HERE
    from isrc_variants import VARIANTS
    WORK = HERE.parent / "work_isrc"
    params = [fit_isrc(WORK / f"{n}.npz") for n in VARIANTS]
    out = emit_all(params, WORK / "models" / "isrc_models.lib")
    print(f"wrote {out}")
    print(emit_isrc(params[0]))           # show one
