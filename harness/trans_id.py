"""Single-transient multitone identification of Zout(f) AND PSRR(f) (R5 experiment).

The production recipe characterizes an LDO with FOUR separate analyses per corner
(Zout-AC, PSRR-AC, .noise, DC), each with its own injection source. This module tests
whether ONE multitone transient can replace the two AC sweeps: it drives BOTH
excitations at once via frequency-INTERLEAVED tones and reads both transfer functions
back from a single waveform.

  - voltage tones on vin  (A-set) -> PSRR(fa) = Vout(fa) / Vin(fa)
  - current tones into vout (B-set) -> Zout(fb) = Vout(fb) / Iinj(fb)

Because the LDO is LTI in small signal and the A/B tones sit on DISJOINT coherent FFT
bins, superposition separates the two responses with no cross-talk. Extraction takes
per-bin RATIOS (Vout/input) so the absolute tone amplitude AND the FFT window/time-origin
phase reference cancel exactly -> correct transfer-function magnitude *and* phase.

This is .noise-INDEPENDENT on purpose: a deterministic .tran carries no device noise, so
the intrinsic noise PSD still comes from a separate .noise (kept as-is). DC Vout falls
out of the settled window mean for free.

Conventions are matched to bench.measure_zout / measure_psrr so the extracted z/p are
directly comparable to (and drop-in for) the AC reference arrays:
  measure_zout: `Iac 0 vout AC 1` -> Z = V(vout)/I   ==  here `Biinj 0 vout I=...`
  measure_psrr: `Vin vin 0 ... AC 1` -> H = V(vout)/V(vin)  == here `Bvin vin 0 V=1.05+...`

Nothing here is hardcoded to a particular band/corner: every frequency is derived from the
band arguments. Output Zout/PSRR are returned as np.c_[freq, Re, Im], the same column
layout gen_reference writes for z_{il}/p_{il}.
"""
import json
import pathlib
import numpy as np
import ng
import bench

VDD = 1.05                 # supply DC, matched to bench (the AC path is also 1.05-referenced)
VA_DEFAULT = 5e-4          # vin tone amplitude [V]  (small-signal: total ripple ~1% of VDD)
IB_DEFAULT = 1e-6          # vout current-tone amplitude [A] (small-signal at any |Zout| in band)
BAND_TIMEOUT = 900         # ngspice wall-clock budget per band [s]

# IM de-aliasing: place EVERY tone (both PSRR and Zout) on a bin == IM_RES (mod IM_MOD).
# Then every 2nd-order intermodulation product falls on a residue that holds NO tone bin:
#   a+b == 1+1 == 2,  a-b == 1-1 == 0,  2a == 2   (mod 3)  -- all != 1 -> off ALL measurement
# bins. This matters for a REAL (mildly nonlinear) DUT: without it the same-grid/half-step layout
# put a+/-b and 2a directly on measurement bins (invisible to the leak metric, which masks tone
# bins). With it, 2nd-order IM both (i) stops contaminating the measurements and (ii) becomes
# VISIBLE in the off-tone leak floor. 3rd-order (a+b-c == 1) can still alias but scales as amp^3
# and is caught by the half-amplitude linearity gate (see measure_zp(lin_gate=True)).
IM_MOD = 3
IM_RES = 1


# ----------------------------------------------------------------- coherent multitone plan
def _log_grid(f_lo, f_hi, n_per_dec, offset=0.0):
    """Log-spaced frequencies f_lo..f_hi at n_per_dec points/decade, shifted by `offset`
    steps (offset=0.5 gives the half-step-interleaved companion grid)."""
    ndec = np.log10(f_hi / f_lo)
    n = max(2, int(np.floor(ndec * n_per_dec)) + 1)
    k = np.arange(n) + offset
    g = f_lo * 10.0 ** (k / n_per_dec)
    return g[g <= f_hi * 1.0000001]


def _snap_im_bin(b):
    """Nearest bin index congruent to IM_RES (mod IM_MOD) -- the IM-de-aliased grid."""
    return int(round((b - IM_RES) / IM_MOD)) * IM_MOD + IM_RES


def plan_band(f_lo, f_hi, n_per_dec=12, ppp=12, settle_cycles=8, kbins=4, settle_s=None):
    """Coherent-sampling plan for ONE multitone band. Bin width is chosen fine enough that
    (i) the lowest tone is resolved and (ii) the closest A-vs-B tone pair (half a log step
    apart at the low end) stays >= kbins bins apart after IM-de-aliased snapping, so neither a
    Hann main lobe nor a 2nd-order IM product contaminates a measurement bin. Every tone is
    placed on a bin == IM_RES (mod IM_MOD) (see IM_MOD note). Returns dt/N/Twin/t0/tstop + two
    DISJOINT on-bin tone sets fa/ba (PSRR, on vin) and fb/bb (Zout, into vout).

    settle_s: absolute settle pre-roll [s]. Default settle_cycles/f_lo (cheap; sufficient here
    because the only slow ring is the resonance, which is out-of-band for the high band and
    Hann-suppressed). PRODUCTION should pass the DUT's slowest settling mode (e.g. several
    Q/(pi*f_res) or 1/UGB) so a low-freq/high-Q part is fully settled -- a profile parameter,
    not a magic constant. All other quantities derive from the band arguments (no hardcoding)."""
    f_lo = float(f_lo); f_hi = float(f_hi)
    r = 10.0 ** (1.0 / n_per_dec)
    # 2*kbins headroom so the IM-de-alias snap (which coarsens the allowed grid to every
    # IM_MOD-th bin) cannot push a close A/B pair below the kbins separation.
    binhz = f_lo * (r ** 0.5 - 1.0) / (2 * kbins)
    Twin = 1.0 / binhz
    dt = 1.0 / (ppp * f_hi)                          # >= ppp samples per highest-tone period
    N = int(round(Twin / dt))
    N += N % 2                                       # even N (cosmetic, real-FFT friendly)
    dt = Twin / N
    binhz = 1.0 / (N * dt)                           # exact achieved bin width
    t0 = float(settle_s) if settle_s else settle_cycles / f_lo

    def snap(freqs, used):
        bs, fs = [], []
        for fr in freqs:
            b = _snap_im_bin(int(round(fr / binhz)))
            if b < max(kbins, IM_MOD) or b > N // 2 - kbins:
                continue
            if any(abs(b - u) < kbins for u in used) or any(abs(b - o) < kbins for o in bs):
                continue
            used.append(b); bs.append(b); fs.append(b * binhz)
        return np.array(fs, float), bs

    used = []
    fa, ba = snap(_log_grid(f_lo, f_hi, n_per_dec, 0.0), used)
    fb, bb = snap(_log_grid(f_lo, f_hi, n_per_dec, 0.5), used)
    return dict(f_lo=f_lo, f_hi=f_hi, binhz=binhz, Twin=Twin, dt=dt, N=N,
                t0=t0, tstop=t0 + Twin, fa=fa, ba=ba, fb=fb, bb=bb)


# --------------------------------------------------------------------------- deck + spectrum
def _sumsin(amp, freqs):
    """ngspice B-source sub-expression: sum of amp*sin(2*pi*f*time)."""
    if len(freqs) == 0:
        return "0"
    return "+".join(f"{amp:.8e}*sin({2.0 * np.pi * float(fr):.12e}*time)" for fr in freqs)


def build_deck(subckt, xparams, plan, il, va, ib):
    """Interleaved-multitone testbench: A-tones (voltage) on vin via an ideal B-source,
    B-tones (current) injected into vout, corner load on vout. Probes v(vin) & v(vout)."""
    vexpr = f"{VDD:.6e}" + ("" if len(plan["fa"]) == 0 else "+" + _sumsin(va, plan["fa"]))
    iexpr = _sumsin(ib, plan["fb"])
    return f"""* multitone trans-ID: PSRR via vin voltage tones, Zout via vout current tones
Xdut vin vout {subckt} {xparams}
Bvin vin 0 V = {vexpr}
Biinj 0 vout I = {iexpr}
Iload vout 0 DC {il}
.control
set wr_singlescale
tran {plan['dt']:.8e} {plan['tstop']:.8e} 0 {plan['dt']:.8e}
wrdata out.dat v(vin) v(vout)
quit
.endc
.end
"""


def _spectrum(t, v, plan):
    """Resample v over the coherent window [t0, t0+Twin) onto N uniform points; return the
    single-sided COMPLEX spectrum (Hann, amp-corrected) and the window-mean (=DC). Mirrors
    systest._spectrum / spur_char.spectrum_from_tv so on-bin tones read back exact amp+phase;
    the amp-correction factor is identical for every signal so per-bin ratios cancel it."""
    N, t0, dt = plan["N"], plan["t0"], plan["dt"]
    tg = t0 + dt * np.arange(N)
    vg = np.interp(tg, t, v)
    dc = float(vg.mean())
    w = np.hanning(N)
    V = np.fft.rfft((vg - vg.mean()) * w) * (2.0 / N) / w.mean()
    f = np.fft.rfftfreq(N, dt)
    return f, V, dc


# ------------------------------------------------------------------ extraction core (no sim)
def extract_zp_from_wave(t, vout, plan, ib=IB_DEFAULT, va=VA_DEFAULT, vin=None):
    """Pure FFT+ratio extraction of Zout (at B-bins) and PSRR (at A-bins) from ONE band's
    waveform -- the simulator-INDEPENDENT core shared by measure_band (which first runs
    ngspice) and trans_import (which reads an exported Cadence/ngspice waveform). Phase is
    recovered by dividing the FFT of v(vout) by the FFT of the *known* excitation: the supply
    tones (probed/exported v(vin) when given, else reconstructed analytically as an ideal
    source vdd+Sum va*sin) for PSRR, and the analytic injected current resampled on the SAME
    coherent grid for Zout -> the window/time-origin phase reference cancels in the ratio.

    `plan` is one band's coherent-window dict (plan_band() output, or one entry of an
    emit_stim_va sidecar): needs N, t0, dt, fa, ba, fb, bb. ba/bb are bin indices, fa/fb the
    tone freqs. Returns dict(fz, Z, fp, P, vout_dc, leak_db, N) -- np.c_-ready complex arrays."""
    N = int(plan["N"])
    fa = [float(x) for x in plan["fa"]]
    fb = [float(x) for x in plan["fb"]]
    ba = [int(b) for b in plan["ba"]]
    bb = [int(b) for b in plan["bb"]]

    # coherent-window COVERAGE guard: _spectrum resamples onto [t0, t0+(N-1)dt] via np.interp,
    # which SILENTLY clamps (holds the end value) if the waveform doesn't span that window --
    # corrupting the extraction with only an advisory leak metric. A real export that is too
    # short / mis-settled / time-shifted must fail LOUDLY, not be quietly wrong.
    t = np.asarray(t, float)
    t0, dt = float(plan["t0"]), float(plan["dt"])
    need_hi = t0 + (N - 1) * dt
    tol = 2.0 * dt
    if t.min() > t0 + tol or t.max() < need_hi - tol:
        raise ValueError(
            f"waveform does not cover the coherent window [{t0:.6g}, {need_hi:.6g}] s "
            f"(have [{float(t.min()):.6g}, {float(t.max()):.6g}]). Extend the .tran (settle "
            f"pre-roll + full Twin) or check the export window -- np.interp would otherwise "
            f"clamp the ends and silently corrupt the extracted Zout/PSRR.")

    fgrid, Vout, vdc = _spectrum(t, vout, plan)
    tg = plan["t0"] + plan["dt"] * np.arange(N)
    if vin is not None:
        _, Vin, _ = _spectrum(t, vin, plan)
    else:                                              # ideal supply source -> reconstruct
        van = np.full(N, VDD, float)
        for fr in fa:
            van = van + va * np.sin(2.0 * np.pi * fr * tg)
        _, Vin, _ = _spectrum(tg, van, plan)
    # analytic injected current on the SAME uniform grid -> identical FFT processing
    iinj = np.zeros_like(tg)
    for fr in fb:
        iinj += ib * np.sin(2.0 * np.pi * fr * tg)
    _, Iinj, _ = _spectrum(tg, iinj, plan)

    fp = fgrid[ba]
    P = np.array([Vout[b] / Vin[b] for b in ba]) if ba else np.zeros(0, complex)
    fz = fgrid[bb]
    Z = np.array([Vout[b] / Iinj[b] for b in bb]) if bb else np.zeros(0, complex)

    # leakage / IM floor: median |Vout| at bins away from every tone (and from DC),
    # relative to the median tone level -> how far cross-talk/IM sits below the signal.
    nb = len(Vout)
    near = np.zeros(nb, bool)
    near[:3] = True                                   # exclude a few near-DC bins
    for b in ba + bb:
        near[max(0, b - 2):min(nb, b + 3)] = True
    sig = float(np.median(np.abs(Vout[ba + bb]))) if (ba or bb) else 0.0
    leak = float(np.median(np.abs(Vout[~near]))) if (~near).any() else 0.0
    leak_db = 20.0 * np.log10((leak + 1e-300) / (sig + 1e-300))

    return dict(fz=fz, Z=Z, fp=fp, P=P, vout_dc=vdc, leak_db=leak_db, N=N)


# ------------------------------------------------------------------------- per-band measure
def measure_band(libs, subckt, il, plan, xparams="", va=VA_DEFAULT, ib=IB_DEFAULT, tag="tid"):
    """Run one multitone transient and extract Zout (at B-bins) and PSRR (at A-bins) via
    extract_zp_from_wave. Returns dict(fz, Z, fp, P, vout_dc, leak_db, N, tstop, ...)."""
    libs = list(libs) if isinstance(libs, (list, tuple)) else [libs]
    deck = build_deck(subckt, xparams, plan, il, va, ib)
    r = ng.run(ng.assemble(deck, libs=libs), bench.WORK / tag, outputs=["out.dat"],
               timeout=BAND_TIMEOUT)
    if r["out.dat"] is None:
        raise RuntimeError(f"trans-ID band ({tag}) produced no data:\n{r['_stderr'][-1800:]}")
    a = r["out.dat"][1]
    t, vin, vout = a[:, 0], a[:, 1], a[:, 2]
    d = extract_zp_from_wave(t, vout, plan, ib=ib, va=va, vin=vin)
    d.update(tstop=plan["tstop"], n_tones=len(plan["ba"]) + len(plan["bb"]),
             f_lo=plan["f_lo"], f_hi=plan["f_hi"])
    return d


# ------------------------------------------------------------------- full (multi-band) Z/P
def measure_zp(libs, subckt, il, bands, xparams="", va=VA_DEFAULT, ib=IB_DEFAULT, tagbase="tid"):
    """Run a list of bands (each a kwargs dict for plan_band) and concatenate into wideband
    Zout/PSRR. Returns (z, p, info) where z=np.c_[freq,Re,Im], p=np.c_[freq,Re,Im] (sorted by
    freq) and info carries vout_dc (from the lowest band), per-band diagnostics and totals."""
    bands = sorted(bands, key=lambda b: b["f_lo"])
    zf, zr, zi, pf, pr, pi = [], [], [], [], [], []
    per = []
    vout_dc = None
    for i, b in enumerate(bands):
        plan = plan_band(**b)
        d = measure_band(libs, subckt, il, plan, xparams=xparams, va=va, ib=ib,
                         tag=f"{tagbase}{i}")
        zf.append(d["fz"]); zr.append(d["Z"].real); zi.append(d["Z"].imag)
        pf.append(d["fp"]); pr.append(d["P"].real); pi.append(d["P"].imag)
        if vout_dc is None:                 # lowest band (bands are sorted) -> cleanest DC
            vout_dc = d["vout_dc"]
        per.append({k: d[k] for k in ("f_lo", "f_hi", "N", "tstop", "n_tones", "leak_db")})

    def stack(fs, rs, iss):
        f = np.concatenate(fs); R = np.concatenate(rs); I = np.concatenate(iss)
        o = np.argsort(f)
        return np.c_[f[o], R[o], I[o]]

    z = stack(zf, zr, zi)
    p = stack(pf, pr, pi)
    info = dict(vout_dc=vout_dc, bands=per,
                n_z=z.shape[0], n_p=p.shape[0],
                total_N=int(sum(x["N"] for x in per)),
                worst_leak_db=float(max(x["leak_db"] for x in per)))
    return z, p, info


# ----------------------------------------------------- Verilog-A stimulus emitter (piece A)
def _va_terms(coef, freqs):
    """List of Verilog-A terms `coef*sin(`M_TWO_PI*f*$abstime)`, one per tone. `coef` is a
    parameter NAME so the amplitude stays settable in ADE; only the frequencies are baked
    (compile-time constants). Returns the term strings (caller joins into one statement)."""
    return [f"{coef}*sin(`M_TWO_PI*{float(fr):.12e}*$abstime)" for fr in freqs]


def _write_stim_readme(outdir, plan):
    L = ["Multitone trans-ID STIMULUS fixture -- how to use (auto-generated)",
         "=" * 64, "",
         "ONE reusable Verilog-A fixture per BAND drives BOTH excitations in a single",
         "transient: supply voltage tones on vin (-> PSRR) and injected current tones into",
         "vout (-> Zout). The recipe is band-split (a single combined .tran would blow up the",
         "point count); run each band's .va as its OWN transient. The SAME fixture serves",
         "every load corner -- only the DC load on vout changes.", "",
         "Per corner, per band:",
         "  1. Instantiate the band's module (below) in PARALLEL with your DUT:",
         "        vin  -> the LDO supply node      vout -> the LDO output node",
         "  2. Add an ideal DC current source on vout for the load corner (sets the OP).",
         "  3. Run ONE transient with EXACTLY the listed step/stop, then export v(vin) & v(vout).",
         "  4. Feed the waveforms + plan.json to harness/trans_import.py (-> z/p CSV -> GUI).", "",
         f"Globals: vdd={plan['VDD']:.6g} V, va={plan['va']:.6g} V (supply tone amp), "
         f"ib={plan['ib']:.6g} A (current tone amp).",
         "IMPORTANT: va/ib are settable .va parameters BUT the importer reads them from plan.json "
         "(Iinj is reconstructed analytically). If you change va or ib in ADE, REGENERATE plan.json "
         "with the same values (or the extracted Zout magnitude will be mis-scaled).",
         "NOTE: $abstime => these fixtures are TRANSIENT-ONLY (not PSS/HB-portable).",
         "Noise PSD still needs a separate .noise (a deterministic .tran has no device noise).",
         "", "Bands:"]
    for b in plan["bands"]:
        L.append(f"  band {b['index']}: {b['f_lo']:.4g}..{b['f_hi']:.4g} Hz  "
                 f"module={b['module']}  file={b['va_file']}")
        L.append(f"            run:  {b['tran_cmd']}   "
                 f"(N={b['N']}, {len(b['fa'])} PSRR tones, {len(b['fb'])} Zout tones)")
    pathlib.Path(outdir, "README.txt").write_text("\n".join(L) + "\n", encoding="utf-8")


def emit_stim_va(bands, outdir, va=VA_DEFAULT, ib=IB_DEFAULT, iload=None, module="mtone_stim"):
    """Emit the productionized multitone-trans stimulus as parameterized Verilog-A + a sidecar
    plan.json + a README -- ONE .va per band (the validated recipe is band-split; a single
    combined .tran would reintroduce the timestep x duration blow-up). Each .va is a drop-in
    2-port (vin, vout) fixture for ONE transient; the DC corner load is added separately in
    ADE (same fixture for every corner). Flat-unrolled (no VA arrays/loops) to match the
    proven-compiling emit_va spur idiom; $abstime => .tran-only. Nothing hardcoded: tones and
    windows derive from `bands` (from the variant profile / hf ceiling).

    Returns dict(va_files=[paths], plan_path=path, plan=dict). The plan.json carries, per band,
    the coherent window (N, dt, t0, Twin, binhz) + tone freqs/bins (fa/ba, fb/bb) the importer
    needs for per-band coherent extraction."""
    outdir = pathlib.Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    bands = sorted(bands, key=lambda b: b["f_lo"])
    plans = [plan_band(**b) for b in bands]
    il_val = 0.0 if iload is None else float(iload)

    va_files, bands_json = [], []
    for i, p in enumerate(plans):
        mod = f"{module}_b{i}"
        vterms = _va_terms("va", p["fa"])
        vexpr = "vdd" + "".join("\n               + " + tm for tm in vterms)
        iterms = _va_terms("ib", p["fb"])
        iexpr = "\n               + ".join(iterms) if iterms else "0.0"
        tran_cmd = f"tran {p['dt']:.8e} {p['tstop']:.8e} 0 {p['dt']:.8e}"
        va_txt = f"""// ============================================================
// Multitone trans-ID STIMULUS fixture  (band {i}: {p['f_lo']:.4g}..{p['f_hi']:.4g} Hz)
// Auto-generated by harness/trans_id.py emit_stim_va -- DO NOT hand-edit.
// Drop into ADE in PARALLEL with the DUT (vin=supply node, vout=LDO output), add an
// ideal DC current source on vout for the load corner, and run ONE transient:
//     {tran_cmd}
//   vin  <- supply voltage tones   -> PSRR(f) = Vout(f)/Vin(f)
//   vout <- injected current tones -> Zout(f) = Vout(f)/Iinj(f)
// Export v(vin) & v(vout); feed the waveform + plan.json to harness/trans_import.py.
// NOTE: $abstime => TRANSIENT ONLY (not PSS/HB-portable).
// ============================================================
`include "constants.vams"
`include "disciplines.vams"

module {mod}(vin, vout);
  inout vin, vout;
  electrical vin, vout;
  parameter real vdd = {VDD:.6e};
  parameter real va  = {va:.6e};       // supply tone amplitude [V]
  parameter real ib  = {ib:.6e};       // injected current tone amplitude [A]
  parameter real iload = {il_val:.6e};   // informational: the DC load this run targets [A]

  analog begin
    // supply (PSRR drive): ideal source vdd + sum va*sin(2*pi*fa*t)
    V(vin)  <+ {vexpr};
    // output current injection (Zout drive): -sum ib*sin(2*pi*fb*t) => current INTO vout
    I(vout) <+ -( {iexpr} );
  end
endmodule
"""
        vpath = outdir / f"{mod}.va"
        vpath.write_text(va_txt, encoding="utf-8")
        va_files.append(str(vpath))
        bands_json.append(dict(
            index=i, module=mod, va_file=vpath.name, tran_cmd=tran_cmd,
            f_lo=float(p["f_lo"]), f_hi=float(p["f_hi"]), N=int(p["N"]), dt=float(p["dt"]),
            t0=float(p["t0"]), Twin=float(p["Twin"]), binhz=float(p["binhz"]),
            tstop=float(p["tstop"]),
            fa=[float(x) for x in p["fa"]], ba=[int(b) for b in p["ba"]],
            fb=[float(x) for x in p["fb"]], bb=[int(b) for b in p["bb"]]))

    plan = dict(module=module, VDD=float(VDD), va=float(va), ib=float(ib), iload=il_val,
                bands=bands_json)
    plan_path = outdir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    _write_stim_readme(outdir, plan)
    return dict(va_files=va_files, plan_path=str(plan_path), plan=plan)


# ------------------------------------------------------------------- linearity (IM) gate
def linearity_gate(libs, subckt, il, bands, xparams="", va=VA_DEFAULT, ib=IB_DEFAULT,
                   tagbase="lin"):
    """Half-amplitude rerun -> empirical IM / large-signal detector. For an LTI DUT the
    extracted Zout/PSRR are amplitude-INVARIANT (per-bin ratios cancel the drive level), so
    any change when va/ib are halved flags a nonlinear contribution on a measurement bin
    (2nd-order IM scales 1/4, 3rd-order 1/8 relative to the linear tone's 1/2). Returns the
    max |dB| change for Zout and PSRR -- small => the trans-ID operated in small signal."""
    zf, pf, _ = measure_zp(libs, subckt, il, bands, xparams, va, ib, tagbase + "f")
    zh, ph, _ = measure_zp(libs, subckt, il, bands, xparams, va * 0.5, ib * 0.5, tagbase + "h")
    def dmax(af, ah):
        return float(np.max(np.abs(20 * np.log10(
            (np.abs(af[:, 1] + 1j * af[:, 2]) + 1e-30) /
            (np.abs(ah[:, 1] + 1j * ah[:, 2]) + 1e-30)))))
    return dict(lin_z_db=dmax(zf, zh), lin_p_db=dmax(pf, ph))


# --------------------------------------------------------------------------- quick smoke
def _smoke():
    """Smoke test on base/121u: one modest band, compare extracted Zout/PSRR to the AC ref."""
    import variants
    v = variants.get("base")
    bands = [dict(f_lo=1e6, f_hi=1e8, n_per_dec=12, ppp=12)]
    z, p, info = measure_zp(v["libs"], v["subckt"], "121u", bands, xparams=v["xparams"],
                            tagbase="smoke")
    ref = np.load(ng.ROOT / "results" / "ref" / "base.npz", allow_pickle=True)
    gz = ref["z_121u"]; gp = ref["p_121u"]

    def cinterp(f_to, f_from, Z):
        mag = np.exp(np.interp(np.log(f_to), np.log(f_from), np.log(np.abs(Z) + 1e-30)))
        ph = np.interp(np.log(f_to), np.log(f_from), np.unwrap(np.angle(Z)))
        return mag * np.exp(1j * ph)

    fz, Z = z[:, 0], z[:, 1] + 1j * z[:, 2]
    fp, Pc = p[:, 0], p[:, 1] + 1j * p[:, 2]
    Zg = cinterp(fz, gz[:, 0], gz[:, 1] + 1j * gz[:, 2])
    Pg = cinterp(fp, gp[:, 0], gp[:, 1] + 1j * gp[:, 2])
    ez = 20 * np.log10(np.abs(Z) / np.abs(Zg))
    ezph = np.degrees(np.angle(Z / Zg))
    ep = 20 * np.log10(np.abs(Pc) / np.abs(Pg))
    epph = np.degrees(np.angle(Pc / Pg))
    print(f"info: {info}")
    print(f"{'f[Hz]':>12} | {'|Zt|':>8} {'|Zac|':>8} {'dZdB':>7} {'dZph':>7} | "
          f"{'|Pt|':>9} {'|Pac|':>9} {'dPdB':>7} {'dPph':>7}")
    fall = np.union1d(fz, fp)
    for f in fall:
        zi = np.argmin(np.abs(fz - f)); pi_ = np.argmin(np.abs(fp - f))
        zline = (f"{np.abs(Z[zi]):8.3f} {np.abs(Zg[zi]):8.3f} {ez[zi]:7.2f} {ezph[zi]:7.1f}"
                 if abs(fz[zi] - f) < 1 else f"{'':>8} {'':>8} {'':>7} {'':>7}")
        pline = (f"{np.abs(Pc[pi_]):9.2e} {np.abs(Pg[pi_]):9.2e} {ep[pi_]:7.2f} {epph[pi_]:7.1f}"
                 if abs(fp[pi_] - f) < 1 else f"{'':>9} {'':>9} {'':>7} {'':>7}")
        print(f"{f:12.0f} | {zline} | {pline}")
    print(f"\nZ  mag |err| med {np.median(np.abs(ez)):.3f} dB  max {np.max(np.abs(ez)):.3f} dB  "
          f"| phase med {np.median(np.abs(ezph)):.2f} deg max {np.max(np.abs(ezph)):.2f} deg")
    print(f"PSRR mag |err| med {np.median(np.abs(ep)):.3f} dB  max {np.max(np.abs(ep)):.3f} dB  "
          f"| phase med {np.median(np.abs(epph)):.2f} deg max {np.max(np.abs(epph)):.2f} deg")


if __name__ == "__main__":
    _smoke()
