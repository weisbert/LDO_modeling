"""Score a candidate LDO model against the ground-truth reference. THIS is the
feedback loop. Runs the SAME Zout/PSRR/noise/transient stimuli on the candidate
that gen_reference ran on the GT, then reports error metrics + overlay plots.

Metrics (per load corner unless noted):
  Zout : magnitude RMS + band err(8/16/24MHz) + resonance peak df/dB + PHASE RMS
  PSRR : band err + worst-notch + PHASE RMS
  Trans: load-step droop err, settled err, waveform RMS, ring-freq err  (req#1)
  Noise: output-PSD log-RMS + resonance-peak err + integrated-rms err     (req#2)
  Spur : SANITY gate (16/24MHz dBc) + discrete-tone amplitude AND PHASE fidelity
  HF   : |Zout|/PSRR RMS in the *_hf extension band (above the in-band AC top,
         nominal corner) -- only when the ref carries the *_hf arrays
All phase errors are WRAPPED to +-180deg (angle of the model/GT ratio).

    python score.py                       # grade model/ldo_model.lib
    python score.py --lib path --subckt name
"""
import argparse
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import bench
import ng

REF = ng.ROOT / "results" / "ref" / "gt_ref.npz"
SCOREDIR = ng.ROOT / "results" / "score"
BANDS = bench.SPUR_BANDS
_TRAP = getattr(np, "trapezoid", None) or np.trapz
# composite weights (documented, transparent; lower composite = better)
# spurph: discrete-spur PHASE (deg) -- HB sidebands superpose coherently, so spur
#         phase is as load-bearing as amplitude (same per-degree weight as pphase).
# zhf/phf: |Zout|/PSRR error in the *_hf EXTENSION band (above the in-band AC top,
#         up to the variant's hf_stop ceiling). Scored only when the reference
#         carries the *_hf arrays -- closes the "composite is blind to HF" gap
#         (v7 inductive tail / v8 notch used to be invisible here).
W = dict(zrms=1.0, zband=3.0, zphase=0.04, pkdb=1.0, pband=2.0,
         pphase=0.03, trms=0.2, noise=0.5, spur=0.5, spurph=0.03,
         zhf=0.5, phf=0.5, sspur=0.5, npk=0.1, nir_lf=0.01)
# npk/nir_lf: noise-resonance matched-freq error (dB) + LF (10Hz-100kHz, datasheet uVrms
#        window) integrated-RMS error (%), folded into the composite at LOW weight with
#        saturation (NPK_CAP/NIR_LF_CAP) so a DESIGNED order-limit (v8_dlc/v10_3lc In-notch)
#        nudges the score but never dominates it. The full-band ir_pct stays diagnostic-only.
NPK_CAP = 6.0      # dB saturation on the folded noise-resonance term
NIR_LF_CAP = 50.0  # % saturation on the folded LF integrated-RMS term
# sspur: SUPPLY-spur rejection error (dB) at the AVDD aggressor tones (DC-DC comb + ref
#        clock). Folds into the composite ONLY when the ref carries the collected
#        supply_spur_* arrays (regenerate the reference to enable); legacy refs still get
#        the scorecard line, derived from p_* -- see _supply_spur_metrics.


def _interp_mag(f_to, f_from, mag):
    return np.exp(np.interp(np.log(f_to), np.log(f_from), np.log(mag + 1e-30)))


def _interp_cplx(f_to, f_from, Z):
    mag = _interp_mag(f_to, f_from, np.abs(Z))
    ph = np.interp(np.log(f_to), np.log(f_from), np.unwrap(np.angle(Z)))
    return mag * np.exp(1j * ph)


def _band_err(f, err_db):
    out = {}
    for fb in BANDS:
        m = (f >= 0.9 * fb) & (f <= 1.1 * fb)
        out[fb] = float(np.mean(np.abs(err_db[m]))) if m.any() else np.nan
    return out


def _atten(H):
    return -20 * np.log10(np.abs(H) + 1e-30)


def _passivity(f, Z):
    """1-port immittance passivity via scikit-rf. Passive <=> Re(Z(jw))>=0 <=> |S11|<=1
    for any z0>0 (S11=(Z-z0)/(Z+z0)). Returns (is_passive, min_ReZ, max|S11|).
    The SYNTH model must be passive (HB convergence guardrail); the GT may legitimately
    be NON-passive (a regulated LDO actively sources/sinks => Re(Z)<0 in band), so GT
    passivity is reported as DIAGNOSTIC only, never used to reject the reference."""
    try:
        import skrf
        z0 = max(float(np.median(np.abs(Z))), 1.0)
        s11 = ((Z - z0) / (Z + z0)).reshape(-1, 1, 1)
        ntwk = skrf.Network(frequency=skrf.Frequency.from_f(f, unit="Hz"), s=s11, z0=z0)
        maxs = float(np.max(np.abs(ntwk.s.flatten())))
    except Exception:                       # skrf missing -> fall back to the Re(Z) test
        maxs = float(np.max(np.abs((Z - 1.0) / (Z + 1.0))))  # placeholder magnitude
        return bool(np.min(Z.real) >= -1e-3), float(np.min(Z.real)), maxs
    return bool(maxs <= 1.0 + 1e-6), float(np.min(Z.real)), maxs


def _trans_metrics(t, vg, vm):
    preg = vg[(t > 3e-6) & (t < 5e-6)].mean()
    prem = vm[(t > 3e-6) & (t < 5e-6)].mean()
    win = (t >= 5e-6) & (t < 13e-6)
    droopg, droopm = preg - vg[win].min(), prem - vm[win].min()
    setg = vg[(t > 13e-6) & (t < 15e-6)].mean() - preg
    setm = vm[(t > 13e-6) & (t < 15e-6)].mean() - prem
    aw = (t >= 5e-6) & (t < 25e-6)
    eg, em = vg[aw] - preg, vm[aw] - prem
    wrms = np.sqrt(np.mean((em - eg) ** 2)) / (np.sqrt(np.mean(eg ** 2)) + 1e-15) * 100
    return dict(droopg=droopg * 1e3, droopm=droopm * 1e3,
                drerr=(droopm - droopg) * 1e3, seterr=(setm - setg) * 1e3,
                wrms=wrms, ringg=bench.ring_freq(t, vg), ringm=bench.ring_freq(t, vm))


def _noise_metrics(fn, Sg, fm, Sm, fres=None):
    Smi = _interp_mag(fn, fm, Sm)
    db = 20 * np.log10((Smi + 1e-18) / (Sg + 1e-18))
    band = (fn >= 10) & (fn <= 100e6)
    # R1: GT-PSD-ENERGY-WEIGHTED log-RMS. The flat unweighted average over 7 decades let a
    # deep post-rolloff numerical-floor tail (~0.3% of the integrated noise energy) dominate
    # the headline and over-penalize a fine in-band model ~6x. Weighting by Sg^2 suppresses
    # that tail (THE win) -- measured, the Sg^2 weight sits ~entirely in the LF energy-bearing
    # decades, so psd_rms becomes an LF-energy-dominated dB metric. The FULL band is kept (not
    # capped at 1MHz) only because the weighting already starves the HF floor so a cap buys
    # nothing; resonance sensitivity is INTENTIONALLY delegated to the npk term below, not here.
    w = Sg[band] ** 2
    w = w / (w.sum() + 1e-300)
    psd_rms = float(np.sqrt(np.sum(w * db[band] ** 2)))
    # R2: FREQUENCY-AWARE resonance metric, ANCHORED TO THE GT Zout-PEAK FREQUENCY fres (the
    # LC resonance location). This replaces the fixed 0.5-3MHz window (v2_capless's 4-8MHz
    # resonance fell outside it). The Zout anchor is robust where an argmax(Sv) anchor is not:
    # when the 1/f tail dominates Sv, argmax(Sv) latches the band bottom, not the resonance.
    # pkdb = MATCHED-FREQUENCY model-vs-GT Sv error at fres, so a correct-height peak at the
    # WRONG frequency no longer scores ~0 dB. On v8_dlc the anchor lands on the dominant LC
    # resonance PEAK (~0.75MHz) and pkdb measures reproduction of that peak HEIGHT (bounded by
    # NPK_CAP); the 9.4MHz anti-resonance notch is a separate Zout feature (zrms/zband/zhf), not
    # this term. npkf = model Sv-peak freq within +-half-decade of fres, over fres (1.0 =
    # aligned OR no resolvable peak), mirroring the Zout pkfratio; diagnostic-only (never in the
    # composite). fres=None -> fall back to the in-band Sv local peak (callers with no Zout grid).
    if fres is not None and np.isfinite(fres) and fres > 0:
        i0 = int(np.argmin(np.abs(fn - fres)))
    else:
        sb = np.where((fn >= 2e5) & (fn <= 5e7))[0]
        i0 = int(sb[int(np.argmax(Sg[sb]))]) if sb.size else int(np.argmax(Sg))
    f0 = float(fn[i0])
    pkdb = float(20 * np.log10((Smi[i0] + 1e-18) / (Sg[i0] + 1e-18)))
    jdx = np.where((fn >= f0 / 3.1623) & (fn <= f0 * 3.1623))[0]   # +-half decade around fres
    if jdx.size >= 3:
        a = int(np.argmax(Smi[jdx]))
        # an INTERIOR max is a genuine resonance; a max pinned to the window edge means Sv is
        # monotone across the window (no resolvable peak) -> report 1.0 (diagnostic, never scored).
        npkf = float(fn[int(jdx[a])] / f0) if 0 < a < jdx.size - 1 else 1.0
    else:
        npkf = 1.0
    irg = np.sqrt(_TRAP(Sg[band] ** 2, fn[band]))
    irm = np.sqrt(_TRAP(Smi[band] ** 2, fn[band]))
    # R3 input: integrated-RMS over the LF datasheet window (10Hz-100kHz, the uVrms a designer
    # signs off on) -- folded into the composite at low weight. It adds an integrated-uVrms
    # (linear) flavor over a band that overlaps the (now LF-dominated) psd_rms near-totally, so
    # its weight is kept tiny. The full-band ir_pct stays diagnostic-only (blind to localized
    # errors; redundant with psd_rms).
    lf = (fn >= 10) & (fn <= 1e5)
    irg_lf = float(np.sqrt(_TRAP(Sg[lf] ** 2, fn[lf]))) if lf.any() else 0.0
    if irg_lf > 0:
        irm_lf = float(np.sqrt(_TRAP(Smi[lf] ** 2, fn[lf])))
        ir_lf = float((irm_lf / irg_lf - 1) * 100)
    else:
        ir_lf = 0.0
    return dict(psd_rms=psd_rms, pkdb=pkdb, npkf=npkf,
                ir_pct=float((irm / irg - 1) * 100), ir_lf=ir_lf,
                Smi=Smi, fn=fn, Sg=Sg)


def score(lib, subckt, xp="", refpath=None):
    refpath = refpath or REF
    ref = np.load(refpath, allow_pickle=True)
    loads = [str(x) for x in ref["loads"]]
    rows = []
    for il in loads:
        xpil = (xp + f" iload={il}").strip()
        # ---- Zout (mag + phase) ----
        gz = ref[f"z_{il}"]
        fz, Zg = gz[:, 0], gz[:, 1] + 1j * gz[:, 2]
        fm, Zm = bench.measure_zout(lib, subckt, il, xparams=xpil)
        Zmi = _interp_cplx(fz, fm, Zm)
        ez = 20 * np.log10(np.abs(Zmi) / np.abs(Zg))
        # phase error WRAPPED to +-180deg (angle of the ratio): raw principal-value
        # subtraction reads a true 2deg error as 358deg when GT/model straddle the
        # +-180 boundary (report.py/_wrapdeg and fit_all's pph already wrap).
        ezph = np.degrees(np.angle(Zmi / Zg))
        lo = fz < 1e7
        ipg, ipm = np.argmax(np.abs(Zg) * lo), np.argmax(np.abs(Zmi) * lo)
        # ---- PSRR (mag + phase) ----
        gp = ref[f"p_{il}"]
        fp, Hg = gp[:, 0], gp[:, 1] + 1j * gp[:, 2]
        fpm, Hm = bench.measure_psrr(lib, subckt, il, xparams=xpil)
        Hmi = _interp_cplx(fp, fpm, Hm)
        ag, am = _atten(Hg), np.clip(_atten(Hmi), None, 200)
        ep = am - ag
        epph = np.degrees(np.angle(Hmi / Hg))   # wrapped (see ezph above)
        # ---- Noise ----
        gn = ref[f"noise_{il}"]
        fnm, Sm = bench.measure_noise(lib, subckt, il, xparams=xpil)
        # anchor the resonance metric to the GT Zout-peak freq (ipg, computed above)
        nm = _noise_metrics(gn[:, 0], gn[:, 1], fnm, Sm, fres=float(fz[ipg]))
        # ---- Transient (linear step, proportional to bias) ----
        tg = ref[f"trans_lin_{il}"]
        t, vg = tg[:, 0], tg[:, 1]
        base = ng.amps(il)
        _, vm = bench.measure_loadstep(lib, subckt, bench.LIN_FRAC * base,
                                       iload=base, xparams=xpil)
        tm = _trans_metrics(t, vg, vm)
        zb, pb = _band_err(fz, ez), _band_err(fp, ep)
        bandsel = (fz >= 1e3)
        # passivity GUARDRAIL on the synthesized Zout (must be passive for HB robustness);
        # GT passivity reported as a diagnostic (LDO Zout can be actively non-passive).
        zp_m = _passivity(fz[bandsel], Zmi[bandsel])
        zp_g = _passivity(fz[bandsel], Zg[bandsel])
        rows.append(dict(il=il, fz=fz, Zg=np.abs(Zg), Zm=np.abs(Zmi), ez=ez, ezph=ezph,
                         fp=fp, ag=ag, am=am, ep=ep, epph=epph,
                         zmax=np.max(np.abs(ez)), zrms=np.sqrt(np.mean(ez[bandsel]**2)),
                         zphase=np.sqrt(np.mean(ezph[bandsel]**2)),
                         zband=np.nanmean(list(zb.values())), zb=zb,
                         pkfratio=fz[ipm]/fz[ipg], pkdb=20*np.log10(np.abs(Zmi)[ipm]/np.abs(Zg)[ipg]),
                         pband=np.nanmean(list(pb.values())), pb=pb,
                         pphase=np.sqrt(np.mean(epph[bandsel]**2)),
                         zpass_m=zp_m, zpass_g=zp_g, nm=nm, tm=tm))
    # big/slew transient at 121u
    extra = {}
    for tag in ("big", "slew"):
        key = f"trans_{tag}_121u"
        if key in ref.files:
            d = ref[key]
            t, vg = d[:, 0], d[:, 1]
            # large-signal steps use the nonlinear core (slew_en=1)
            xp121 = (xp + " iload=121u slew_en=1").strip()
            _, vm = bench.measure_loadstep(lib, subckt, bench.STEP_DI[tag], iload=121e-6, xparams=xp121)
            extra[tag] = _trans_metrics(t, vg, vm)
    # spur sanity gate
    sp = ref["spur_500u"]
    fs_g, As_g = sp[:, 0], sp[:, 1]
    fs_m, As_m = bench.measure_spur(lib, subckt, "500u", xparams=(xp + " iload=121u").strip())
    a1 = bench.level_at(fs_m, As_m, 8e6)
    spur_dbc = {fb: 20*np.log10(bench.level_at(fs_m, As_m, fb)/(a1+1e-30)) for fb in (16e6, 24e6)}

    # discrete-spur block: reproduce the GT's intrinsic spur tones (Part B)
    spm = _spur_metrics(ref, lib, subckt, xp, loads)

    # HF extension block: model vs the *_hf wideband reference above the in-band AC top
    hfm = _hf_metrics(ref, lib, subckt, xp, loads)

    # SUPPLY-spur block: model vs GT supply->output rejection at the AVDD aggressor tones
    ssm = _supply_spur_metrics(ref, lib, subckt, xp, loads)

    _print(rows, extra, spur_dbc, spm, hfm, ssm)
    _plots(rows, lib, subckt, refpath)
    comp = (W["zrms"]*np.mean([r["zrms"] for r in rows])
            + W["zband"]*np.mean([r["zband"] for r in rows])
            + W["zphase"]*np.mean([r["zphase"] for r in rows])
            + W["pkdb"]*np.mean([abs(r["pkdb"]) for r in rows])
            + W["pband"]*np.mean([r["pband"] for r in rows])
            + W["pphase"]*np.mean([r["pphase"] for r in rows])
            + W["trms"]*np.mean([r["tm"]["wrms"] for r in rows])
            + W["noise"]*np.mean([r["nm"]["psd_rms"] for r in rows]))
    if spm:                                  # spur fidelity adds to composite only when spurs exist
        comp += W["spur"] * spm["mean_db"] + 2.0 * (spm["n_missed"] + spm["n_false"])
        if np.isfinite(spm.get("mean_ph_deg", np.nan)):   # coherent-phase fidelity (deg)
            comp += W["spurph"] * spm["mean_ph_deg"]
    if hfm:                                  # HF extension fidelity (refs with *_hf arrays)
        comp += W["zhf"] * hfm["zhf"] + W["phf"] * hfm.get("phf", 0.0)
    if ssm and ssm["explicit"]:              # supply-spur fidelity (refs with supply_spur_* arrays)
        comp += W["sspur"] * ssm["mean_db"]
    # R3: matched-frequency noise-resonance error + LF (datasheet uVrms window) integrated-RMS
    # error, low weight + saturation (noise ref is always present, so no gating needed; the
    # caps keep a designed v8/v10 In-notch from dominating). Mirrors the zband/pband pattern.
    comp += W["npk"] * float(np.nanmean([float(np.minimum(np.abs(r["nm"]["pkdb"]), NPK_CAP)) for r in rows]))
    comp += W["nir_lf"] * float(np.nanmean([float(np.minimum(np.abs(r["nm"]["ir_lf"]), NIR_LF_CAP)) for r in rows]))
    print(f"\n>>> composite error score (lower=better): {comp:.1f}")
    summary = dict(
        composite=float(comp),
        per=[dict(il=r["il"], zrms=float(r["zrms"]), zband=float(r["zband"]),
                  zphase=float(r["zphase"]), pkf=float(r["pkfratio"]), pkdb=float(r["pkdb"]),
                  pband=float(r["pband"]), pphase=float(r["pphase"]),
                  wrms=float(r["tm"]["wrms"]), npsd=float(r["nm"]["psd_rms"]),
                  npk=float(r["nm"]["pkdb"]), npkf=float(r["nm"]["npkf"]),
                  nir=float(r["nm"]["ir_pct"]), nir_lf=float(r["nm"]["ir_lf"])) for r in rows],
        spur16=float(spur_dbc[16e6]), spur24=float(spur_dbc[24e6]),
        big_wrms=float(extra["big"]["wrms"]) if "big" in extra else None,
        slew_wrms=float(extra["slew"]["wrms"]) if "slew" in extra else None,
        spur_n=(spm["n_gt"] if spm else 0),
        spur_worst_db=(spm["worst_db"] if spm else None),
        spur_mean_db=(spm["mean_db"] if spm else None),
        spur_missed=(spm["n_missed"] if spm else None),
        spur_false=(spm["n_false"] if spm else None),
        spur_worst_ph_deg=(spm.get("worst_ph_deg") if spm else None),
        spur_mean_ph_deg=(spm.get("mean_ph_deg") if spm else None),
        sspur_worst_db=(ssm["worst_db"] if ssm else None),
        sspur_mean_db=(ssm["mean_db"] if ssm else None),
        sspur_worst_f=(ssm["worst_f"] if ssm else None),
        sspur_scored=(bool(ssm["explicit"]) if ssm else False),
        zhf=(hfm["zhf"] if hfm else None),
        zhf_phase=(hfm["zhf_phase"] if hfm else None),
        phf=(hfm.get("phf") if hfm else None),
        phf_phase=(hfm.get("phf_phase") if hfm else None),
        hf_fmax=(hfm["fmax"] if hfm else None),
        zpass_synth_ok=bool(all(r["zpass_m"][0] for r in rows)),
        zout_minre_synth=float(min(r["zpass_m"][1] for r in rows)),
        zout_minre_gt=float(min(r["zpass_g"][1] for r in rows)),
    )
    return summary


def _spur_metrics(ref, lib, subckt, xp, loads):
    """Reproduce-the-GT-spurs check: characterize the MODEL's intrinsic output tones
    (transient-FFT) at the nominal corner and match to the stored GT spur table.
    Returns None when the GT has no discrete spurs (selector off -> no spur block)."""
    import spur_char
    if "spur_F" not in ref.files or len(ref["spur_F"]) == 0:
        return None
    il = "121u" if "121u" in loads else loads[len(loads) // 2]
    gt = ref[f"spurs_{il}"]
    gt_sp = [dict(f=float(r[0]), amp=float(r[1]), phase=float(r[2])) for r in gt]
    res = spur_char.characterize(lib, subckt, il, xparams=(xp + f" iload={il}").strip())
    sc = spur_char.score_spurs(gt_sp, res["spurs"])
    sc["il"] = il
    sc["rows_print"] = sc["rows"]
    return sc


def _hf_metrics(ref, lib, subckt, xp, loads):
    """HF EXTENSION fidelity: re-measure the MODEL with the wideband AC sweep and
    score it against the stored z/p *_hf reference, in the band ABOVE the in-band
    AC top only (the in-band part is already scored per corner -- no double count).
    The composite used to be BLIND here: an RLC model that fits to 100MHz but
    extrapolates wrong at the carrier (v7 ESL tail, v8 notch) scored clean. Returns
    None when the reference has no *_hf arrays (e.g. a digest ref whose in-band z
    already reaches the ceiling -- then zrms covers it)."""
    nom = "121u" if "121u" in loads else loads[len(loads) // 2]
    # gen_reference keeps the literal "121u" token in the *_hf array names
    kz = next((k for k in (f"z_{nom}_hf", "z_121u_hf") if k in ref.files), None)
    if kz is None:
        return None
    gz = ref[kz]
    fh, Zg = gz[:, 0], gz[:, 1] + 1j * gz[:, 2]
    ftop_in = float(ref[f"z_{nom}"][:, 0].max())
    m = fh > ftop_in
    if m.sum() < 3:
        return None
    xpn = (xp + f" iload={nom}").strip()
    accmd = bench.ac_hf_cmd(float(fh.max()))
    fm, Zm = bench.measure_zout(lib, subckt, nom, xparams=xpn, accmd=accmd)
    Zmi = _interp_cplx(fh[m], fm, Zm)
    ez = 20 * np.log10(np.abs(Zmi) / np.abs(Zg[m]))
    out = dict(il=nom, fmax=float(fh.max()), f_lo=ftop_in,
               zhf=float(np.sqrt(np.mean(ez ** 2))),
               zhf_phase=float(np.sqrt(np.mean(np.degrees(np.angle(Zmi / Zg[m])) ** 2))),
               zhf_worst=(float(fh[m][np.argmax(np.abs(ez))]), float(ez[np.argmax(np.abs(ez))])))
    kp = next((k for k in (f"p_{nom}_hf", "p_121u_hf") if k in ref.files), None)
    if kp is not None:
        gp = ref[kp]
        fph, Hg = gp[:, 0], gp[:, 1] + 1j * gp[:, 2]
        mp = fph > float(ref[f"p_{nom}"][:, 0].max())
        if mp.sum() >= 3:
            fpm, Hm = bench.measure_psrr(lib, subckt, nom, xparams=xpn, accmd=accmd)
            Hmi = _interp_cplx(fph[mp], fpm, Hm)
            epd = np.clip(_atten(Hmi), None, 200) - _atten(Hg[mp])
            out["phf"] = float(np.sqrt(np.mean(epd ** 2)))
            out["phf_phase"] = float(np.sqrt(np.mean(
                np.degrees(np.angle(Hmi / Hg[mp])) ** 2)))
    return out


def _supply_spur_metrics(ref, lib, subckt, xp, loads):
    """SUPPLY-spur rejection fidelity: model-vs-GT supply->output attenuation [dB] at the
    AVDD aggressor tones (DC-DC comb + ref clock). Because the supply-noise output a spur
    causes = input_PSD * |PSRR(f0)|, this attenuation error IS the supply-spur OUTPUT error
    (the input PSD cancels in the ratio) -- validated == a real `noisefile` .noise injection
    to <0.4 pp (cadence/supply_noise/gt_vs_model_supply_noise.py).

    GT attenuation comes from the COLLECTED `supply_spur_{il}` array when present (regenerate
    the reference to enable composite scoring); else it is derived from the stored PSRR
    `p_{il}` so the scorecard line still prints on legacy refs. `explicit` flags which path.
    Returns None only if neither array exists."""
    nom = "121u" if "121u" in loads else loads[len(loads) // 2]
    explicit = f"supply_spur_{nom}" in ref.files
    if explicit:
        arr = ref[f"supply_spur_{nom}"]
        sf, gat = arr[:, 0], arr[:, 1]
    elif f"p_{nom}" in ref.files:
        gp = ref[f"p_{nom}"]
        sf, gat = bench.supply_spur_atten(gp[:, 0], gp[:, 1] + 1j * gp[:, 2])
    else:
        return None
    fpm, Hm = bench.measure_psrr(lib, subckt, nom, xparams=(xp + f" iload={nom}").strip())
    _, mat = bench.supply_spur_atten(fpm, Hm, spurs=sf)
    err = mat - gat                          # +dB => model OVER-rejects (under-predicts the spur)
    rows = [dict(f=float(f0), gt_db=float(g), md_db=float(m), err=float(e))
            for f0, g, m, e in zip(sf, gat, mat, err)]
    iw = int(np.argmax(np.abs(err)))
    return dict(il=nom, rows=rows, explicit=bool(explicit),
                worst_db=float(np.abs(err[iw])), worst_f=float(sf[iw]),
                mean_db=float(np.mean(np.abs(err))))


def _print(rows, extra, spur, spm=None, hfm=None, ssm=None):
    print(f"\n{'='*92}\nSCORECARD  (errors in dB unless noted; band = 8/16/24MHz spur offsets)\n{'='*92}")
    print(f"{'load':>5} | {'Zrms':>5} {'Zband':>6} {'Zdeg':>5} {'pk_df':>5} {'pk_dB':>6}"
          f" | {'Pband':>6} {'Pdeg':>5} | {'Twrms%':>6} {'Tdroop':>7} | "
          f"{'Npsd':>5} {'Npk':>5} {'Npkf':>5} {'Nrms%':>6} {'Nlf%':>6}")
    for r in rows:
        t, n = r["tm"], r["nm"]
        print(f"{r['il']:>5} | {r['zrms']:5.2f} {r['zband']:6.2f} {r['zphase']:5.1f} "
              f"{r['pkfratio']:5.2f} {r['pkdb']:6.1f} | {r['pband']:6.2f} {r['pphase']:5.1f} | "
              f"{t['wrms']:6.1f} {t['drerr']:+6.2f}m | {n['psd_rms']:5.1f} {n['pkdb']:+5.1f} "
              f"{n['npkf']:5.2f} {n['ir_pct']:+6.0f} {n['ir_lf']:+6.0f}")
    print(f"\nZout band @121u: " + "  ".join(f"{fb/1e6:.0f}M:{rows[1]['zb'][fb]:+.2f}" for fb in BANDS) +
          f"   PSRR band @121u: " + "  ".join(f"{fb/1e6:.0f}M:{rows[1]['pb'][fb]:+.2f}" for fb in BANDS))
    print(f"Transient @121u(lin +50uA): GT droop={rows[1]['tm']['droopg']:.3f}mV "
          f"model={rows[1]['tm']['droopm']:.3f}mV | ring GT={rows[1]['tm']['ringg']/1e6:.2f} "
          f"model={rows[1]['tm']['ringm']/1e6:.2f}MHz")
    for tag, t in extra.items():
        print(f"Transient @121u({tag:>4}): GT droop={t['droopg']:.2f}mV model={t['droopm']:.2f}mV "
              f"(err {t['drerr']:+.2f}mV, wrms {t['wrms']:.0f}%)")
    print(f"Noise @121u: PSD log-RMS(Sg-wtd)={rows[1]['nm']['psd_rms']:.2f}dB  "
          f"resonance: height err={rows[1]['nm']['pkdb']:+.1f}dB freq ratio={rows[1]['nm']['npkf']:.2f}  "
          f"int-rms err full={rows[1]['nm']['ir_pct']:+.0f}% LF={rows[1]['nm']['ir_lf']:+.0f}%")
    # passivity: synth must be passive (HB guardrail); GT may be actively non-passive
    synth_ok = all(r["zpass_m"][0] for r in rows)
    smin = min(r["zpass_m"][1] for r in rows); gmin = min(r["zpass_g"][1] for r in rows)
    print(f"Zout PASSIVITY (scikit-rf): synth {'PASS' if synth_ok else '*** FAIL ***'} "
          f"(min Re Zsynth={smin:+.2f}ohm)  |  GT min Re Zgt={gmin:+.2f}ohm"
          f"{'  [GT actively non-passive -> passive model floor]' if gmin < -1e-3 else ''}")
    g16, g24 = spur[16e6], spur[24e6]
    ok = (g16 < -45) and (g24 < -45)
    print(f"Spur SANITY gate: 16M={g16:.0f}dBc 24M={g24:.0f}dBc -> {'PASS (linear)' if ok else 'FAIL (nonlinear!)'}")
    if spm:
        det = "  ".join(
            f"{r['f']/1e6:.2f}MHz:{r['amp_db']:+.2f}dB/{np.degrees(r['phase_err']):+.1f}deg"
            if r['amp_db'] is not None else f"{r['f']/1e6:.2f}MHz:MISS" for r in spm["rows"])
        print(f"Discrete SPURS @{spm['il']} ({spm['n_matched']}/{spm['n_gt']} matched, "
              f"{spm['n_missed']} missed, {spm['n_false']} false): worst {spm['worst_db']:.2f}dB"
              f" / {spm['worst_ph_deg']:.1f}deg | {det}")
    if hfm:
        wf, wv = hfm["zhf_worst"]
        print(f"HF extension @{hfm['il']} ({hfm['f_lo']/1e6:.0f}M-{hfm['fmax']/1e6:.0f}MHz): "
              f"|Z| rms {hfm['zhf']:.2f}dB (worst {wv:+.2f}dB @{wf/1e6:.0f}MHz) "
              f"phase {hfm['zhf_phase']:.1f}deg"
              + (f" | PSRR rms {hfm['phf']:.2f}dB phase {hfm['phf_phase']:.1f}deg"
                 if "phf" in hfm else ""))
    if ssm:
        det = "  ".join(f"{r['f']/1e6:.1f}M:{r['err']:+.2f}" for r in ssm["rows"])
        tag = "" if ssm["explicit"] else "  [derived from PSRR; regen ref to score]"
        print(f"SUPPLY-spur rejection @{ssm['il']} (atten err dB vs GT, worst "
              f"{ssm['worst_db']:.2f}dB @{ssm['worst_f']/1e6:.1f}MHz, mean {ssm['mean_db']:.2f}dB): "
              f"{det}{tag}")


def _plots(rows, lib, subckt, refpath=None):
    refpath = refpath or REF
    SCOREDIR.mkdir(parents=True, exist_ok=True)
    for r in rows:
        fig, ax = plt.subplots(2, 2, figsize=(12, 8))
        ax[0, 0].loglog(r["fz"], r["Zg"], label="GT", lw=2)
        ax[0, 0].loglog(r["fz"], r["Zm"], "--", label="model")
        ax[0, 0].set(title=f"Zout  load={r['il']}", xlabel="Hz", ylabel="|Zout| (ohm)")
        ax[0, 1].semilogx(r["fp"], r["ag"], label="GT", lw=2)
        ax[0, 1].semilogx(r["fp"], r["am"], "--", label="model")
        ax[0, 1].set(title=f"PSRR  load={r['il']}", xlabel="Hz", ylabel="atten (dB)")
        n = r["nm"]
        ax[1, 0].loglog(n["fn"], n["Sg"] * 1e9, label="GT", lw=2)
        ax[1, 0].loglog(n["fn"], n["Smi"] * 1e9, "--", label="model")
        ax[1, 0].set(title=f"output noise  load={r['il']}", xlabel="Hz", ylabel="Sv (nV/rtHz)")
        for a in (ax[0, 0], ax[0, 1], ax[1, 0]):
            for fb in BANDS:
                a.axvline(fb, color="g", ls=":", alpha=.4)
            a.legend(); a.grid(True, which="both", alpha=.3)
        # transient
        d = np.load(refpath, allow_pickle=True)[f"trans_lin_{r['il']}"]
        t, vg = d[:, 0], d[:, 1]
        base = ng.amps(r["il"])
        _, vm = bench.measure_loadstep(lib, subckt, bench.LIN_FRAC * base,
                                       iload=base, xparams=f"iload={r['il']}")
        ax[1, 1].plot(t * 1e6, vg * 1e3, label="GT", lw=2)
        ax[1, 1].plot(t * 1e6, vm * 1e3, "--", label="model")
        ax[1, 1].set(title=f"load step +{bench.LIN_FRAC*base*1e6:.0f}uA  load={r['il']}", xlabel="us", ylabel="Vout (mV)")
        ax[1, 1].legend(); ax[1, 1].grid(True, alpha=.3)
        fig.tight_layout()
        fig.savefig(SCOREDIR / f"overlay_{r['il']}.png", dpi=105)
        plt.close(fig)
    print(f"saved overlays to {SCOREDIR}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    ap.add_argument("--lib", default=None)
    ap.add_argument("--subckt", default="ldo_model")
    ap.add_argument("--ref", default=None)
    ap.add_argument("--xparams", default="")
    ap.add_argument("--crossval", action="store_true",
                    help="ALSO run the out-of-sample guardrails (LOCO / off-grid / "
                         "identifiability) after the in-sample scorecard")
    ap.add_argument("--strict", action="store_true",
                    help="with --crossval: exit nonzero if any guardrail gate FAILs")
    ap.add_argument("--systest", action="store_true",
                    help="ALSO run the system LDO+buffer@carrier acceptance test "
                         "(GT vs emitted model; complex carrier/sideband diff)")
    a = ap.parse_args()
    name = "ldo_model" if a.variant == "base" else f"ldo_{a.variant}"
    lib = a.lib or str(ng.ROOT / "model" / f"{name}.lib")
    refpath = a.ref or str(ng.ROOT / "results" / "ref" / f"{a.variant}.npz")
    score(lib, a.subckt, a.xparams, refpath=refpath)
    if a.crossval:                          # optional held-out checks; lazy import so
        import crossval                     # run_matrix (which calls score() directly,
        rep = crossval.run(a.variant)       # never the CLI) is byte-unaffected
        if a.strict and not all(v for v in rep["passes"].values() if v is not None):
            raise SystemExit(1)
    if a.systest:                           # optional system test; lazy import (same reason
        import systest                      # as --crossval) -> run_matrix byte-unaffected
        srep = systest.run(a.variant)
        # LARGE_SIGNAL (GT nonlinear) / OUT_OF_ENVELOPE withhold the verdict -> not a FAIL;
        # a strict FAIL is only an in-envelope, GT-linear case that misses the thresholds.
        if a.strict and srep["in_envelope"] and srep.get("linear", True) and not srep["pass_"]:
            raise SystemExit(1)
