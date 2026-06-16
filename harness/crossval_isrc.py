"""Cross-validate the BEHAVIORAL current-source model against the MOS-transistor
GT: emit each model, RE-SIMULATE it under the same testbenches, and compare to
the GT npz. The point of the >=8 diverse archetypes is anti-overfit -- ONE model
template must reproduce ALL of them, not just one. Reports per-variant error on
Idc / I-V curve / rout / PSRR(sign+mag) / PTAT, and a PASS gate.

`python crossval_isrc.py`   (needs ngspice on PATH + work_isrc/*.npz from isrc_char).
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ng                                                              # noqa: E402
from isrc_variants import VARIANTS, VDD                                # noqa: E402
from fit_isrc import fit_isrc, TNOM                                    # noqa: E402
from isrc_char import TEMPS                                            # noqa: E402
from emit_isrc import emit_isrc                                        # noqa: E402

WORK = ng.ROOT / "work_isrc"
MODELDIR = WORK / "models"


def _run(name, tag, lib, body, outfile):
    r = ng.run(ng.assemble(body, libs=[lib]), WORK / "_xv" / name / tag, outputs=(outfile,))
    assert r["_rc"] == 0 and r.get(outfile) is not None, \
        f"{name}/{tag}: ngspice failed:\n{r['_stderr'][-900:]}"
    return r[outfile][1]


def _head(sub, temp):
    return f".options temp={temp:g}\nVdd vdd 0 DC {VDD}\nXm vdd out {sub}\n"


def model_iv(name, sub, lib, vc, temp=TNOM):
    body = _head(sub, temp) + (f"Vout out 0 DC {vc:g}\n.control\n"
                               f"dc Vout 0 {VDD} 0.005\nwrdata iv.data i(vout)\n.endc\n.end\n")
    a = _run(name, "iv", lib, body, "iv.data")
    return a[:, 0], np.abs(a[:, 1])


def model_y(name, sub, lib, vc):
    body = _head(sub, temp=TNOM) + (f"Vout out 0 DC {vc:g} AC 1\n.control\n"
                                    f"ac dec 20 10 500meg\nwrdata y.data i(vout)\n.endc\n.end\n")
    a = _run(name, "y", lib, body, "y.data")
    y0 = a[0, 1] + 1j * a[0, 2]
    return 1.0 / abs(y0.real)


def model_psrr(name, sub, lib, vc):
    head = _head(sub, temp=TNOM).replace(f"Vdd vdd 0 DC {VDD}\n", f"Vdd vdd 0 DC {VDD} AC 1\n")
    body = head + (f"Vout out 0 DC {vc:g} AC 0\n.control\n"
                   f"ac dec 20 10 500meg\nwrdata p.data i(vout)\n.endc\n.end\n")
    a = _run(name, "psrr", lib, body, "p.data")
    return a[0, 1] + 1j * a[0, 2]


def model_idcT(name, sub, lib, vc):
    out = []
    for T in TEMPS:
        vo, I = model_iv(name, sub, lib, vc, temp=T)
        out.append(float(np.interp(vc, vo, I)))
    return np.array(out)


def crossval(name):
    d = np.load(WORK / f"{name}.npz", allow_pickle=True)
    p = fit_isrc(WORK / f"{name}.npz")
    sub = f"isrc_model_{name}"
    lib = MODELDIR / f"{name}.lib"
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text(emit_isrc(p))
    vc = p["vc"]

    # GT references
    gt_idc = float(d["idc"]); gt_rout = float(d["rout"]); gt_glf = complex(d["g_lf"])
    gt_iv_v = np.asarray(d["iv_v"]); gt_iv_i = np.asarray(d["iv_i"]); gt_idcT = np.asarray(d["idcT"])

    mv, mi = model_iv(name, sub, lib, vc)
    m_idc = float(np.interp(vc, mv, mi))
    m_rout = model_y(name, sub, lib, vc)
    m_glf = model_psrr(name, sub, lib, vc)
    m_idcT = model_idcT(name, sub, lib, vc)

    # errors
    idc_err = abs(m_idc - gt_idc) / gt_idc
    plateau = gt_iv_i > 0.5 * gt_iv_i.max()                 # compare where there IS current
    mi_on = np.interp(gt_iv_v, mv, mi)
    iv_rms = float(np.sqrt(np.mean(((mi_on[plateau] - gt_iv_i[plateau]) / gt_iv_i[plateau]) ** 2)))
    rout_err = abs(m_rout - gt_rout) / gt_rout
    sign_ok = (np.sign(m_glf.real) == np.sign(gt_glf.real)) or (abs(gt_glf.real) < 1e-12)
    gdd_err = abs(m_glf.real - gt_glf.real)                 # absolute [S]
    ptat_m = m_idcT[-1] / m_idcT[0]; ptat_g = gt_idcT[-1] / gt_idcT[0]
    ptat_err = abs(ptat_m - ptat_g)
    ok = idc_err < 0.02 and iv_rms < 0.05 and rout_err < 0.20 and sign_ok and ptat_err < 0.03
    return dict(name=name, pol=p["pol"], idc_err=idc_err, iv_rms=iv_rms, rout_err=rout_err,
                sign_ok=sign_ok, gdd_err=gdd_err, ptat_err=ptat_err, ok=ok)


def main():
    rows = [crossval(n) for n in VARIANTS]
    print("\n=== behavioral model vs MOS-GT cross-validation "
          "(one template, all 8 archetypes -> anti-overfit) ===\n")
    hdr = f"{'variant':<17}{'pol':<7}{'Idc err':>9}{'IV rms':>9}{'rout err':>10}{'PSRRsign':>10}{'PTAT err':>10}{'PASS':>6}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['name']:<17}{r['pol']:<7}{r['idc_err']*100:>8.2f}%{r['iv_rms']*100:>8.2f}%"
              f"{r['rout_err']*100:>9.1f}%{('ok' if r['sign_ok'] else 'FLIP'):>10}"
              f"{r['ptat_err']:>10.3f}{('yes' if r['ok'] else 'NO'):>6}")
    npass = sum(r["ok"] for r in rows)
    print(f"\n{npass}/{len(rows)} archetypes reproduced by the single behavioral template.")
    return rows


if __name__ == "__main__":
    main()
