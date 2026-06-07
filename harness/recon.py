"""One-shot reconnaissance of the GT LDO for the modeling-approach evaluation.
Characterizes things the current reference does NOT capture:
  (1) verify Zout/PSRR vs documented table
  (2) output noise PSD  (.noise)         -> for noise-modeling requirement
  (3) transient load-step response       -> for trans-fidelity requirement
      at small (linear) and large (slew) drive, to locate the linearity edge
  (4) DC load & line regulation
Prints raw numbers only; writes no permanent artifacts except work/recon/*.
"""
import numpy as np
import ng

WORK = ng.ROOT / "work" / "recon"
LOADS = ["20u", "121u", "250u"]


def run(tb, tag, out="o.dat"):
    r = ng.run(ng.assemble(tb), WORK / tag, outputs=[out])
    if r[out] is None:
        raise RuntimeError(f"{tag} no data:\n{r['_stderr'][-1500:]}\n---STDOUT---\n{r['_stdout'][-1500:]}")
    return r[out][1]


# ---------- (1) Zout / PSRR sanity ----------
def zout_psrr():
    print("=" * 70, "\n(1) Zout / PSRR @ corners  (verify vs PROJECT.md table)\n", "=" * 70)
    for il in LOADS:
        tbz = f"""* z
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC {il} AC 0
Iac 0 vout AC 1
.control
set wr_singlescale
ac dec 40 10 100meg
wrdata o.dat vr(vout) vi(vout)
quit
.endc
.end
"""
        a = run(tbz, "z")
        f, Z = a[:, 0], np.abs(a[:, 1] + 1j * a[:, 2])
        ipk = np.argmax(Z)
        tbp = tbz.replace("Vin vin 0 DC 1.05", "Vin vin 0 DC 1.05 AC 1").replace("Iac 0 vout AC 1", "")
        ap = run(tbp, "p")
        H = np.abs(ap[:, 1] + 1j * ap[:, 2])
        psrr = -20 * np.log10(H)
        print(f"  {il:>5}: Zlf={Z[0]:6.1f}  Zpk={Z[ipk]:6.1f}@{f[ipk]/1e6:.3f}MHz  "
              f"Z@8M={np.interp(8e6,f,Z):5.1f} Z@16M={np.interp(16e6,f,Z):5.1f} Z@24M={np.interp(24e6,f,Z):5.1f}"
              f" | PSRRlf={psrr[0]:4.1f} worst={psrr.min():4.1f}dB")


# ---------- (2) output noise PSD ----------
def noise():
    print("\n", "=" * 70, "\n(2) Output noise PSD at vout  (.noise, input ref = Vin)\n", "=" * 70, sep="")
    for il in LOADS:
        tb = f"""* noise
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05 AC 1
Iload vout 0 DC {il}
.control
set wr_singlescale
noise v(vout) Vin dec 20 10 100meg
setplot noise1
wrdata o.dat onoise_spectrum
quit
.endc
.end
"""
        try:
            a = run(tb, "n")
            f, s = a[:, 0], a[:, 1]                    # V/sqrt(Hz)
            # integrate PSD (s^2) over band -> rms
            band = (f >= 100) & (f <= 100e6)
            _trap = getattr(np, "trapezoid", None) or np.trapz
            rms = np.sqrt(_trap(s[band] ** 2, f[band]))
            print(f"  {il:>5}: Sv@10Hz={s[0]*1e9:7.2f} @1k={np.interp(1e3,f,s)*1e9:7.2f}"
                  f" @100k={np.interp(1e5,f,s)*1e9:7.2f} @1M={np.interp(1e6,f,s)*1e9:7.2f} nV/rtHz"
                  f" | int(100Hz-100MHz)={rms*1e6:.2f} uVrms")
        except Exception as e:
            print(f"  {il:>5}: NOISE FAILED -> {str(e)[:300]}")


# ---------- (3) transient load step ----------
def load_step():
    print("\n", "=" * 70, "\n(3) Transient load-step droop  (linear vs slew edge)\n", "=" * 70, sep="")
    print("  step from 121u to 121u+dI with 1ns edge; report droop & overshoot")
    base = 121e-6
    for dI in [10e-6, 50e-6, 200e-6, 1e-3, 5e-3]:
        tb = f"""* load step
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {base} 5u {base} 5.001u {base+dI} 15u {base+dI} 15.001u {base} 25u {base})
.control
set wr_singlescale
tran 2n 25u 0 2n
wrdata o.dat v(vout)
quit
.endc
.end
"""
        a = run(tb, "t")
        t, v = a[:, 0], a[:, 1]
        pre = v[(t > 3e-6) & (t < 5e-6)].mean()          # settled before step
        m = (t > 5e-6) & (t < 13e-6)
        vmin = v[m].min()
        settled = v[(t > 13e-6) & (t < 15e-6)].mean()
        droop = (pre - vmin) * 1e3
        droop_per_ma = droop / (dI * 1e3)
        print(f"  dI={dI*1e6:6.0f}uA: Vpre={pre*1e3:7.3f}mV droop={droop:7.3f}mV "
              f"settled={settled*1e3:7.3f}mV  droop/mA={droop_per_ma:7.2f}mV/mA")


# ---------- (4) DC regulation ----------
def dc_reg():
    print("\n", "=" * 70, "\n(4) DC load & line regulation\n", "=" * 70, sep="")
    tb = """* dc load reg
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC 1u
.control
set wr_singlescale
dc Iload 1u 500u 5u
wrdata o.dat v(vout)
quit
.endc
.end
"""
    a = run(tb, "dc")
    il, v = a[:, 0], a[:, 1]
    print(f"  load reg: Vout@1u={v[0]*1e3:.3f}mV @121u={np.interp(121e-6,il,v)*1e3:.3f}mV "
          f"@250u={np.interp(250e-6,il,v)*1e3:.3f}mV @500u={v[-1]*1e3:.3f}mV "
          f"-> {(v[0]-v[-1])*1e3/0.499:.3f}mV/mA")
    tb2 = """* line reg
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC 121u
.control
set wr_singlescale
dc Vin 0.95 1.25 0.01
wrdata o.dat v(vout)
quit
.endc
.end
"""
    a = run(tb2, "dc2")
    vin, v = a[:, 0], a[:, 1]
    print(f"  line reg: Vout@Vin0.95={np.interp(0.95,vin,v)*1e3:.3f}mV @1.05={np.interp(1.05,vin,v)*1e3:.3f}mV "
          f"@1.25={np.interp(1.25,vin,v)*1e3:.3f}mV -> {(np.interp(1.25,vin,v)-np.interp(0.95,vin,v))*1e3/0.3:.3f}mV/V")


if __name__ == "__main__":
    zout_psrr()
    noise()
    load_step()
    dc_reg()
