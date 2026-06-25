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
from fit_isrc import fit_isrc, TNOM, _y_rms_db, predict_y             # noqa: E402
from isrc_char import TEMPS, HELDOUT_IDC_TEMPS                         # noqa: E402
from emit_isrc import emit_isrc                                        # noqa: E402

# Observational gate thresholds (the adversarial overfit probes -- HANDOFF_ADVERSARIAL_OVERFIT_
# PROBE.md §C). These flag EXPOSED; they do NOT change the existing scalar PASS gate `ok`, so the 8
# baselines stay 8/8 and double as the false-positive control (a real gate must NOT fire on them).
G_IDC_HELD_PCT = 5.0    # B1: |model-GT| Idc at an interior temp > this % -> temp-curvature exposed
G_Y_RMS_DB = 1.0        # B2: band |Y| dB-RMS (model vs GT) > this AND the GT Re(Y) has >=2 SEPARATED
                        # rising steps (zeros) -- a single zero-pole structurally cannot hold two.
                        # The step count (not raw rms) is what discriminates a 2nd zero from the
                        # generic source-Y residual that fires rms on v4/v5 (both have ONE step).
G_IV_TEMP_PCT = 5.0     # B4: near-knee model IV(Vo,T) error % (with a moving knee) -> T x compliance
G_KNEE_SHIFT_MV = 40.0  # B4: GT compliance-knee movement across the fit temps to call it a real x-term
                        # (benign mirrors drift ~7-11 mV; a real T-knee shift is >100 mV -> 40 mV splits)

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


def model_y_curve(name, sub, lib, vc):
    """Full output admittance Y(s)=i(out)/v(out) of the EMITTED model on the @y grid."""
    body = _head(sub, temp=TNOM) + (f"Vout out 0 DC {vc:g} AC 1\n.control\n"
                                    f"ac dec 20 10 500meg\nwrdata y.data i(vout)\n.endc\n.end\n")
    a = _run(name, "yc", lib, body, "y.data")
    return a[:, 0], a[:, 1] + 1j * a[:, 2]


# --------------------------------------------------------- ADVERSARIAL OVERFIT-PROBE GATES (§C)
# Each re-simulates the EMITTED model (adversarial verify -> attribute a miss to the MODEL, not a
# harness artifact) and compares to the GT held-out grids captured by isrc_char. They return a
# metric + an `exposed` flag; OBSERVATIONAL ONLY (never folded into the `ok` PASS verdict). When the
# npz predates the held-out capture the gate is skipped (exposed=False, note set).
def gate_heldout_idc(name, sub, lib, vc, d):
    """B1: model Idc at the interior held-out temps (25/85C) vs GT -> 3-temp-fit curvature miss."""
    if "idcT_held" not in d:
        return dict(metric=None, exposed=False, note="no idcT_held in npz")
    gt = np.asarray(d["idcT_held"], float)
    errs = []
    for T, g in zip(HELDOUT_IDC_TEMPS, gt):
        vo, I = model_iv(name, sub, lib, vc, temp=T)
        m = float(np.interp(vc, vo, I))
        errs.append(abs(m - g) / (abs(g) + 1e-30) * 100)
    worst = float(max(errs))
    return dict(metric=worst, exposed=worst > G_IDC_HELD_PCT,
                detail={int(T): float(e) for T, e in zip(HELDOUT_IDC_TEMPS, errs)})


def _count_y_zero_steps(f, Y, peak_hi=0.8, valley_frac=0.6, min_sep_dec=0.7):
    """Count the SEPARATED admittance zeros in GT Re(Y). A zero is a RISING step in Re(Y) -> a local
    PEAK in the per-decade slope of log|Re(Y)|. The double-cascode shows TWO slope peaks separated by
    a valley; a single cascode/source ONE broad peak. Count slope peaks above peak_hi that are
    >min_sep_dec apart AND separated by a valley dropping below valley_frac of the smaller peak -- so
    two zeros whose slope never fully relaxes between them (the realized 1.2e5/1.3e7, valley ~0.45)
    still count as 2, while one broad cascode rise counts as 1. Band-edge peaks (top 0.3 decade, the
    numerical Cp tail) are dropped. Returns (n_steps, peak_freqs)."""
    f = np.asarray(f, float); re = np.abs(np.asarray(Y, complex).real) + 1e-30
    lf = np.log10(f)
    slope = np.gradient(np.convolve(np.log10(re), np.ones(5) / 5.0, "same"), lf)
    n = len(slope)
    peaks = [i for i in range(2, n - 2)
             if slope[i] > peak_hi and slope[i] >= slope[i - 1] and slope[i] >= slope[i + 1]
             and lf[i] < lf[-1] - 0.3]
    kept = []
    for i in peaks:
        if not kept:
            kept.append(i)
            continue
        j = kept[-1]
        valley = float(slope[j:i + 1].min())
        if (lf[i] - lf[j]) > min_sep_dec and valley < valley_frac * min(slope[i], slope[j]):
            kept.append(i)                                # a genuinely separate zero
        elif slope[i] > slope[j]:
            kept[-1] = i                                 # same step -> track the taller peak
    return len(kept), [float(10 ** lf[i]) for i in kept]


def gate_y_rms(name, sub, lib, vc, d, p=None):
    """B2: band |Y| dB-RMS, MODEL admittance vs GT -> a single zero-pole cannot hold two zeros.

    Uses the model's analytic admittance predict_y(p) -- the form the DELIVERABLE (Cadence VA) emit
    realizes with a physical internal node. We do NOT re-sim the offline emit_isrc here: that ngspice
    twin deliberately omits the fitted pole-zero (it emits only g0+sCp), so re-simming it would test
    the offline emit's completeness, not the FIT's representational limit -- and would false-positive
    on every baseline that has even ONE real cascode/loop zero (e.g. v6_ptat: predict_y nails it to
    0.08 dB, the offline emit misses it by 7 dB). predict_y carries y_wz/y_wp -> the honest B2 test."""
    if "ac_y" not in d or "ac_f" not in d:
        return dict(metric=None, exposed=False, note="no @y in npz")
    gtf = np.asarray(d["ac_f"], float)
    gty = np.atleast_1d(np.asarray(d["ac_y"], complex))
    if gty.shape != gtf.shape or gtf.size < 6:
        return dict(metric=None, exposed=False, note="degenerate @y")
    if p is None:
        p = fit_isrc(d if hasattr(d, "keys") else None)
    ym = predict_y(p, gtf)
    rms = _y_rms_db(ym, gty)
    dberr = 20 * np.log10((np.abs(ym) + 1e-30) / (np.abs(gty) + 1e-30))
    fworst = float(gtf[int(np.argmax(np.abs(dberr)))])
    # discriminator: the GT must have >=2 SEPARATED admittance zeros (rising steps in Re(Y)) -- the
    # thing a single zero-pole structurally cannot hold. Generic source-Y residual (v4/v5) has ONE
    # step -> high rms but NOT exposed; the double-cascode has two.
    n_steps, intervals = _count_y_zero_steps(gtf, gty)
    return dict(metric=float(rms), n_zero_steps=int(n_steps), fworst=fworst,
                exposed=(rms > G_Y_RMS_DB and n_steps >= 2))


def _psrr_offvc_exposed(g_gt_lo, g_gt_hi, g_md_lo, g_md_hi, tol=1e-12):
    """Pure decision for the B3 gate (factored for the synthetic lock test). Inputs are LF
    dIout/dVdd real parts at vc-delta / vc+delta for the GT and the model. EXPOSED iff the GT sign
    genuinely flips across compliance AND the single-gdd model gets >=1 off-point sign wrong.
    Returns (gt_flips, n_wrong, exposed)."""
    gt_flips = (np.sign(g_gt_lo) != np.sign(g_gt_hi)) and abs(g_gt_lo) > tol and abs(g_gt_hi) > tol
    n_wrong = int(np.sign(g_md_lo) != np.sign(g_gt_lo)) + int(np.sign(g_md_hi) != np.sign(g_gt_hi))
    return bool(gt_flips), n_wrong, bool(gt_flips and n_wrong >= 1)


def gate_psrr_offvc(name, sub, lib, vc, d):
    """B3: off-compliance LF dIout/dVdd SIGN. Exposed iff the GT sign genuinely flips across
    [vc-0.2, vc+0.2] AND the single-gdd model gets >=1 off-point sign wrong (while in-vc sign_ok)."""
    if "g_lf_lo" not in d or "g_lf_hi" not in d:
        return dict(metric=None, exposed=False, note="no off-vc PSRR in npz")
    vlo = float(d.get("vc_lo", max(0.0, vc - 0.2)))
    vhi = float(d.get("vc_hi", min(VDD, vc + 0.2)))
    g_gt_lo = complex(d["g_lf_lo"]); g_gt_hi = complex(d["g_lf_hi"])
    g_md_lo = model_psrr(name, sub, lib, vlo)
    g_md_hi = model_psrr(name, sub, lib, vhi)
    gt_flips, wrong, exposed = _psrr_offvc_exposed(
        g_gt_lo.real, g_gt_hi.real, g_md_lo.real, g_md_hi.real)
    return dict(metric=wrong, exposed=exposed, gt_flips=gt_flips,
                detail=dict(gt_lo=g_gt_lo.real, gt_hi=g_gt_hi.real,
                            md_lo=g_md_lo.real, md_hi=g_md_hi.real))


def _knee_vo(vo, I):
    """Compliance-knee Vo = the 0.5*plateau crossing nearest the steepest |dI/dVo| (the saturation
    <->triode transition), linearly interpolated for sub-grid resolution (the knees sit on a 5 mV
    sweep grid but move by <15 mV on benign mirrors, so grid quantization would mask the signal).
    Side-agnostic: works for a low-side NMOS-sink knee AND a high-side PMOS-source/ceiling knee."""
    vo = np.asarray(vo, float); I = np.asarray(I, float)
    if vo.size < 3:
        return float(vo[0]) if vo.size else 0.0
    half = 0.5 * float(np.median(np.sort(I)[-8:]))
    gidx = int(np.argmax(np.abs(np.gradient(I, vo))))
    cross = np.where(np.diff(np.sign(I - half)))[0]
    if cross.size == 0:
        return float(vo[gidx])
    j = int(cross[int(np.argmin(np.abs(cross - gidx)))])     # crossing nearest the steepest point
    y0, y1 = I[j], I[j + 1]
    return float(vo[j] + (half - y0) * (vo[j + 1] - vo[j]) / (y1 - y0 + 1e-30))


def _knee_shift_mv(gtv, gti):
    """GT compliance-knee movement [mV] across the temp columns of an IV(Vo,T) grid [nVo, nT]."""
    vk = [_knee_vo(gtv, gti[:, k]) for k in range(gti.shape[1])]
    return float((max(vk) - min(vk)) * 1e3), vk


def gate_iv_temps(name, sub, lib, vc, d):
    """B4: a T-MOVING compliance knee a T-independent-knee model cannot bend. The whole-plateau RMS
    is dominated by the generic rout-tilt-vs-T drift (fires on benign mirrors), so we key on the
    SIGNATURE the spec actually targets: the GT knee MOVES with T while Idc@vc stays flat. Metric =
    GT knee shift [mV]; exposed iff the knee moves > G_KNEE_SHIFT_MV AND the emitted (fixed-knee)
    model mis-predicts in the near-knee window > G_IV_TEMP_PCT (so the miss is real + observable)."""
    if "iv_i_temps" not in d:
        return dict(metric=None, exposed=False, note="no iv_i_temps in npz")
    gtv = np.asarray(d["iv_v"], float)
    gti = np.asarray(d["iv_i_temps"], float)                 # [nVo, nT]
    knee_shift, vk_gt = _knee_shift_mv(gtv, gti)
    near_err = {}
    for k, T in enumerate(TEMPS):
        g = gti[:, k]; plat = g.max()
        win = (gtv >= vk_gt[k] - 0.05) & (gtv <= vk_gt[k] + 0.15) & (g > 0.05 * plat)
        if not win.any():
            near_err[int(T)] = 0.0
            continue
        mv, mi = model_iv(name, sub, lib, vc, temp=T)
        mion = np.interp(gtv, mv, mi)
        near_err[int(T)] = float(np.sqrt(np.mean(
            ((mion[win] - g[win]) / (g[win] + 1e-30)) ** 2)) * 100)
    worst_near = float(max(near_err.values()))
    exposed = knee_shift > G_KNEE_SHIFT_MV and worst_near > G_IV_TEMP_PCT
    return dict(metric=knee_shift, near_knee_err=worst_near, exposed=exposed,
                vk_gt=[round(v, 4) for v in vk_gt], detail=near_err)


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
    # OBSERVATIONAL adversarial-probe gates (never change `ok`; see §C). They re-sim the emitted
    # model vs the held-out GT grids and flag the blind spots the scalar PASS gate cannot see.
    g_b1 = gate_heldout_idc(name, sub, lib, vc, d)
    g_b2 = gate_y_rms(name, sub, lib, vc, d, p=p)
    g_b3 = gate_psrr_offvc(name, sub, lib, vc, d)
    g_b4 = gate_iv_temps(name, sub, lib, vc, d)
    return dict(name=name, pol=p["pol"], idc_err=idc_err, iv_rms=iv_rms, rout_err=rout_err,
                sign_ok=sign_ok, gdd_err=gdd_err, ptat_err=ptat_err, ok=ok,
                gates=dict(heldout_idc=g_b1, y_rms=g_b2, psrr_offvc=g_b3, iv_temps=g_b4))


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

    # ---- observational adversarial-probe gates (held-out; NOT part of the PASS verdict) ----
    print("\n=== adversarial overfit-probe gates (held-out; observational -- do NOT change PASS) ===")
    print("    B1=interior-temp Idc miss%  B2=band|Y|dB(#steps)  B3=off-vc PSRR sign  B4=knee shift[mV]")
    gh = f"{'variant':<26}{'B1 idc%':>9}{'B2 ydB/#z':>11}{'B3 flip':>9}{'B4 dVk':>9}   exposed"
    print(gh); print("-" * len(gh))
    for r in rows:
        g = r["gates"]
        b1, b2, b3, b4 = g["heldout_idc"], g["y_rms"], g["psrr_offvc"], g["iv_temps"]
        def _m(x):
            return "  -  " if x["metric"] is None else f"{x['metric']:.2f}"
        b2s = "  -  " if b2["metric"] is None else f"{b2['metric']:.2f}/{b2.get('n_zero_steps', 0)}"
        exposed = [k for k, gg in (("B1", b1), ("B2", b2), ("B3", b3), ("B4", b4)) if gg["exposed"]]
        print(f"{r['name']:<26}{_m(b1):>9}{b2s:>11}"
              f"{('yes' if b3['exposed'] else ('flip?' if b3.get('gt_flips') else 'no')):>9}"
              f"{_m(b4):>9}   {','.join(exposed) if exposed else '-'}")
    return rows


if __name__ == "__main__":
    main()
