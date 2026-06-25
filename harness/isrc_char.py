"""Characterize the MOS current-source GT library (ground_truth/isrc_gt.lib) at
the OUTPUT PIN -- the same terminal quantities the behavioral current model must
later reproduce (G1/G4/G5/G8/G3/G2):

  Idc        DC bias current at the nominal compliance vc                [G1]
  I-V knee   compliance window from a Vout sweep (saturation->triode)    [G5]
  Y(s)       output admittance g0+sCp  -> rout, Cp                       [G7/G8]
  PSRR       dIout/dVdd transfer (magnitude AND SIGN, vs freq)           [G4]
  In         output current-noise density (bias-tee: In = onoise*|Y|)    [G3]
  Idc(T)     temperature law over -40/55/125 C, PTAT slope               [G2]

Pure offline (ngspice via harness/ng.py). No Cadence. `python isrc_char.py`
prints a cross-variant table proving the >=8 sources are physically DIVERSE
(anti-overfit).
"""
import os
import sys
import pathlib
import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ng                                                              # noqa: E402
from isrc_variants import VARIANTS, ISRC_LIB, VDD                      # noqa: E402

WORK = ng.ROOT / "work_isrc"
TEMPS = (-40.0, 55.0, 125.0)        # confirmed PDK points; 55 = nominal
TNOM = 55.0
# --- held-out characterization for the adversarial overfit-probe gates (HANDOFF_ADVERSARIAL_
# OVERFIT_PROBE.md, §C). These are NEVER fed to the fitter (fit_isrc reads only the 3-temp idcT /
# the single-vc psrr_g / the iv_v|iv_i at TNOM); they are extra npz fields the crossval_isrc gates
# diff the EMITTED model against -> the existing 8 baselines re-fit byte-identical (additive only).
HELDOUT_IDC_TEMPS = (25.0, 85.0)    # interior temps: the 3-temp line through (-40,55,125) misses them (B1)
PSRR_OFFVC_DELTA = 0.2              # off-compliance offset for the bias-dependent PSRR-sign gate (B3)


def _deck(body):
    return ng.assemble(body, libs=[ISRC_LIB])


def _head(sub, temp):
    return f".options temp={temp:g}\nVdd vdd 0 DC {VDD}\nXd vdd out {sub}\n"


def _run(name, tag, body, outfile):
    r = ng.run(_deck(body), WORK / name / tag, outputs=(outfile,))
    data = r.get(outfile)
    if data is None or r["_rc"] != 0:
        raise RuntimeError(f"{name}/{tag} ngspice failed (rc={r['_rc']}):\n{r['_stderr'][-1200:]}")
    return data[1]                                    # ndarray


def iv_curve(name, v, temp=TNOM):
    body = _head(v["subckt"], temp) + f"""Vout out 0 DC {v['vc']:g}
.control
dc Vout 0 {VDD} 0.005
wrdata iv.data i(vout)
.endc
.end
"""
    arr = _run(name, "iv", body, "iv.data")
    return arr[:, 0], np.abs(arr[:, 1])               # vout, |I|


def knee(vo, I, pol):
    """Compliance window: out-range where |I| >= 0.9*Iplateau."""
    Iplat = np.median(np.sort(I)[-8:])                # robust plateau (top values)
    ok = I >= 0.9 * Iplat
    lo = vo[ok].min() if ok.any() else float("nan")
    hi = vo[ok].max() if ok.any() else float("nan")
    return Iplat, lo, hi


def admittance(name, v, temp=TNOM):
    body = _head(v["subckt"], temp) + f"""Vout out 0 DC {v['vc']:g} AC 1
.control
ac dec 20 10 500meg
wrdata y.data i(vout)
.endc
.end
"""
    arr = _run(name, "y", body, "y.data")
    f = arr[:, 0]; y = arr[:, 1] + 1j * arr[:, 2]
    rout = 1.0 / abs(y[0].real) if y[0].real != 0 else float("inf")
    # i(vout) = -(current into the pin), so Im(y) carries the probe-convention sign;
    # the physical output capacitance is |Im(Y)|/w.
    cp = abs(y[-1].imag) / (2 * np.pi * f[-1])
    return f, y, rout, cp


def psrr(name, v, temp=TNOM):
    head = _head(v["subckt"], temp).replace(
        f"Vdd vdd 0 DC {VDD}\n", f"Vdd vdd 0 DC {VDD} AC 1\n")
    body = head + f"""Vout out 0 DC {v['vc']:g} AC 0
.control
ac dec 20 10 500meg
wrdata p.data i(vout)
.endc
.end
"""
    arr = _run(name, "psrr", body, "p.data")
    f = arr[:, 0]; g = arr[:, 1] + 1j * arr[:, 2]     # dIout/dVdd [S]
    return f, g


def psrr_at(name, v, vo, temp=TNOM):
    """LF dIout/dVdd [S, complex] at an EXPLICIT compliance voltage `vo` (vs psrr() which uses
    the nominal vc). Used to capture the off-compliance PSRR sign for the B3 gate (held-out)."""
    head = _head(v["subckt"], temp).replace(
        f"Vdd vdd 0 DC {VDD}\n", f"Vdd vdd 0 DC {VDD} AC 1\n")
    body = head + f"""Vout out 0 DC {vo:g} AC 0
.control
ac dec 20 10 500meg
wrdata p.data i(vout)
.endc
.end
"""
    arr = _run(name, f"psrr_{vo:.2f}", body, "p.data")
    return complex(arr[0, 1] + 1j * arr[0, 2])           # LF dIout/dVdd


def noise(name, v, yf, y, temp=TNOM):
    body = _head(v["subckt"], temp) + f"""Lbias out ncc 1e9
Vcc ncc 0 DC {v['vc']:g} AC 1
.control
noise v(out) Vcc dec 10 10 100meg
setplot noise1
wrdata n.data onoise_spectrum
.endc
.end
"""
    arr = _run(name, "noise", body, "n.data")
    fn = arr[:, 0]; onoise = arr[:, 1]                # V/rtHz at the floated node
    ymag = np.interp(fn, yf, np.abs(y))               # |Y| on the noise grid
    In = onoise * ymag                                # A/rtHz short-circuit output current noise
    return fn, In


def tempco(name, v):
    idc = []
    for T in TEMPS:
        vo, I = iv_curve(name, v, temp=T)
        idc.append(float(np.interp(v["vc"], vo, I)))
    idc = np.array(idc)
    Tk = np.array(TEMPS) + 273.15
    slope = np.polyfit(Tk, idc, 1)[0]                 # dIdc/dT [A/K]
    ptat_ratio = idc[-1] / idc[0]                     # I(125)/I(-40)
    return idc, slope, ptat_ratio


def characterize(name):
    v = VARIANTS[name]
    vo, I = iv_curve(name, v)
    Iplat, klo, khi = knee(vo, I, v["pol"])
    idc_vc = float(np.interp(v["vc"], vo, I))
    f, y, rout, cp = admittance(name, v)
    fp, g = psrr(name, v)
    glf = g[0]                                         # low-freq dIout/dVdd
    fn, In = noise(name, v, f, y)
    In_1k = float(np.interp(1e3, fn, In))
    In_100k = float(np.interp(1e5, fn, In))
    idcT, slope, ptat = tempco(name, v)
    # --- HELD-OUT probe data (additive; never fitted -- see HELDOUT_IDC_TEMPS / PSRR_OFFVC_DELTA) ---
    # B1: Idc at the interior held-out temps -> 3-temp straight-line miss
    idcT_held = np.array([float(np.interp(v["vc"], *iv_curve(name, v, temp=T)))
                          for T in HELDOUT_IDC_TEMPS])
    # B3: off-compliance LF PSRR sign (dIout/dVdd at vc +- delta, clamped to the rail)
    vlo = max(0.0, v["vc"] - PSRR_OFFVC_DELTA)
    vhi = min(VDD, v["vc"] + PSRR_OFFVC_DELTA)
    g_lf_lo = psrr_at(name, v, vlo)
    g_lf_hi = psrr_at(name, v, vhi)
    # B4: full IV(Vo) at each fit temp on the shared `vo` grid -> compliance-knee-vs-T
    iv_i_temps = np.array([np.interp(vo, *iv_curve(name, v, temp=T)) for T in TEMPS]).T
    return dict(name=name, pol=v["pol"], idc=idc_vc, knee_lo=klo, knee_hi=khi,
                rout=rout, cp=cp, g_lf=glf, In_1k=In_1k, In_100k=In_100k,
                idcT=idcT, dIdT=slope, ptat=ptat, note=v["note"],
                # raw curves (modeling input for the behavioral fit step)
                iv_v=vo, iv_i=I, ac_f=f, ac_y=y, psrr_f=fp, psrr_g=g,
                nz_f=fn, nz_in=In, temps=np.array(TEMPS), vc=v["vc"],
                # held-out probe grids (crossval_isrc gates diff the EMITTED model vs these)
                idcT_held=idcT_held, temps_held=np.array(HELDOUT_IDC_TEMPS),
                g_lf_lo=g_lf_lo, g_lf_hi=g_lf_hi, vc_lo=float(vlo), vc_hi=float(vhi),
                iv_i_temps=iv_i_temps)


def save_npz(name, r):
    out = WORK / f"{name}.npz"
    np.savez(out, **{k: v for k, v in r.items()
                     if isinstance(v, (np.ndarray, int, float, str, complex))})
    return out


def main():
    rows = []
    for name in VARIANTS:
        try:
            r = characterize(name)
            save_npz(name, r)
            rows.append(r)
            print(f"  [ok] {name}  -> {WORK / (name + '.npz')}", file=sys.stderr)
        except Exception as e:
            print(f"  [FAIL] {name}: {e}", file=sys.stderr)
    print("\n=== MOS current-source GT library: terminal characterization "
          "(anti-overfit diversity) ===\n")
    hdr = (f"{'variant':<17}{'pol':<7}{'Idc[uA]':>9}{'knee[V]':>12}"
           f"{'rout[Mohm]':>11}{'Cp[fF]':>9}{'dId/dVdd[nS]':>13}"
           f"{'In@1k[fA/rt]':>13}{'PTAT I125/I-40':>15}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        knee_s = f"{r['knee_lo']:.2f}-{r['knee_hi']:.2f}"
        print(f"{r['name']:<17}{r['pol']:<7}{r['idc']*1e6:>9.3f}{knee_s:>12}"
              f"{r['rout']/1e6:>11.1f}{r['cp']*1e15:>9.1f}{r['g_lf'].real*1e9:>13.3f}"
              f"{r['In_1k']*1e15:>13.1f}{r['ptat']:>15.3f}")
    print("\nIdeal-PTAT I(125C)/I(-40C) = 398/233 = 1.708  (v6_ptat should be ~that;")
    print("flat/CTAT parts sit well below 1.708 -> the temp axis is diverse).")
    print("dId/dVdd sign: resistor-biased v7 should be the supply-pushing outlier.\n")
    return rows


if __name__ == "__main__":
    main()
