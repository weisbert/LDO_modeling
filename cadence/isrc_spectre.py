"""Run the WHOLE current-source flow on the LOCAL Spectre CLI:
  MOS-transistor GT (ported to Spectre spice: BSIM3 level 8->49, {param}->bare)
  vs the emitted behavioral Verilog-A model (ahdl_include, ahdlcmi -64) -- the same
  probe (Vout:p) measures both, so signs are directly comparable (no convention
  bookkeeping). Proves the offline ngspice pipeline transfers to Cadence/Spectre.

  IV (dc sweep of the probe) · PSRR (ac on supply, dI_pin/dVdd) · Idc(T) (temp option).

`python cadence/isrc_spectre.py [name ...]`   (needs SPECTRE181 per cadence/spectre_run.py
 and work_isrc/*.npz from harness/isrc_char.py).
"""
import os
import re
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "harness"))
os.environ.setdefault("LDO_SPECTRE_WORK", "work_isrc_sp")
import spectre_run as sr                                               # noqa: E402
from fit_isrc import fit_isrc                                          # noqa: E402
from emit_pmu_model import emit_pmu_va, current_crow_from_isrc_fit     # noqa: E402
from isrc_variants import VARIANTS, VDD                                # noqa: E402

WORK = ROOT / "work_isrc"
WORK_SP = ROOT / "work_isrc_sp"                # Spectre-characterized npz (gitignored work_*)
WORK_SP.mkdir(parents=True, exist_ok=True)
VADIR = ROOT / "cadence" / os.environ["LDO_SPECTRE_WORK"] / "va"
PIN = "OUTP"
TEMPS = (-40.0, 55.0, 125.0)


def _gt_block(name):
    """GT transistor subckt ported to Spectre spice-lang (level 8->49, {p}->bare)."""
    models = "\n".join(re.sub(r'([Ll]evel\s*=\s*)8\b', r'\g<1>49', open(ROOT / m).read())
                       for m in ("models/nmos_lv.mod", "models/pmos_lv.mod"))
    lib = re.sub(r'\{\s*([A-Za-z_]\w*)\s*\}', r'\1',
                 open(ROOT / "ground_truth" / "isrc_gt.lib").read())
    sub = VARIANTS[name]["subckt"]
    return (f"simulator lang=spice\n{models}\n{lib}\n"
            f"xdut AVDD1P0 {PIN} {sub}\nsimulator lang=spectre\n")


def _va_block(name, p):
    """Emit the behavioral VA for this archetype, return the ahdl_include + instance."""
    VADIR.mkdir(parents=True, exist_ok=True)
    va = VADIR / f"{name}.va"
    crow = current_crow_from_isrc_fit(p, pin=PIN)
    emit_pmu_va(dict(voltage={}, current=[crow], meta={}), f"PMU_{name}", str(va),
                supply="AVDD1P0", ground="VSS", supply_dc=VDD)
    return f'ahdl_include "{va.resolve()}"\nXm (AVDD1P0 {PIN} 0) PMU_{name}\n'


def _opt(temp):
    return f"optsT options temp={temp:g}\n" if temp is not None else ""


def iv(block, vc, tag, temp=55.0):
    scs = (f"// IV\nsimulator lang=spectre\n{_opt(temp)}{block}"
           f"Vdd (AVDD1P0 0) vsource dc={VDD}\nVout ({PIN} 0) vsource dc={vc:g}\n"
           f"save Vout:p\nswp dc dev=Vout param=dc start=0 stop={VDD} step=0.005\n")
    d = sr.run(scs, tag)
    vo = np.asarray(d["swp"]["dc"]).real
    I = np.abs(np.asarray(d["swp"]["Vout:p"]).real)
    return vo, I


def ac_y(block, vc, tag, temp=55.0):
    scs = (f"// Y\nsimulator lang=spectre\n{_opt(temp)}{block}"
           f"Vdd (AVDD1P0 0) vsource dc={VDD}\nVout ({PIN} 0) vsource dc={vc:g} mag=1\n"
           f"save Vout:p\nacy ac start=10 stop=500M dec=20\n")
    d = sr.run(scs, tag)
    f = np.asarray(d["acy"]["freq"]).real
    y = np.asarray(d["acy"]["Vout:p"])
    return f, y


def psrr(block, vc, tag, temp=55.0):
    scs = (f"// PSRR\nsimulator lang=spectre\n{_opt(temp)}{block}"
           f"Vdd (AVDD1P0 0) vsource dc={VDD} mag=1\nVout ({PIN} 0) vsource dc={vc:g} mag=0\n"
           f"save Vout:p\nacp ac start=10 stop=500M dec=20\n")
    d = sr.run(scs, tag)
    g = np.asarray(d["acp"]["Vout:p"])
    return complex(g[0])                       # LF dI_pin/dVdd (signed complex)


def idc_at(block, vc, tag, temp):
    vo, I = iv(block, vc, tag, temp=temp)
    return float(np.interp(vc, vo, I))


def compare(name, do_temp=False):
    d = np.load(WORK / f"{name}.npz", allow_pickle=True)
    p = fit_isrc(WORK / f"{name}.npz")
    vc = p["vc"]
    gt, va = _gt_block(name), _va_block(name, p)

    gvo, gI = iv(gt, vc, f"{name}_gt_iv")
    mvo, mI = iv(va, vc, f"{name}_md_iv")
    g_idc = float(np.interp(vc, gvo, gI)); m_idc = float(np.interp(vc, mvo, mI))
    idc_err = abs(m_idc - g_idc) / g_idc
    plateau = gI > 0.5 * gI.max()
    mI_on = np.interp(gvo, mvo, mI)
    iv_rms = float(np.sqrt(np.mean(((mI_on[plateau] - gI[plateau]) / gI[plateau]) ** 2)))

    g_glf = psrr(gt, vc, f"{name}_gt_p"); m_glf = psrr(va, vc, f"{name}_md_p")
    sign_ok = (np.sign(g_glf.real) == np.sign(m_glf.real)) or (abs(g_glf.real) < 1e-12)

    ptat_err = float("nan")
    if do_temp:
        g_t = np.array([idc_at(gt, vc, f"{name}_gt_t{int(T)}", T) for T in TEMPS])
        m_t = np.array([idc_at(va, vc, f"{name}_md_t{int(T)}", T) for T in TEMPS])
        ptat_err = abs(m_t[-1] / m_t[0] - g_t[-1] / g_t[0])

    ok = idc_err < 0.03 and iv_rms < 0.06 and sign_ok and (np.isnan(ptat_err) or ptat_err < 0.05)
    return dict(name=name, pol=p["pol"], idc_err=idc_err, iv_rms=iv_rms,
                g_glf=g_glf.real, m_glf=m_glf.real, sign_ok=sign_ok,
                ptat_err=ptat_err, ok=ok)


def char_spectre(name):
    """Characterize the GT IN SPECTRE -> npz (same schema as harness/isrc_char.py).
    Deterministic terms (IV / Y / PSRR / Idc(T)) from Spectre; current-noise arrays are
    carried from the ngspice characterization (noise re-sim in Spectre is a later step and
    is not in the deterministic pass/fail). Saves work_isrc_sp/<name>.npz and returns it."""
    v = VARIANTS[name]; vc = v["vc"]; pol = v["pol"]
    gt = _gt_block(name)
    vo, I = iv(gt, vc, f"{name}_sc_iv")
    idc = float(np.interp(vc, vo, I))
    f, y = ac_y(gt, vc, f"{name}_sc_y")
    rout = 1.0 / abs(y[0].real); cp = abs(y[-1].imag) / (2 * np.pi * f[-1])
    pf = np.asarray(sr.run(  # PSRR full sweep (need the freq grid too)
        f"// PSRRf\nsimulator lang=spectre\n{gt}Vdd (AVDD1P0 0) vsource dc={VDD} mag=1\n"
        f"Vout ({PIN} 0) vsource dc={vc:g} mag=0\nsave Vout:p\nacp ac start=10 stop=500M dec=20\n",
        f"{name}_sc_p")["acp"]["freq"]).real
    g = psrr(gt, vc, f"{name}_sc_p2")            # LF value (re-run cheap; or reuse)
    idcT = np.array([idc_at(gt, vc, f"{name}_sc_t{int(T)}", T) for T in TEMPS])
    # borrow noise arrays from the ngspice characterization
    dn = np.load(WORK / f"{name}.npz", allow_pickle=True)
    Iplat = np.median(np.sort(I)[-8:]); ok = I >= 0.9 * Iplat
    out = dict(name=name, pol=pol, vc=vc, idc=idc, rout=rout, cp=cp,
               knee_lo=float(vo[ok].min()), knee_hi=float(vo[ok].max()),
               iv_v=vo, iv_i=I, ac_f=f, ac_y=y, psrr_f=pf,
               psrr_g=np.full_like(pf, g, dtype=complex), g_lf=g,
               nz_f=np.asarray(dn["nz_f"]), nz_in=np.asarray(dn["nz_in"]),
               temps=np.array(TEMPS), idcT=idcT)
    np.savez(WORK_SP / f"{name}.npz", **out)
    return WORK_SP / f"{name}.npz"


def selfconsistent(name, do_temp=False):
    """The WHOLE flow in Spectre, self-consistent: GT char (Spectre) -> fit -> emit VA
    -> validate (Spectre), all against the SAME Spectre GT card (no cross-sim mismatch)."""
    npz = char_spectre(name)
    p = fit_isrc(npz)
    d = np.load(npz, allow_pickle=True)
    vc = p["vc"]
    va = _va_block(name, p)
    gt = _gt_block(name)
    gvo, gI = np.asarray(d["iv_v"]), np.asarray(d["iv_i"])
    mvo, mI = iv(va, vc, f"{name}_scm_iv")
    g_idc = float(np.interp(vc, gvo, gI)); m_idc = float(np.interp(vc, mvo, mI))
    idc_err = abs(m_idc - g_idc) / g_idc
    plateau = gI > 0.5 * gI.max()
    mI_on = np.interp(gvo, mvo, mI)
    iv_rms = float(np.sqrt(np.mean(((mI_on[plateau] - gI[plateau]) / gI[plateau]) ** 2)))
    g_glf = complex(d["g_lf"]); m_glf = psrr(va, vc, f"{name}_scm_p")
    sign_ok = (np.sign(g_glf.real) == np.sign(m_glf.real)) or (abs(g_glf.real) < 1e-12)
    ptat_err = float("nan")
    if do_temp:
        m_t = np.array([idc_at(va, vc, f"{name}_scm_t{int(T)}", T) for T in TEMPS])
        g_t = np.asarray(d["idcT"])
        ptat_err = abs(m_t[-1] / m_t[0] - g_t[-1] / g_t[0])
    ok = idc_err < 0.03 and iv_rms < 0.06 and sign_ok and (np.isnan(ptat_err) or ptat_err < 0.05)
    return dict(name=name, pol=p["pol"], idc_err=idc_err, iv_rms=iv_rms,
                g_glf=g_glf.real, m_glf=m_glf.real, sign_ok=sign_ok, ptat_err=ptat_err, ok=ok)


def main_sc(names=None, temp_for=("v6_ptat",)):
    names = names or list(VARIANTS)
    rows = []
    for n in names:
        r = selfconsistent(n, do_temp=(n in temp_for))
        rows.append(r)
        print(f"  [{'ok ' if r['ok'] else 'NO '}] {n}", file=sys.stderr)
    print("\n=== LOCAL SPECTRE, SELF-CONSISTENT: char(Spectre)->fit->VA->validate(Spectre) ===\n")
    hdr = f"{'variant':<17}{'pol':<7}{'Idc err':>9}{'IV rms':>9}{'GT dId/dVdd':>13}{'MD dId/dVdd':>13}{'sign':>6}{'PTAT err':>10}{'PASS':>6}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        pe = "  -  " if np.isnan(r["ptat_err"]) else f"{r['ptat_err']:.3f}"
        print(f"{r['name']:<17}{r['pol']:<7}{r['idc_err']*100:>8.2f}%{r['iv_rms']*100:>8.2f}%"
              f"{r['g_glf']*1e9:>12.2f}n{r['m_glf']*1e9:>12.2f}n{('ok' if r['sign_ok'] else 'FLIP'):>6}"
              f"{pe:>10}{('yes' if r['ok'] else 'NO'):>6}")
    print(f"\n{sum(r['ok'] for r in rows)}/{len(rows)} archetypes: VA model matches MOS-GT "
          "in Spectre (self-consistent).")
    return rows


def main(names=None, temp_for=("v6_ptat",)):
    names = names or list(VARIANTS)
    rows = []
    for n in names:
        r = compare(n, do_temp=(n in temp_for))
        rows.append(r)
        print(f"  [{'ok ' if r['ok'] else 'NO '}] {n}", file=sys.stderr)
    print("\n=== LOCAL SPECTRE: behavioral VA model vs MOS-GT (same probe) ===\n")
    hdr = f"{'variant':<17}{'pol':<7}{'Idc err':>9}{'IV rms':>9}{'GT dId/dVdd':>13}{'MD dId/dVdd':>13}{'sign':>6}{'PTAT err':>10}{'PASS':>6}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        pe = "  -  " if np.isnan(r["ptat_err"]) else f"{r['ptat_err']:.3f}"
        print(f"{r['name']:<17}{r['pol']:<7}{r['idc_err']*100:>8.2f}%{r['iv_rms']*100:>8.2f}%"
              f"{r['g_glf']*1e9:>12.2f}n{r['m_glf']*1e9:>12.2f}n{('ok' if r['sign_ok'] else 'FLIP'):>6}"
              f"{pe:>10}{('yes' if r['ok'] else 'NO'):>6}")
    npass = sum(r["ok"] for r in rows)
    print(f"\n{npass}/{len(rows)} archetypes: VA model matches MOS-GT in Spectre.")
    return rows


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--sc":
        main_sc(args[1:] or None)
    else:
        main(args or None)
