"""In-situ pin-level extraction of the stand-in PMU_top (multi-output / multi-supply).

PMU_top is instantiated as a BLACK-BOX subckt (its guts -- vref_bias + the two LDO
cores -- are reached ONLY through its external pins, exactly like the real in-situ
case). EN/CTRL dummies tie to 0. We apply the CADENCE_EXTRACTION contract stimuli at
the LDO output pins and idealize the supply AC, then write the GENERALIZED contract:

  voltage out <o> in {pll,vco}, supply <s> in {1p0,1p8}:
    z_<o>          Zout      (1 A AC into the out pin, both supplies ideal)
    p_<o>_<s>      PSRR      (1 V AC on supply <s>, others ideal) -> Vout/Vsup complex
    noise_<o>      out noise (supplies ideal/noiseless)
  current sink <c> in {i500n,i1u}:
    y_<c>          admittance (1 V AC on the pin, read its current)
    pi_<c>_<s>     current-PSRR (1 V AC on supply, read sink current)
  shared-ref coupling (validates the shared VREF/IBIAS we built in):
    couple_<a>_to_<b>   inject 1 A at out <a>, read V at out <b>

This is the CLI rehearsal; on the company machine the same analyses run in the
Test_PMU ADE and export PSF -> import_cadence -> npz. The PMU_top subckt here is the
one Test_PMU netlisted.
"""
import os
import pathlib
import numpy as np

os.environ.setdefault("LDO_SPECTRE_WORK", "work_pmu")
import sys
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import spectre_run as sr                                            # noqa: E402

VA = [f"/home/yusheng/cadence_work/Test/workarea/sim_yusheng/{c}/veriloga/veriloga.va"
      for c in ("vref_bias", "ldo_core_pll", "ldo_core_vco")]
OUTNET = {"pll": "VDD0P8_PLL", "vco": "VDD0P8_VCO"}
SUPNET = {"1p0": "VDD1P0", "1p8": "VDD1P8"}
SINKNET = {"i500n": "IBIAS_500N", "i1u": "IBIAS_1U"}
SINKSRC = {"i500n": "Vb500", "i1u": "Vb1u"}
AC = "ac start=10 stop=500M dec=20"
NOISE = "noise start=10 stop=100M dec=20"

_SUBCKT = "\n".join(f'ahdl_include "{p}"' for p in VA) + """
subckt PMU_top BIAS_EN IBIAS_1U IBIAS_500N PLLC3 PLLC2 PLLC1 PLLC0 PLL_EN \\
        TC3 TC2 TC1 TC0 VCOC3 VCOC2 VCOC1 VCOC0 VCO_EN \\
        VDD0P8_PLL VDD0P8_VCO VDD1P0 VDD1P8 VSS
    X_REF (VDD1P0 VDD1P8 VSS VREF IBIAS IBIAS_500N IBIAS_1U) vref_bias
    X_PLL (VDD1P0 VSS VREF IBIAS VDD0P8_PLL) ldo_core_pll
    X_VCO (VDD1P0 VSS VREF IBIAS VDD0P8_VCO) ldo_core_vco
ends PMU_top
"""


def _common(m0=0, m8=0, mb500=0, mb1u=0, iload=500e-6):
    """Full DUT context: PMU_top instanced (EN/CTRL -> 0), both supplies (ideal DC
    unless their mag is set), nominal output loads, sink pins held at a DC bias
    (ideal unless their mag is set). Returns the scs text up to (not incl.) the analysis."""
    return f"""simulator lang=spectre
{_SUBCKT}
Xdut (0 IBIAS_1U IBIAS_500N 0 0 0 0 0 0 0 0 0 0 0 0 0 0 \\
      VDD0P8_PLL VDD0P8_VCO VDD1P0 VDD1P8 0) PMU_top
Vd0  (VDD1P0 0)      vsource dc=1.05 mag={m0:g}
Vd8  (VDD1P8 0)      vsource dc=1.8  mag={m8:g}
Ipll (VDD0P8_PLL 0)  isource dc={iload:g}
Ivco (VDD0P8_VCO 0)  isource dc={iload:g}
Vb500 (IBIAS_500N 0) vsource dc=0.4 mag={mb500:g}
Vb1u  (IBIAS_1U 0)   vsource dc=0.4 mag={mb1u:g}
saveOptions options save=allpub
save Vb500:p Vb1u:p
"""


def _ac(d, name, node):
    f = np.asarray(d[name]["freq"]).real
    return f, np.asarray(d[name][node])


def measure_z(o):
    net = OUTNET[o]
    scs = _common(0, 0) + f"Iac (0 {net}) isource mag=1\nacz {AC}\n"
    d = sr.run(scs, f"z_{o}")
    f, V = _ac(d, "acz", net)          # 1 A injected -> V = Z
    return f, V


def measure_p(o, s):
    onet, snet = OUTNET[o], SUPNET[s]
    scs = _common(m0=1 if s == "1p0" else 0, m8=1 if s == "1p8" else 0) + f"acp {AC}\n"
    d = sr.run(scs, f"p_{o}_{s}")
    f = np.asarray(d["acp"]["freq"]).real
    H = np.asarray(d["acp"][onet]) / np.asarray(d["acp"][snet])
    return f, H


def measure_noise(o):
    net = OUTNET[o]
    scs = _common(0, 0) + f"nz ({net} 0) {NOISE}\n"
    d = sr.run(scs, f"n_{o}")
    f = np.asarray(d["nz"]["freq"]).real
    Sv = np.asarray(d["nz"]["out"]).real
    return f, Sv


def measure_couple(a, b):
    anet, bnet = OUTNET[a], OUTNET[b]
    scs = _common(0, 0) + f"Iac (0 {anet}) isource mag=1\nacc {AC}\n"
    d = sr.run(scs, f"c_{a}_{b}")
    f, V = _ac(d, "acc", bnet)         # transimpedance out_a -> out_b
    return f, V


def measure_y(c):
    src = SINKSRC[c]
    scs = _common(mb500=1 if c == "i500n" else 0, mb1u=1 if c == "i1u" else 0) + f"acy {AC}\n"
    d = sr.run(scs, f"y_{c}")
    f = np.asarray(d["acy"]["freq"]).real
    I = np.asarray(d["acy"][f"{src}:p"])     # current the 1 V AC source delivers = -Ipin
    return f, -I                              # admittance looking into the sink pin


def measure_pi(c, s):
    src, snet = SINKSRC[c], SUPNET[s]
    scs = _common(m0=1 if s == "1p0" else 0, m8=1 if s == "1p8" else 0) + f"acpi {AC}\n"
    d = sr.run(scs, f"pi_{c}_{s}")
    f = np.asarray(d["acpi"]["freq"]).real
    I = np.asarray(d["acpi"][f"{src}:p"])
    return f, -I / np.asarray(d["acpi"][snet])


def main():
    ref = {"loads": np.array(["nom"]), "meta_vin1p0": 1.05, "meta_vin1p8": 1.8,
           "meta_note": "behavioral stand-in; LTI -> load-independent (single corner)"}
    db = lambda H: -20 * np.log10(np.abs(H))

    print("=== voltage outputs: Zout / PSRR(x2 supplies) / noise ===")
    for o in ("pll", "vco"):
        fz, Z = measure_z(o); ref[f"z_{o}_nom"] = np.c_[fz, Z.real, Z.imag]
        fn, Sv = measure_noise(o); ref[f"noise_{o}_nom"] = np.c_[fn, Sv]
        line = f"  {o}: Zout LF={np.abs(Z[0]):.4g}ohm @304MHz={np.interp(304e6,fz,np.abs(Z)):.4g}ohm"
        for s in ("1p0", "1p8"):
            fp, H = measure_p(o, s); ref[f"p_{o}_{s}_nom"] = np.c_[fp, H.real, H.imag]
            line += f" | PSRR<-{s} LF={db(H)[0]:.1f}dB @304M={np.interp(304e6,fp,db(H)):.1f}dB"
        band = (fn >= 100) & (fn <= 100e6)
        _trap = getattr(np, "trapezoid", None) or np.trapz
        nint = np.sqrt(_trap(Sv[band]**2, fn[band]))
        print(line + f" | noise={nint*1e6:.2f}uVrms")

    print("=== shared-ref coupling (inject one output, read the other) ===")
    for a, b in (("pll", "vco"), ("vco", "pll")):
        fc, Vc = measure_couple(a, b); ref[f"couple_{a}_{b}_nom"] = np.c_[fc, Vc.real, Vc.imag]
        print(f"  {a}->{b}: |Z_couple| LF={np.abs(Vc[0]):.4g}ohm  (vs own Zout ~0.1ohm)")

    print("=== current sinks: admittance / current-PSRR ===")
    for c in ("i500n", "i1u"):
        try:
            fy, Y = measure_y(c); ref[f"y_{c}_nom"] = np.c_[fy, Y.real, Y.imag]
            fpi, PI = measure_pi(c, "1p0"); ref[f"pi_{c}_1p0_nom"] = np.c_[fpi, PI.real, PI.imag]
            print(f"  {c}: Y LF={np.abs(Y[0]):.3g}S (Rout={1/np.abs(Y[0]):.3g}ohm) "
                  f"| dI/dVdd1p0 LF={np.abs(PI[0]):.3g}A/V")
        except Exception as e:
            print(f"  {c}: FAILED ({repr(e)[:160]})")

    REFDIR = ROOT / "results" / "ref"; REFDIR.mkdir(parents=True, exist_ok=True)
    out = REFDIR / "pmu_standin.npz"
    np.savez(out, **ref)
    print(f"\nsaved {out}  ({len(ref)} arrays)")


if __name__ == "__main__":
    main()
