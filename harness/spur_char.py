"""Discrete-spur characterization (Part B of the noise block).

Real LDO output noise carries DISCRETE TONES (bandgap/reference spurs, charge-pump
ripple, internal-clock feedthrough) that a smooth PSD (Part A) cannot reproduce but
that BEAT the carrier in PSS/HB. We characterize them the way they are measured on
real silicon: a TRANSIENT run of vout (NO external stimulus) -> FFT over a coherent
window -> pick the discrete lines that stand ABOVE the local broadband floor. Output
is a (f, amplitude, phase) table the model injects as deterministic tones at vout.

This is .noise-INDEPENDENT on purpose: .noise only returns the smooth PSD.
"""
import numpy as np
import ng

# coherent-sampling transient: bin spacing = 1/(TWIN_LEN). Spurs placed on this grid
# integrate cleanly. Defaults resolve >=62.5kHz-spaced tones over 0..fmax.
DT = 1e-9
TSTOP = 80e-6
TWIN = (16e-6, 80e-6)           # 64us window -> 15.625 kHz bins
BINHZ = 1.0 / (TWIN[1] - TWIN[0])


def _tran_vout(lib, subckt, iload, xparams="", dt=DT, tstop=TSTOP, extra=""):
    """Run a plain transient and return (t, v(vout)) on the native time grid."""
    import bench
    libs = list(lib) if isinstance(lib, (list, tuple)) else [lib]
    tb = f"""* intrinsic output-spectrum transient
Xdut vin vout {subckt} {xparams}
Vin vin 0 DC 1.05
Iload vout 0 DC {iload}
{extra}
.control
set wr_singlescale
tran {dt} {tstop} 0
wrdata out.dat v(vout)
quit
.endc
.end
"""
    r = ng.run(ng.assemble(tb, libs=libs), bench.WORK / "spur", outputs=["out.dat"])
    if r["out.dat"] is None:
        raise RuntimeError("spur transient failed:\n" + r["_stderr"][-1500:])
    a = r["out.dat"][1]
    return a[:, 0], a[:, 1]


def spectrum_from_tv(t, v, twin=None, dt=None):
    """Single-sided amp+phase spectrum of a PRE-CAPTURED (t, v) waveform over a coherent
    window -- the array-only core of output_spectrum (no simulator). This is what lets the
    Cadence side export a plain transient and have the harness do the FFT/peak-pick itself.
      twin=(t0, t1) [s] selects the window (t1=None -> end of record); default skips the first
        20% of the record as settling and uses the rest.
      dt [s] = uniform resample step (default = median native sample spacing; handles the
        non-uniform time grid of an adaptive-step transient export).
    Returns (f, amp[V peak], phase[rad], twin0[s], binhz[Hz]). twin0 is the window start (the
    phase reference) and binhz the achieved bin width -- store these as spur_twin0/spur_binhz."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    if twin is None:
        t0, t1 = t[0] + 0.2 * (t[-1] - t[0]), t[-1]
    else:
        t0, t1 = twin
        if t1 is None:
            t1 = t[-1]
    if dt is None:
        dt = float(np.median(np.diff(t)))
    tg = np.arange(t0, t1, dt)
    vg = np.interp(tg, t, v)
    n = len(tg)
    V = np.fft.rfft((vg - vg.mean()) * np.hanning(n)) * (2.0 / n) / 0.5  # hann amp-correct
    f = np.fft.rfftfreq(n, dt)
    return f, np.abs(V), np.angle(V), float(t0), float(1.0 / (n * dt))


def output_spectrum(lib, subckt, iload, xparams="", dt=DT, tstop=TSTOP, twin=TWIN, extra=""):
    """Single-sided amplitude+phase spectrum of v(vout) over a coherent window.
    Returns (f, amp[V peak], phase[rad]). Resampled to a uniform grid so the bins
    are exactly k/Twin (coherent for tones on that grid). Runs the transient, then
    defers the math to spectrum_from_tv (the simulator-free core)."""
    t, v = _tran_vout(lib, subckt, iload, xparams, dt, tstop, extra)
    f, amp, ph, _, _ = spectrum_from_tv(t, v, twin=twin, dt=dt)
    return f, amp, ph


def find_spurs(f, amp, fmax=30e6, n_floor=25, snr=6.0, max_spurs=12,
               min_amp=5e-8, rel_floor=300.0, binhz=BINHZ):
    """Pick discrete lines that exceed a local median floor by `snr`x. A line is
    kept only if it ALSO clears an absolute amplitude floor `min_amp` [V] AND is
    within `rel_floor`x of the largest detected line (drops sub-uV numerical
    artifacts on an otherwise floorless model spectrum). Returns dicts (f, amp, snr)
    sorted by amplitude. n_floor = half-width (bins) of the rolling-median floor.
    `binhz` sets the near-DC exclusion floor (5*binhz); pass the achieved bin width
    for an arbitrary-length waveform so the cutoff scales with it."""
    sel = (f > 5 * binhz) & (f <= fmax)
    fi, ai = f[sel], amp[sel]
    floor = np.array([np.median(ai[max(0, i - n_floor):i + n_floor + 1]) for i in range(len(ai))])
    floor = np.maximum(floor, 1e-18)
    cand = (ai > snr * floor) & (ai > min_amp)
    spurs = []
    i = 0
    while i < len(ai):
        if cand[i]:
            j = i
            while j + 1 < len(ai) and cand[j + 1]:
                j += 1
            k = i + int(np.argmax(ai[i:j + 1]))            # local peak bin
            spurs.append(dict(f=float(fi[k]), amp=float(ai[k]), snr=float(ai[k] / floor[k])))
            i = j + 1
        else:
            i += 1
    if spurs:
        amax = max(s["amp"] for s in spurs)
        spurs = [s for s in spurs if s["amp"] >= amax / rel_floor]   # drop >rel_floor down
    spurs.sort(key=lambda s: -s["amp"])
    return spurs[:max_spurs]


def characterize(lib, subckt, iload="121u", xparams="", fmax=30e6):
    """Convenience: spectrum + spur table for a DUT (intrinsic spurs only)."""
    f, amp, ph = output_spectrum(lib, subckt, iload, xparams=xparams)
    spurs = find_spurs(f, amp, fmax=fmax)
    # attach phase at each detected line
    for s in spurs:
        s["phase"] = float(ph[np.argmin(np.abs(f - s["f"]))])
    return dict(f=f, amp=amp, phase=ph, spurs=spurs)


def _is_combo(f, base_fs, tol, max_order, max_terms):
    """True if f ~ sum(n_i*base_fs[i]) for small integers n_i (a nonlinear IM /
    harmonic product of the given fundamentals)."""
    import itertools
    if not base_fs:
        return False
    rng = range(-max_order, max_order + 1)
    for combo in itertools.product(rng, repeat=len(base_fs)):
        sa = sum(abs(c) for c in combo)
        if sa == 0 or sa > max_terms:
            continue
        if abs(sum(c * bf for c, bf in zip(combo, base_fs)) - f) <= tol:
            return True
    return False


def classify_fundamentals(spurs, tol_hz=1.5 * BINHZ, max_order=2, max_terms=3):
    """Split detected spurs into independent FUNDAMENTALS vs nonlinear IM/harmonic
    PRODUCTS. Greedy by amplitude: a line is a product if its freq is a small-integer
    combination of already-accepted (larger) fundamentals. The behavioral model is
    LINEAR, so it emits ONLY fundamentals; the user's real nonlinear RF block
    regenerates the products (emitting them in the model would double-count). Returns
    (funds sorted by freq, products)."""
    funds, prods = [], []
    for s in sorted(spurs, key=lambda s: -s["amp"]):
        if _is_combo(s["f"], [g["f"] for g in funds], tol_hz, max_order, max_terms):
            prods.append(s)
        else:
            funds.append(s)
    funds.sort(key=lambda s: s["f"])
    return funds, prods


def fundamental_base(freqs, tol_hz=0.6 * BINHZ):
    """GCD of the freq set on the coherent bin grid -> the single PSS base tone whose
    harmonics span all spurs (commensurate case). Returns (base_hz, max_order). If the
    implied order is huge (effectively incommensurate), base is meaningless -> caller
    declares separate funds."""
    import math
    if not freqs:
        return None, 0
    bins = [int(round(f / BINHZ)) for f in freqs]
    g = bins[0]
    for b in bins[1:]:
        g = math.gcd(g, b)
    base = g * BINHZ
    return base, max(int(round(f / base)) for f in freqs)


def characterize_corners(libs, subckt, loads, xparams_of, fmax=30e6):
    """Per-corner intrinsic-spur table with a CONSISTENT fundamental set. Detect +
    classify fundamentals at the nominal (middle) corner to fix the freq list F, then
    read amplitude+phase of those exact bins at every load corner (so the table has
    the same N tones per corner, ready for amplitude interpolation). xparams_of(il)
    returns the per-corner xparams string (lets GT and model use different forms)."""
    nom = loads[len(loads) // 2]
    base = characterize(libs, subckt, nom, xparams=xparams_of(nom), fmax=fmax)
    funds, prods = classify_fundamentals(base["spurs"])
    F = [s["f"] for s in funds]
    per = {}
    for il in loads:
        f, amp, ph = output_spectrum(libs, subckt, il, xparams=xparams_of(il))
        rows = [(fk, float(amp[np.argmin(np.abs(f - fk))]),
                 float(ph[np.argmin(np.abs(f - fk))])) for fk in F]
        per[il] = np.array(rows) if rows else np.zeros((0, 3))
    return dict(F=F, per=per, funds=funds, prods=prods)


def characterize_corners_from_waves(waves, loads, nominal=None, fmax=30e6, twin=None, dt=None):
    """Array-only twin of characterize_corners: build the per-corner intrinsic-spur table from
    PRE-CAPTURED raw v(vout) waveforms instead of running sims -- the path import_cadence uses so
    the Cadence side exports a plain transient (no external stimulus) and the harness does the
    FFT / peak-pick / fundamental-classification. `waves` maps corner-key -> (t, v) arrays of
    v(vout). Detects + classifies fundamentals at the NOMINAL corner to FIX the freq list F, then
    reads amp+phase of those exact bins at every corner (so every corner has the same N tones,
    ready for amplitude interpolation -- identical contract to characterize_corners). twin/dt are
    forwarded to spectrum_from_tv (default: skip first 20%, resample at the native median step).
    Returns dict(F, per, funds, prods, twin0, binhz) -- twin0/binhz are taken from the nominal
    corner and become spur_twin0/spur_binhz."""
    nom = str(nominal) if nominal else loads[len(loads) // 2]
    if nom not in waves:
        raise ValueError(f"nominal corner '{nom}' has no raw waveform (have {list(waves)})")
    fn, an, phn, twin0, binhz = spectrum_from_tv(*waves[nom], twin=twin, dt=dt)
    spurs = find_spurs(fn, an, fmax=fmax, binhz=binhz)
    for s in spurs:
        s["phase"] = float(phn[np.argmin(np.abs(fn - s["f"]))])
    funds, prods = classify_fundamentals(spurs, tol_hz=1.5 * binhz)
    F = [s["f"] for s in funds]
    per = {}
    for il in loads:
        if il not in waves:
            per[il] = np.zeros((0, 3))
            continue
        f, amp, ph, _, _ = spectrum_from_tv(*waves[il], twin=twin, dt=dt)
        rows = [(fk, float(amp[np.argmin(np.abs(f - fk))]),
                 float(ph[np.argmin(np.abs(f - fk))])) for fk in F]
        per[il] = np.array(rows) if rows else np.zeros((0, 3))
    return dict(F=F, per=per, funds=funds, prods=prods, twin0=twin0, binhz=binhz)


def match_spurs(gt_spurs, model_spurs, tol_hz=2 * BINHZ):
    """Match model spurs to GT spurs by nearest frequency (within tol). Returns a
    list of dicts per GT spur: f, gt_amp, model_amp (or None if missed), amp_db
    (20log10(model/gt)), phase_err. Also flags model-only 'spurious' tones."""
    used = set()
    rows = []
    for g in gt_spurs:
        cand = [(abs(m["f"] - g["f"]), j) for j, m in enumerate(model_spurs)
                if j not in used and abs(m["f"] - g["f"]) <= tol_hz]
        if cand:
            _, j = min(cand)
            used.add(j); m = model_spurs[j]
            rows.append(dict(f=g["f"], gt_amp=g["amp"], model_amp=m["amp"],
                             amp_db=float(20 * np.log10(m["amp"] / (g["amp"] + 1e-18))),
                             phase_err=float(np.angle(np.exp(1j * (m.get("phase", 0) - g.get("phase", 0)))))))
        else:
            rows.append(dict(f=g["f"], gt_amp=g["amp"], model_amp=None, amp_db=None, phase_err=None))
    extra = [model_spurs[j] for j in range(len(model_spurs)) if j not in used]
    return rows, extra


def score_spurs(gt_spurs, model_spurs, tol_hz=2 * BINHZ):
    """Scalar spur fidelity: worst/mean |amp error dB| AND worst/mean |phase error deg|
    over MATCHED GT spurs, count of missed GT spurs and model-only false spurs.
    Lower = better. Phase matters as much as amplitude for the PSS/HB end use --
    sidebands superpose COHERENTLY, so an in-amplitude spur 45deg off still corrupts
    the carrier spectrum (cf. the emit_va -H sign bug: 180deg, invisible to any
    magnitude-only gate)."""
    rows, extra = match_spurs(gt_spurs, model_spurs, tol_hz)
    matched = [r for r in rows if r["model_amp"] is not None]
    errs = [abs(r["amp_db"]) for r in matched]
    pherrs = [abs(np.degrees(r["phase_err"])) for r in matched]
    return dict(n_gt=len(gt_spurs), n_matched=len(matched), n_missed=len(rows) - len(matched),
                n_false=len(extra),
                worst_db=float(max(errs)) if errs else float("nan"),
                mean_db=float(np.mean(errs)) if errs else float("nan"),
                worst_ph_deg=float(max(pherrs)) if pherrs else float("nan"),
                mean_ph_deg=float(np.mean(pherrs)) if pherrs else float("nan"),
                rows=rows, extra=extra)


if __name__ == "__main__":
    import argparse
    import variants
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    ap.add_argument("--iload", default="121u")
    a = ap.parse_args()
    v = variants.get(a.variant)
    libs = [str(p) for p in v["libs"]]
    res = characterize(libs, v["subckt"], a.iload, xparams=v["xparams"])
    print(f"variant '{a.variant}' iload={a.iload}: bin={BINHZ/1e3:.1f}kHz  detected {len(res['spurs'])} spurs")
    for s in res["spurs"]:
        print(f"  f={s['f']/1e6:8.4f}MHz  amp={s['amp']*1e6:9.3f}uV  snr={s['snr']:5.1f}  ph={s['phase']:+.2f}")
