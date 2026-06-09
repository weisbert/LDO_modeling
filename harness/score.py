"""Score a candidate LDO model against the ground-truth reference. THIS is the
feedback loop. Runs the SAME Zout/PSRR/noise/transient stimuli on the candidate
that gen_reference ran on the GT, then reports error metrics + overlay plots.

Metrics (per load corner unless noted):
  Zout : magnitude RMS + band err(8/16/24MHz) + resonance peak df/dB + PHASE RMS
  PSRR : band err + worst-notch + PHASE RMS
  Trans: load-step droop err, settled err, waveform RMS, ring-freq err  (req#1)
  Noise: output-PSD log-RMS + resonance-peak err + integrated-rms err     (req#2)
  Spur : SANITY gate only (GT harmonics at numerical floor -> not scored)

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
W = dict(zrms=1.0, zband=3.0, zphase=0.04, pkdb=1.0, pband=2.0,
         pphase=0.03, trms=0.2, noise=0.5, spur=0.5)


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


def _noise_metrics(fn, Sg, fm, Sm):
    Smi = _interp_mag(fn, fm, Sm)
    db = 20 * np.log10((Smi + 1e-18) / (Sg + 1e-18))
    band = (fn >= 10) & (fn <= 100e6)
    res = (fn > 0.5e6) & (fn < 3e6)
    irg = np.sqrt(_TRAP(Sg[band] ** 2, fn[band]))
    irm = np.sqrt(_TRAP(Smi[band] ** 2, fn[band]))
    return dict(psd_rms=float(np.sqrt(np.mean(db[band] ** 2))),
                pkdb=float(20 * np.log10(Smi[res].max() / Sg[res].max())),
                ir_pct=float((irm / irg - 1) * 100), Smi=Smi, fn=fn, Sg=Sg)


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
        ezph = np.degrees(np.angle(Zmi) - np.angle(Zg))
        lo = fz < 1e7
        ipg, ipm = np.argmax(np.abs(Zg) * lo), np.argmax(np.abs(Zmi) * lo)
        # ---- PSRR (mag + phase) ----
        gp = ref[f"p_{il}"]
        fp, Hg = gp[:, 0], gp[:, 1] + 1j * gp[:, 2]
        fpm, Hm = bench.measure_psrr(lib, subckt, il, xparams=xpil)
        Hmi = _interp_cplx(fp, fpm, Hm)
        ag, am = _atten(Hg), np.clip(_atten(Hmi), None, 200)
        ep = am - ag
        epph = np.degrees(np.angle(Hmi) - np.angle(Hg))
        # ---- Noise ----
        gn = ref[f"noise_{il}"]
        fnm, Sm = bench.measure_noise(lib, subckt, il, xparams=xpil)
        nm = _noise_metrics(gn[:, 0], gn[:, 1], fnm, Sm)
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

    _print(rows, extra, spur_dbc, spm)
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
    print(f"\n>>> composite error score (lower=better): {comp:.1f}")
    summary = dict(
        composite=float(comp),
        per=[dict(il=r["il"], zrms=float(r["zrms"]), zband=float(r["zband"]),
                  zphase=float(r["zphase"]), pkf=float(r["pkfratio"]), pkdb=float(r["pkdb"]),
                  pband=float(r["pband"]), pphase=float(r["pphase"]),
                  wrms=float(r["tm"]["wrms"]), npsd=float(r["nm"]["psd_rms"]),
                  npk=float(r["nm"]["pkdb"]), nir=float(r["nm"]["ir_pct"])) for r in rows],
        spur16=float(spur_dbc[16e6]), spur24=float(spur_dbc[24e6]),
        big_wrms=float(extra["big"]["wrms"]) if "big" in extra else None,
        slew_wrms=float(extra["slew"]["wrms"]) if "slew" in extra else None,
        spur_n=(spm["n_gt"] if spm else 0),
        spur_worst_db=(spm["worst_db"] if spm else None),
        spur_mean_db=(spm["mean_db"] if spm else None),
        spur_missed=(spm["n_missed"] if spm else None),
        spur_false=(spm["n_false"] if spm else None),
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


def _print(rows, extra, spur, spm=None):
    print(f"\n{'='*92}\nSCORECARD  (errors in dB unless noted; band = 8/16/24MHz spur offsets)\n{'='*92}")
    print(f"{'load':>5} | {'Zrms':>5} {'Zband':>6} {'Zdeg':>5} {'pk_df':>5} {'pk_dB':>6}"
          f" | {'Pband':>6} {'Pdeg':>5} | {'Twrms%':>6} {'Tdroop':>7} | {'Npsd':>5} {'Npk':>5} {'Nrms%':>6}")
    for r in rows:
        t, n = r["tm"], r["nm"]
        print(f"{r['il']:>5} | {r['zrms']:5.2f} {r['zband']:6.2f} {r['zphase']:5.1f} "
              f"{r['pkfratio']:5.2f} {r['pkdb']:6.1f} | {r['pband']:6.2f} {r['pphase']:5.1f} | "
              f"{t['wrms']:6.1f} {t['drerr']:+6.2f}m | {n['psd_rms']:5.1f} {n['pkdb']:+5.1f} {n['ir_pct']:+6.0f}")
    print(f"\nZout band @121u: " + "  ".join(f"{fb/1e6:.0f}M:{rows[1]['zb'][fb]:+.2f}" for fb in BANDS) +
          f"   PSRR band @121u: " + "  ".join(f"{fb/1e6:.0f}M:{rows[1]['pb'][fb]:+.2f}" for fb in BANDS))
    print(f"Transient @121u(lin +50uA): GT droop={rows[1]['tm']['droopg']:.3f}mV "
          f"model={rows[1]['tm']['droopm']:.3f}mV | ring GT={rows[1]['tm']['ringg']/1e6:.2f} "
          f"model={rows[1]['tm']['ringm']/1e6:.2f}MHz")
    for tag, t in extra.items():
        print(f"Transient @121u({tag:>4}): GT droop={t['droopg']:.2f}mV model={t['droopm']:.2f}mV "
              f"(err {t['drerr']:+.2f}mV, wrms {t['wrms']:.0f}%)")
    print(f"Noise @121u: PSD log-RMS={rows[1]['nm']['psd_rms']:.1f}dB  "
          f"peak err={rows[1]['nm']['pkdb']:+.1f}dB  integrated-rms err={rows[1]['nm']['ir_pct']:+.0f}%")
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
        det = "  ".join(f"{r['f']/1e6:.2f}MHz:{r['amp_db']:+.2f}dB" if r['amp_db'] is not None
                        else f"{r['f']/1e6:.2f}MHz:MISS" for r in spm["rows"])
        print(f"Discrete SPURS @{spm['il']} ({spm['n_matched']}/{spm['n_gt']} matched, "
              f"{spm['n_missed']} missed, {spm['n_false']} false): worst {spm['worst_db']:.2f}dB | {det}")


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
