"""Generate the reference dataset from a ground-truth LDO VARIANT. This is the
fixed TARGET that candidate models are scored against. Run once per variant
(re-run if the GT or device cards change).

    python gen_reference.py                 # variant 'base' (Target A)
    python gen_reference.py --variant v1_nmos

Stored in results/ref/{variant}.npz:
  z_{il}, p_{il}        Zout(f), PSRR(f) COMPLEX per load corner (AC 10Hz-100MHz)
  z_121u_hf, p_121u_hf  same, extended to 500MHz (bound model up to RF carrier;
                        z_121u_hf also drives Cout/ESR auto-extraction in the fit)
  noise_{il}            output noise PSD Sv(f) [V/rtHz] per corner (req#2 target)
  trans_lin_{il}        small load step (linear regime) v(vout,t)        (req#1)
  trans_big_121u        1mA step (gm-compression onset)
  trans_slew_121u       5mA step (slew/dropout diagnostic, out-of-LTI-scope)
  dc_loadreg, dc_linereg, dc_dropout   DC regulation / dropout curves
  ibp_xfer_{il}         IBP(bias)->Vout transimpedance (optional bias port; skipped
                        if the variant has no bias node)
  spur_500u             8MHz-tone FFT (linearity SANITY check)
"""
import argparse
import numpy as np
import bench
import ng
import variants
import spur_char

REFDIR = ng.ROOT / "results" / "ref"


def _ibp_xfer(libs, subckt, il, xparams, biasnode):
    """IBP->Vout transimpedance via hierarchical injection into the GT bias node."""
    tb = f"""* IBP(bias) -> Vout transfer
Xldo vin vout {subckt} {xparams}
Vin vin 0 DC 1.05
Iload vout 0 DC {il}
Iibp xldo.{biasnode} 0 AC 1
.control
set wr_singlescale
{bench.AC}
wrdata out.dat vr(vout) vi(vout)
quit
.endc
.end
"""
    r = ng.run(ng.assemble(tb, libs=libs), bench.WORK / "ibp", outputs=["out.dat"])
    if r["out.dat"] is None:
        raise RuntimeError("ibp run failed:\n" + r["_stderr"][-1500:])
    return ng.complex_col(r["out.dat"][1])


def main(vkey="base"):
    v = variants.get(vkey)
    libs, sub, xp, bias = v["libs"], v["subckt"], v["xparams"], v["biasnode"]
    REFDIR.mkdir(parents=True, exist_ok=True)
    ref = {"loads": np.array(bench.LOADS), "meta_cout": v["cout"], "meta_esr": v["esr"]}
    _trap = getattr(np, "trapezoid", None) or np.trapz
    print(f"### VARIANT '{vkey}': {v['note']}\n    subckt={sub} xparams='{xp}' libs={[p.name for p in libs]}")

    print("=== AC: Zout / PSRR / noise / IBP-transfer per corner ===")
    for il in bench.LOADS:
        fz, Z = bench.measure_zout(libs, sub, il, xparams=xp)
        fp, H = bench.measure_psrr(libs, sub, il, xparams=xp)
        fn, Sv = bench.measure_noise(libs, sub, il, xparams=xp)
        ref[f"z_{il}"] = np.c_[fz, Z.real, Z.imag]
        ref[f"p_{il}"] = np.c_[fp, H.real, H.imag]
        ref[f"noise_{il}"] = np.c_[fn, Sv]
        # supply-spur rejection: GT supply->output attenuation at the AVDD aggressor tones
        ssf, ssat = bench.supply_spur_atten(fp, H)
        ref[f"supply_spur_{il}"] = np.c_[ssf, ssat]
        if bias:
            fi, Zi = _ibp_xfer(libs, sub, il, xp, bias)
            ref[f"ibp_xfer_{il}"] = np.c_[fi, Zi.real, Zi.imag]
        zmag, ipk = np.abs(Z), int(np.argmax(np.abs(Z)))
        band = (fn >= 100) & (fn <= 100e6)
        nrms = np.sqrt(_trap(Sv[band] ** 2, fn[band]))
        res = (fn > 0.5e6) & (fn < 3e6)
        npk = Sv[res].max() if res.any() else np.nan
        print(f"  {il:>5}: Zout LF={zmag[0]:6.1f} peak={zmag[ipk]:6.1f}@{fz[ipk]/1e6:.3f}MHz "
              f"(Q~{zmag[ipk]/zmag[0]:.1f}) | "
              f"PSRR LF={-20*np.log10(np.abs(H[0])):4.1f} worst={(-20*np.log10(np.abs(H))).min():4.1f}dB | "
              f"noise wht@200k={np.interp(2e5,fn,Sv)*1e9:5.1f} pk={npk*1e9:6.1f} int={nrms*1e6:.1f}uVrms")

    # R4: held-out off-corner LOAD noise -- GT noise at currents BETWEEN the fit corners (the
    # ln-midpoints, worst-case for the model's quad-in-ln amplitude interpolation over frozen
    # poles). VALIDATION-only: never fitted, never in the composite; score grades the model's
    # interpolated noise here against this GT (the most-exercised axis: PMU load lines sweep
    # current continuously, yet the off-corner spectrum is otherwise unobserved).
    for il in bench.OFFGRID_NOISE_LOADS:
        fn, Sv = bench.measure_noise(libs, sub, il, xparams=xp)
        ref[f"noise_offgrid_{il}"] = np.c_[fn, Sv]
        band = (fn >= 100) & (fn <= 100e6)
        nrms = np.sqrt(_trap(Sv[band] ** 2, fn[band]))
        print(f"  off-corner {il:>5}: noise wht@200k={np.interp(2e5,fn,Sv)*1e9:5.1f}nV "
              f"int={nrms*1e6:.1f}uVrms  (HELD-OUT, not fitted)")

    # *_hf ceiling is a per-variant characterization-recipe param (default 500MHz; a GHz part
    # overrides it via variants[..]["hf_stop"]). The array NAMES keep the nominal "121u" token
    # (de-hardcoding the nominal corner is the separate deferred R1 item).
    fstop = v.get("hf_stop") or bench.HF_STOP
    accmd_hf = bench.ac_hf_cmd(fstop)
    print(f"=== HF extension (121u, to {fstop/1e6:.0f}MHz) -> also Cout/ESR autoextract source ===")
    fzh, Zh = bench.measure_zout(libs, sub, "121u", xparams=xp, accmd=accmd_hf)
    fph, Hh = bench.measure_psrr(libs, sub, "121u", xparams=xp, accmd=accmd_hf)
    ref["z_121u_hf"] = np.c_[fzh, Zh.real, Zh.imag]
    ref["p_121u_hf"] = np.c_[fph, Hh.real, Hh.imag]
    fcar = 0.6 * fstop   # representative carrier-band probe = a fraction of the ceiling (general)
    print(f"  Zout@{fcar/1e6:.0f}MHz={np.interp(fcar,fzh,np.abs(Zh)):.2f}ohm  "
          f"PSRR@{fcar/1e6:.0f}MHz={-20*np.log10(np.interp(fcar,fph,np.abs(Hh))):.1f}dB")

    print("=== Transient load steps (req#1) ===")
    for il in bench.LOADS:
        base = ng.amps(il)
        dI = bench.LIN_FRAC * base
        t, vv = bench.measure_loadstep(libs, sub, dI, iload=base, xparams=xp)
        ref[f"trans_lin_{il}"] = np.c_[t, vv]
        pre = vv[(t > 3e-6) & (t < 5e-6)].mean()
        droop = (pre - vv[(t > 5e-6) & (t < 13e-6)].min()) * 1e3
        print(f"  {il:>5} lin(+{dI*1e6:.0f}uA): droop={droop:6.3f}mV "
              f"ring={bench.ring_freq(t,vv)/1e6:.2f}MHz")
    for tag in ("big", "slew"):
        t, vv = bench.measure_loadstep(libs, sub, bench.STEP_DI[tag], iload=121e-6, xparams=xp)
        ref[f"trans_{tag}_121u"] = np.c_[t, vv]
        pre = vv[(t > 3e-6) & (t < 5e-6)].mean()
        droop = (pre - vv[(t > 5e-6) & (t < 13e-6)].min()) * 1e3
        print(f"  121u {tag}(+{bench.STEP_DI[tag]*1e3:.1f}mA): droop={droop:7.3f}mV")

    print("=== DC regulation + dropout ===")
    il_s, v_s = bench.measure_dc_loadreg(libs, sub, xparams=xp)
    vin_s, vl_s = bench.measure_dc_linereg(libs, sub, xparams=xp)
    id_s, vd_s = bench.measure_dc_loadreg(libs, sub, xparams=xp, istop="6m", istep="20u")
    ref["dc_loadreg"] = np.c_[il_s, v_s]
    ref["dc_linereg"] = np.c_[vin_s, vl_s]
    ref["dc_dropout"] = np.c_[id_s, vd_s]
    idrop = id_s[np.argmin(np.abs(vd_s - 0.5))]
    print(f"  load reg={(v_s[0]-v_s[-1])*1e3/0.499:.2f}mV/mA  "
          f"line reg={(np.interp(1.25,vin_s,vl_s)-np.interp(0.95,vin_s,vl_s))*1e3/0.3:.2f}mV/V  "
          f"Vout=0.5V@{idrop*1e3:.2f}mA (dropout)")

    print("=== Spur sanity (8MHz tone, 500uA) ===")
    fs, As = bench.measure_spur(libs, sub, "500u", xparams=xp)
    ref["spur_500u"] = np.c_[fs, As]
    a1 = bench.level_at(fs, As, 8e6)
    print(f"  16M={20*np.log10(bench.level_at(fs,As,16e6)/a1):.0f}dBc "
          f"24M={20*np.log10(bench.level_at(fs,As,24e6)/a1):.0f}dBc (linear if << 0)")

    print("=== Intrinsic spur table (transient-FFT, NO external stimulus) ===")
    sc = spur_char.characterize_corners(libs, sub, bench.LOADS, xparams_of=lambda il: xp)
    ref["spur_F"] = np.array(sc["F"])
    ref["spur_twin0"] = np.array(spur_char.TWIN[0])
    ref["spur_binhz"] = np.array(spur_char.BINHZ)
    for il in bench.LOADS:
        ref[f"spurs_{il}"] = sc["per"][il]          # [N x 3] = (f, vout-amp, phase)
    if sc["F"]:
        nom = sc["per"]["121u"]
        print(f"  {len(sc['F'])} fundamental(s): " +
              "  ".join(f"{r[0]/1e6:.3f}MHz@{r[1]*1e6:.1f}uV" for r in nom) +
              (f"  + {len(sc['prods'])} IM/harmonic product(s) (not modeled)" if sc["prods"] else ""))
    else:
        print("  no discrete spurs above floor (smooth-noise DUT) -> spur block stays empty")

    out = REFDIR / f"{vkey}.npz"
    np.savez(out, **ref)
    print(f"\nsaved {out}  ({len(ref)} arrays)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    main(ap.parse_args().variant)
