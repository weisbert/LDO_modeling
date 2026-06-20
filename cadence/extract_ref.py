"""Spectre equivalent of harness/gen_reference.py — characterize a DUT in Spectre
and write results/ref/<name>.npz in the EXACT CADENCE_EXTRACTION.md schema, so
`cd harness && python fit_model.py --variant <name>` consumes it unchanged.

Phase 1 : DUT = the emitted Verilog-A model (round-trip against a known answer).
Phase 2 : DUT = the transistor GT spice subckt (ngspice cross-sim).

Intrinsic-spur arrays are emitted EMPTY here: neither the behavioral model nor the
ldo_gt reference has on-chip oscillators/charge-pumps (spur_F is empty by the
contract). The 304 MHz RF use-case spur is an *aggressor* that rides PSRR/Zout in
the system testbench, not an intrinsic tone — so it needs no extraction here.
"""
import argparse
import pathlib
import numpy as np
import spectre_bench as sb
import bench

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFDIR = ROOT / "results" / "ref"
_trap = getattr(np, "trapezoid", None) or np.trapz


def extract(dut, name, cout=np.nan, esr=np.nan):
    REFDIR.mkdir(parents=True, exist_ok=True)
    ref = {"loads": np.array(bench.LOADS), "meta_cout": cout, "meta_esr": esr}

    print("=== AC: Zout / PSRR / noise per corner ===")
    for il in bench.LOADS:
        fz, Z = sb.measure_zout(dut, il)
        fp, H = sb.measure_psrr(dut, il)
        fn, Sv = sb.measure_noise(dut, il)
        ref[f"z_{il}"] = np.c_[fz, Z.real, Z.imag]
        ref[f"p_{il}"] = np.c_[fp, H.real, H.imag]
        ref[f"noise_{il}"] = np.c_[fn, Sv]
        # supply-spur rejection: GT supply->output attenuation at the AVDD aggressor tones
        ssf, ssat = bench.supply_spur_atten(fp, H)
        ref[f"supply_spur_{il}"] = np.c_[ssf, ssat]
        zmag = np.abs(Z); ipk = int(np.argmax(zmag))
        band = (fn >= 100) & (fn <= 100e6)
        nrms = np.sqrt(_trap(Sv[band] ** 2, fn[band]))
        print(f"  {il:>5}: Zout LF={zmag[0]:6.1f} peak={zmag[ipk]:6.1f}@{fz[ipk]/1e6:.3f}MHz "
              f"| PSRR LF={-20*np.log10(abs(H[0])):4.1f} worst={(-20*np.log10(np.abs(H))).min():4.1f}dB "
              f"| noise int={nrms*1e6:.2f}uVrms")

    print("=== HF extension (121u -> 500MHz) ===")
    fzh, Zh = sb.measure_zout(dut, "121u", accmd=sb.AC_HF, tag="zhf")
    fph, Hh = sb.measure_psrr(dut, "121u", accmd=sb.AC_HF, tag="phf")
    ref["z_121u_hf"] = np.c_[fzh, Zh.real, Zh.imag]
    ref["p_121u_hf"] = np.c_[fph, Hh.real, Hh.imag]
    print(f"  Zout@304MHz={np.interp(304e6,fzh,np.abs(Zh)):.2f}ohm  "
          f"PSRR@304MHz={-20*np.log10(np.interp(304e6,fph,np.abs(Hh))):.1f}dB")

    print("=== Transient load steps ===")
    for il in bench.LOADS:
        base = float(il.replace("u", "e-6"))
        dI = bench.LIN_FRAC * base
        t, vv = sb.measure_loadstep(dut, dI, iload=base, tag=f"tl_{il}")
        ref[f"trans_lin_{il}"] = np.c_[t, vv]
        pre = vv[(t > 3e-6) & (t < 5e-6)].mean()
        droop = (pre - vv[(t > 5e-6) & (t < 13e-6)].min()) * 1e3
        print(f"  {il:>5} lin(+{dI*1e6:.0f}uA): droop={droop:6.3f}mV ring={bench.ring_freq(t,vv)/1e6:.2f}MHz")
    for tag in ("big", "slew"):
        t, vv = sb.measure_loadstep(dut, bench.STEP_DI[tag], iload=121e-6, tag=f"t_{tag}")
        ref[f"trans_{tag}_121u"] = np.c_[t, vv]

    print("=== DC regulation + dropout ===")
    il_s, v_s = sb.measure_dc_loadreg(dut)
    vin_s, vl_s = sb.measure_dc_linereg(dut)
    id_s, vd_s = sb.measure_dc_loadreg(dut, istop=6e-3, istep=20e-6, tag="drop")
    ref["dc_loadreg"] = np.c_[il_s, v_s]
    ref["dc_linereg"] = np.c_[vin_s, vl_s]
    ref["dc_dropout"] = np.c_[id_s, vd_s]
    print(f"  load reg={(v_s[0]-v_s[-1])*1e3/0.499:.2f}mV/mA  "
          f"line reg={(np.interp(1.25,vin_s,vl_s)-np.interp(0.95,vin_s,vl_s))*1e3/0.3:.2f}mV/V")

    print("=== Spur sanity (8MHz tone, 500uA) ===")
    fs, As = sb.measure_spur(dut, "500u")
    ref["spur_500u"] = np.c_[fs, As]
    a1 = bench.level_at(fs, As, 8e6)
    print(f"  16M={20*np.log10(bench.level_at(fs,As,16e6)/(a1+1e-30)):.0f}dBc "
          f"24M={20*np.log10(bench.level_at(fs,As,24e6)/(a1+1e-30)):.0f}dBc")

    # intrinsic spurs: empty for behavioral model + ldo_gt (no on-chip tones)
    ref["spur_F"] = np.array([])
    ref["spur_twin0"] = np.array(bench.TWIN[0])
    ref["spur_binhz"] = np.array(1.0 / (bench.TSTOP - bench.TWIN[0]))
    for il in bench.LOADS:
        ref[f"spurs_{il}"] = np.empty((0, 3))

    out = REFDIR / f"{name}.npz"
    np.savez(out, **ref)
    print(f"\nsaved {out}  ({len(ref)} arrays)")
    return out


def gt_variant_dut(vkey):
    """Build a Spectre GT DutSpec for any transistor variant in harness/variants.py
    (registry of libs / subckt / xparams). Shared BSIM3 model cards + the variant's
    own subckt lib(s)."""
    import variants
    v = variants.get(vkey)
    models = [ROOT / "models" / "nmos_lv.mod", ROOT / "models" / "pmos_lv.mod"]
    subckts = [str(p) for p in v["libs"]]
    dut = sb.spice_dut([str(m) for m in models], subckts, v["subckt"], xparams=v["xparams"])
    return dut, v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dut", choices=["va", "gt", "variant"], default="va")
    ap.add_argument("--variant", default=None, help="registry key (variants.py); GT cross-sim")
    ap.add_argument("--name", default=None)
    ap.add_argument("--va", default=str(ROOT / "model" / "ldo_model.va"))
    ap.add_argument("--module", default="ldo_model")
    ap.add_argument("--subckt", default="ldo_gt")
    ap.add_argument("--xparams", default="")
    a = ap.parse_args()
    if a.variant:                                  # registry-driven GT (any architecture)
        dut, v = gt_variant_dut(a.variant)
        name = a.name or f"{a.variant}_spectre"
        extract(dut, name, cout=v["cout"], esr=v["esr"])
    elif a.dut == "va":
        dut = sb.va_dut(a.va, module=a.module)
        name = a.name or "va_rt"
        extract(dut, name)
    else:
        models = [ROOT / "models" / "nmos_lv.mod", ROOT / "models" / "pmos_lv.mod"]
        subckts = [ROOT / "ground_truth" / "ldo_gt.lib"]
        dut = sb.spice_dut([str(m) for m in models], [str(s) for s in subckts],
                           a.subckt, xparams=a.xparams)
        name = a.name or "gt_spectre"
        extract(dut, name, cout=1e-9, esr=0.5)


if __name__ == "__main__":
    main()
