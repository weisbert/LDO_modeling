"""SYSTEM-LEVEL acceptance test: LDO + RF buffer @ carrier (audit R4 / D4#1, merges R6#3).

The project's deliverable is COHERENT CARRIER-SIDEBAND FIDELITY, which score.py never
measures: it grades per-block, per-corner (Zout/PSRR/noise/trans/spur) entirely IN-SAMPLE,
and its only carrier-ish check is a single 8 MHz LOAD-tone pass/fail gate. This module
builds the missing end-to-end test -- the same representative RF buffer is dropped on the
output of BOTH the ground-truth LDO and the EMITTED behavioral model, a transient is run,
and the COMPLEX spectrum (magnitude AND phase) of v(vout) at the carrier f_c and the
sidebands f_c +/- Delta is compared GT-vs-model.

THE PHYSICS (why the buffer must be a vout-dependent mixer)
  Above the loop UGB the LDO is LTI -> it maps a tone at X to output only at X. A sideband
  at f_c +/- Delta therefore CANNOT come from load/supply tones alone; it requires the
  BUFFER to mix. So the buffer's carrier-rate current is modulated by vout:
        i_buf(t) = I0*(1 + k*(V(vout) - Vout_dc)) * sin(2*pi*f_c*t)
                       \________ the mixer (supply pushing) ________/
  A supply-ripple aggressor at Delta on vin (Vin SIN(1.05 vrip Delta)) makes vout ripple at
  Delta via PSRR(Delta); the buffer mixes it onto f_c -> sidebands at f_c +/- Delta whose
  level is Zout(f_c +/- Delta)*I0*k*PSRR(Delta)*vrip. The SAME buffer+aggressor drive GT and
  model, so the sideband differs only by the LDO's Zout(f_c+/-Delta)*PSRR(Delta) -- a clean
  coherent-vector-sum test. One testbench -> three diagnostics:
    f_c       carrier ripple  = Zout(f_c)*I0          (the R6#3 "no buffer ripple" check)
    Delta     baseband        = PSRR(Delta)*vrip      (supply rejection)
    f_c+/-Del coherent sideband (the deliverable)     (mag + phase + upper/lower asymmetry)

VALIDITY ENVELOPE (detect-don't-assume, R6#3): the model's Zout/PSRR are characterized only
to the *_hf ceiling (500 MHz at the nominal corner, 100 MHz off-nominal). If f_c +/- Delta
exceeds the characterized ceiling at the corner used, the synthesized network is EXTRAPOLATING
(R6#3 "no ripple" silent false-pass), so the test reports OUT_OF_ENVELOPE instead of a verdict.

SMALL-SIGNAL VALIDITY (detect-don't-assume): the model is LTI, so its deliverable is the
small-signal carrier-sideband response. A fixed vrip can over-drive the GT past its linear range
(e.g. a sharp >0dB PSRR peak -> a large output ripple -> regulator compression), where the LTI
model CORRECTLY does not follow -- a fixed-vrip test would then mis-report the model as FAIL. So
vrip is AUTO-CALIBRATED to keep the predicted output ripple at Delta within LIN_FRAC_RIPPLE of
Vout (it only tightens for variants whose PSRR peaks above ~0dB at Delta; an explicit --vrip is
honored). An empirical GT linearity probe (a 2nd GT run at vrip/KLIN) then checks the GT actually
scales linearly; if not, the test reports LARGE_SIGNAL instead of a verdict -- it never blames the
model for GT large-signal compression.

General by construction (no 304MHz/8-24MHz/121uA/"3 corners" hardcoded): f_c/Delta/I0/k/vrip
and the corner are profile/CLI parameters with envelope-derived defaults (f_c defaults to a
fraction of the characterized ceiling). This is a PURE INCREMENT: it does not touch fit_model's
fit/emit nor score's composite -- it fits+emits to results/systest/ and runs both sides live,
exactly like crossval.offgrid.

    python systest.py --variant base                 # one DUT
    python systest.py --all                           # whole registry -> matrix.md
    python systest.py --variant base --fc 304e6 --delta 1e6
    python systest.py --variant base --k 0            # mixer A/B sanity (sidebands must vanish)
"""
import argparse
import json
import math
import numpy as np

import ng
import bench
import variants
import fit_model
import score as scoremod

OUT = ng.ROOT / "results" / "systest"

# --- stimulus defaults (general; all overridable per-variant or via CLI) ------------------
FC_FRAC = 0.6        # default carrier = FC_FRAC * characterized ceiling (envelope-derived)
DELTA_DIV = 300.0    # default sideband offset Delta ~ f_c / DELTA_DIV (snapped to 1eN)
IBUF_FRAC = 1.0      # buffer carrier-current amplitude I0 = IBUF_FRAC * nominal load current
K_PUSH = 10.0        # buffer supply-push coeff k [1/V] (i_buf gain vs its rail ripple)
VRIP = 10e-3         # supply-ripple aggressor amplitude on vin [V]

# --- coherent-sampling knobs --------------------------------------------------------------
NBEAT = 8            # window = NBEAT/Delta -> carrier<->sideband separation = NBEAT bins
                     # (keeps the Hann skirt of the large carrier well below the sideband)
PTS_PER_PERIOD = 12  # uniform resample density at the carrier (>= a clean tone capture)
NSET = 2             # settling pre-roll = NSET full windows (integer -> stays coherent)
SYS_TIMEOUT = 600    # ngspice timeout [s] for the (long, fine-dt) carrier transient

# --- first-cut verdict thresholds (this round MEASURES; thresholds are informational) -----
GATE_MAG_DB = 3.0
GATE_PH_DEG = 30.0

# --- linearity / small-signal-validity knobs (detect-don't-assume; all dimensionless) -----
# The model is LTI, so its deliverable is the SMALL-SIGNAL carrier-sideband response. With a
# fixed vrip the test can over-drive the GT past its linear range (e.g. a sharp >0dB PSRR peak
# -> a large output ripple -> regulator compression), where the LTI model CORRECTLY does not
# follow -- mis-reporting the model as FAIL. So (1) auto-calibrate vrip to keep the predicted
# output ripple small-signal, and (2) empirically check GT linearity and withhold the verdict
# (status LARGE_SIGNAL) when the GT is being driven nonlinear, exactly like OUT_OF_ENVELOPE.
LIN_FRAC_RIPPLE = 0.01   # auto-calibrate vrip so predicted vout ripple at Delta <= this * Vout
KLIN = 4.0               # GT linearity probe: extra GT-only runs at vrip/KLIN and vrip=0
LIN_TOL_DB = 1.0         # GT baseband gain change > this between vrip and vrip/KLIN -> nonlinear
LIN_MIN_SNR = 8.0        # only judge linearity when the GT response is > this x the FFT floor

# Optional per-variant overrides. Empty by default -> everything is envelope-derived, so the
# tool stays general. Add entries like {"myldo": {"fc": 5.8e9, "delta": 10e6}} for real parts.
SYS_PROFILE = {}


# ----------------------------------------------------------------- profile / freq planning
def _snap(x):
    """Round x to one significant figure on a 1eN grid (clean, commensurate Delta)."""
    if x <= 0:
        return x
    e = math.floor(math.log10(x))
    return round(x / 10 ** e) * 10 ** e


def _envelope(ref, corner, nominal):
    """Max characterized frequency the MODEL's Zout/PSRR is valid to at this corner: the
    *_hf ceiling (~500 MHz) only exists at the nominal corner; off-nominal stops at the
    base AC ceiling (~100 MHz)."""
    files = set(ref.files)
    if corner == nominal and f"z_{nominal}_hf" in files:
        return float(ref[f"z_{nominal}_hf"][:, 0].max())
    if f"z_{corner}" in files:
        return float(ref[f"z_{corner}"][:, 0].max())
    return float(ref[f"z_{nominal}"][:, 0].max())


def _profile(vkey, ref, nominal, ov):
    """Resolve f_c/Delta/I0/k/vrip/corner: CLI override (ov) > SYS_PROFILE[vkey] > envelope
    default. f_c defaults to FC_FRAC * the characterized ceiling (so the Target-A 304 MHz
    carrier falls out of a 500 MHz nominal ceiling without any 304e6 literal)."""
    p = dict(SYS_PROFILE.get(vkey, {}))
    corner = ov.get("corner") or p.get("corner") or nominal
    Inom = ng.amps(corner)
    f_hi = _envelope(ref, corner, nominal)
    fc = ov.get("fc") or p.get("fc") or FC_FRAC * f_hi
    delta = ov.get("delta") or p.get("delta") or _snap(fc / DELTA_DIV)
    ibuf = ov.get("ibuf") or p.get("ibuf") or IBUF_FRAC * Inom
    k = K_PUSH if ov.get("k") is None else ov["k"]
    k = p.get("k", k)
    vrip = ov.get("vrip") or p.get("vrip") or VRIP
    # was vrip set explicitly (CLI/profile)? -> honor it as-is (user can force large-signal to
    # exercise the linearity gate); otherwise it is just the default cap for auto-calibration.
    vrip_explicit = ov.get("vrip") is not None or "vrip" in p
    return dict(corner=corner, Inom=Inom, f_hi=f_hi, fc=float(fc), delta=float(delta),
                ibuf=float(ibuf), k=float(k), vrip=float(vrip),
                vrip_explicit=bool(vrip_explicit))


def _plan(fc, delta, nbeat=NBEAT, ppp=PTS_PER_PERIOD, nset=NSET):
    """Coherent-sampling plan: snap f_c to a multiple of Delta so f_c, f_c+/-Delta, Delta all
    land on exact FFT bins separated by `nbeat` bins; pick dt for >=ppp samples/carrier-period
    with an integer N samples per window (N*dt == Twin exactly)."""
    delta = float(delta)
    mc = max(1, round(fc / delta))
    fc_s = mc * delta
    Twin = nbeat / delta                 # bin spacing binhz = delta/nbeat
    binhz = delta / nbeat
    N = int(round(Twin / (1.0 / (ppp * fc_s))))
    N += N % 2                           # even N (cosmetic)
    dt = Twin / N
    t0 = nset * Twin
    tstop = t0 + Twin
    b = lambda fr: int(round(fr / binhz))
    bins = dict(carrier=b(fc_s), lower=b(fc_s - delta), upper=b(fc_s + delta), base=b(delta))
    return dict(fc=fc_s, delta=delta, dt=dt, tstop=tstop, t0=t0, Twin=Twin, N=N,
                binhz=binhz, nbeat=nbeat, bins=bins)


# -------------------------------------------------------------------------- testbench / FFT
def _deck(libs, subckt, xparams, plan, ibuf, k, vrip, Inom, voutdc):
    """LDO DUT + supply-ripple aggressor on vin + vout-dependent (mixing) buffer on vout."""
    w = 2.0 * math.pi * plan["fc"]
    gm = ibuf * k                        # supply-push transconductance (the mixer term)
    # i_buf = I0*sin(w t) + gm*(V(vout)-Vout_dc)*sin(w t)   (current sunk from vout)
    bexpr = (f"{ibuf:.8e}*sin({w:.10e}*time)"
             f"+{gm:.8e}*(V(vout)-({voutdc:.8e}))*sin({w:.10e}*time)")
    return f"""* system test: LDO + buffer @ carrier
{bench.xline(libs, subckt, xparams)}
Vin vin 0 DC 1.05 SIN(1.05 {vrip:.8e} {plan['delta']:.8e})
Iload vout 0 DC {Inom:.8e}
Bbuf vout 0 I = {bexpr}
.control
set wr_singlescale
tran {plan['dt']:.8e} {plan['tstop']:.8e} 0 {plan['dt']:.8e}
wrdata out.dat v(vout)
quit
.endc
.end
"""


def _run_tb(libs, subckt, xparams, plan, ibuf, k, vrip, Inom, voutdc, tag):
    libs = list(libs) if isinstance(libs, (list, tuple)) else [libs]
    tb = _deck(libs, subckt, xparams, plan, ibuf, k, vrip, Inom, voutdc)
    r = ng.run(ng.assemble(tb, libs=libs), bench.WORK / f"sys_{tag}",
               outputs=["out.dat"], timeout=SYS_TIMEOUT)
    if r["out.dat"] is None:
        raise RuntimeError(f"system tran ({tag}) failed:\n{r['_stderr'][-1500:]}")
    a = r["out.dat"][1]
    return a[:, 0], a[:, 1]


def _spectrum(t, v, plan):
    """Resample v over the coherent window [t0, t0+Twin) onto N uniform points and return the
    single-sided COMPLEX spectrum (Hann, amp-corrected). Bins land exactly on f_c/f_c+/-Delta."""
    N, Twin, t0 = plan["N"], plan["Twin"], plan["t0"]
    dt = Twin / N
    tg = t0 + dt * np.arange(N)
    vg = np.interp(tg, t, v)
    w = np.hanning(N)
    V = np.fft.rfft((vg - vg.mean()) * w) * (2.0 / N) / w.mean()
    f = np.fft.rfftfreq(N, dt)
    return f, V


def _bin(V, plan, key):
    return complex(V[plan["bins"][key]])


# ------------------------------------------------------------------------- metrics / report
def _db(num, den):
    return float(20.0 * np.log10((abs(num) + 1e-300) / (abs(den) + 1e-300)))


def _phdeg(c):
    return float(np.degrees(np.angle(c)))


def _cmp(Vm, Vg, plan, key):
    """GT-vs-model at one bin: model/GT magnitude error [dB], phase error [deg], plus the raw
    levels relative to the GT carrier [dBc] for both sides."""
    m, g = _bin(Vm, plan, key), _bin(Vg, plan, key)
    cg = _bin(Vg, plan, "carrier")
    cm = _bin(Vm, plan, "carrier")
    return dict(mag_db=_db(m, g), ph_deg=_phdeg(m / (g + 1e-300)),
                gt_dbc=_db(g, cg), model_dbc=_db(m, cm),
                gt_abs=float(abs(g)), model_abs=float(abs(m)))


def _gate(d):
    return bool(abs(d["mag_db"]) <= GATE_MAG_DB and abs(d["ph_deg"]) <= GATE_PH_DEG)


def measure_system(res, vkey, ref, ov, _print_dbg=False):
    """Fit+emit the model, resolve the stimulus profile + coherent plan, run GT and model
    through the buffer testbench, return the full comparison dict (no I/O)."""
    nominal = res.nominal
    pr = _profile(vkey, ref, nominal, ov)
    plan = _plan(pr["fc"], pr["delta"])
    corner = pr["corner"]

    # validity-envelope gate (detect-don't-assume): refuse a verdict above the ceiling
    fmax_test = plan["fc"] + plan["delta"]
    in_env = fmax_test <= pr["f_hi"]

    # Vout DC operating point for the buffer mixer reference (sideband-insensitive to its
    # exact value -> a small error only nudges the carrier amplitude). From GT dc_loadreg.
    voutdc = 0.0
    if "dc_loadreg" in set(ref.files):
        dc = ref["dc_loadreg"]
        voutdc = float(np.interp(pr["Inom"], dc[:, 0], dc[:, 1]))

    # analytic block decomposition (no sim) -> localizes which block drives any gap AND drives
    # the small-signal stimulus calibration below. Diagnostic for the bands; in-sample.
    P = res.P[corner]
    farr = np.array([plan["fc"], plan["fc"] - plan["delta"], plan["fc"] + plan["delta"], plan["delta"]])
    pred = fit_model.predict(P, farr, res.nfk)
    Zc, Zl, Zu, _ = np.abs(pred["Zout"])
    _, _, _, Pd = np.abs(pred["PSRR"])                 # |PSRR(Delta)| (model)

    # --- small-signal stimulus auto-calibration (detect-don't-assume, default) -------------
    # Keep the PREDICTED output ripple at Delta within LIN_FRAC_RIPPLE of Vout so the test
    # measures the LTI deliverable, not GT large-signal compression. vout_ref from the GT DC
    # curve (fallback: the fitted regulated reference). Only engages when |PSRR(Delta)| is large
    # enough that the default vrip would over-drive (>~0dB); an explicit --vrip is kept as-is.
    vout_ref = voutdc if voutdc > 0 else float(P.get("vreg", res.vref))
    vrip_auto = LIN_FRAC_RIPPLE * vout_ref / max(Pd, 1e-6)
    vrip = pr["vrip"] if pr["vrip_explicit"] else min(pr["vrip"], vrip_auto)
    swing_frac = float(Pd * vrip / max(vout_ref, 1e-30))   # predicted vout ripple / Vout

    name = "ldo_model" if vkey == "base" else f"ldo_{vkey}"
    OUT.mkdir(parents=True, exist_ok=True)
    syslib = OUT / f"{name}.lib"
    fit_model.emit(res.P, syslib)                    # non-invasive: under results/systest

    v = variants.get(vkey)
    common = dict(plan=plan, ibuf=pr["ibuf"], k=pr["k"], vrip=vrip,
                  Inom=pr["Inom"], voutdc=voutdc)
    tg, vg = _run_tb(v["libs"], v["subckt"], v["xparams"], tag=f"{vkey}_gt", **common)
    tm, vm = _run_tb([syslib], "ldo_model", f"iload={corner}", tag=f"{vkey}_md", **common)
    fG, VG = _spectrum(tg, vg, plan)
    fM, VM = _spectrum(tm, vm, plan)

    # --- empirical GT linearity gate: extra GT-only runs at vrip/KLIN and at vrip=0. The
    # baseband RESPONSE to the supply ripple (= PSRR(Delta)*vrip) must scale with vrip. The
    # vrip=0 run isolates any vrip-INDEPENDENT content in the baseband bin (intrinsic spurs,
    # DC drift) which is COMPLEX-subtracted first, so a spur landing on Delta cannot masquerade
    # as nonlinearity. A residual gain change > LIN_TOL_DB means the GT is being driven past its
    # linear range -> outside the LTI model's scope -> status LARGE_SIGNAL (verdict withheld);
    # the test never blames the model for GT compression. Model side is LTI by construction.
    vrip_sm = vrip / KLIN
    tg2, vg2 = _run_tb(v["libs"], v["subckt"], v["xparams"], tag=f"{vkey}_lin",
                       **dict(common, vrip=vrip_sm))
    tg0, vg0 = _run_tb(v["libs"], v["subckt"], v["xparams"], tag=f"{vkey}_lin0",
                       **dict(common, vrip=0.0))
    _, VG2 = _spectrum(tg2, vg2, plan)
    _, VG0 = _spectrum(tg0, vg0, plan)
    cb0 = _bin(VG0, plan, "base")                      # vrip-independent baseband content (spur)
    resp_big = _bin(VG, plan, "base") - cb0            # GT PSRR response at vrip      (spur out)
    resp_sm = _bin(VG2, plan, "base") - cb0            # GT PSRR response at vrip/KLIN (spur out)
    floorG = _floor(VG, plan)
    if abs(resp_big) > LIN_MIN_SNR * floorG:           # only judge when the response is sizeable
        lin_err_db = float(abs(_db(resp_big, resp_sm) - 20.0 * np.log10(KLIN)))
        linear = bool(lin_err_db <= LIN_TOL_DB)
    else:                                              # too small to be large-signal -> linear
        lin_err_db = 0.0
        linear = True

    bands = {key: _cmp(VM, VG, plan, key) for key in ("carrier", "lower", "upper", "base")}

    # Zout(f_c) read straight off the carrier ripple (ripple = I0*|Zout(fc)|): a direct
    # GT-vs-model carrier-band Zout comparison as a byproduct.
    zout_fc = dict(gt=bands["carrier"]["gt_abs"] / pr["ibuf"],
                   model=bands["carrier"]["model_abs"] / pr["ibuf"],
                   db=bands["carrier"]["mag_db"])
    # upper/lower sideband asymmetry (a real hot-S diagnostic), per side
    asym = dict(gt_db=_db(_bin(VG, plan, "upper"), _bin(VG, plan, "lower")),
                model_db=_db(_bin(VM, plan, "upper"), _bin(VM, plan, "lower")))

    vrd = Pd * vrip                                    # baseband ripple at vout (PSRR*vrip)
    analytic = dict(
        carrier_V=float(pr["ibuf"] * Zc),
        base_V=float(vrd),
        sb_lower_V=float(Zl * pr["ibuf"] * pr["k"] * vrd),
        sb_upper_V=float(Zu * pr["ibuf"] * pr["k"] * vrd),
        Zout_fc=float(Zc), PSRR_delta=float(Pd))

    # mixer self-check: sideband must sit above the FFT numerical floor (estimate the floor as
    # the median |V| over bins away from all tones).
    floor = _floor(VM, plan)
    sb_snr_db = min(_db(_bin(VM, plan, "lower"), floor), _db(_bin(VM, plan, "upper"), floor))

    # verdict withheld unless BOTH in-frequency-envelope AND the GT is linear at this drive
    gated = in_env and linear
    status = "OUT_OF_ENVELOPE" if not in_env else ("LARGE_SIGNAL" if not linear else "OK")
    passes = {k: _gate(bands[k]) for k in bands} if gated else {k: None for k in bands}
    ok = all(passes.values()) if gated else None

    rep = dict(
        variant=vkey, corner=corner, in_envelope=bool(in_env),
        linear=linear, status=status,
        profile=dict(fc=plan["fc"], delta=plan["delta"], ibuf=pr["ibuf"], k=pr["k"],
                     vrip=float(vrip), vrip_auto=float(vrip_auto),
                     vrip_explicit=bool(pr["vrip_explicit"]), swing_frac=swing_frac,
                     Inom=pr["Inom"], voutdc=voutdc, vout_ref=float(vout_ref), f_hi=pr["f_hi"]),
        plan=dict(dt=plan["dt"], tstop=plan["tstop"], Twin=plan["Twin"], N=plan["N"],
                  binhz=plan["binhz"], nbeat=plan["nbeat"]),
        bands=bands, zout_fc=zout_fc, asym=asym, analytic=analytic,
        linearity=dict(klin=KLIN, vrip_small=float(vrip_sm), lin_err_db=lin_err_db,
                       tol_db=LIN_TOL_DB, resp_big_V=float(abs(resp_big)),
                       resp_small_V=float(abs(resp_sm)), spur_ref_V=float(abs(cb0))),
        floor_V=float(floor), sb_snr_db=float(sb_snr_db),
        envelope=dict(fmax_test=float(fmax_test), f_hi=float(pr["f_hi"]),
                      ratio=float(fmax_test / pr["f_hi"])),
        passes=passes, pass_=ok, lib=str(syslib))
    if _print_dbg:
        print(f"   [dbg {vkey}] rc ok; carrier |Vg|={bands['carrier']['gt_abs']*1e6:.3f}uV "
              f"|Vm|={bands['carrier']['model_abs']*1e6:.3f}uV  sb_snr={sb_snr_db:.1f}dB")
    return rep


def _floor(V, plan):
    """Numerical-floor estimate: median |V| over bins not adjacent to any tone."""
    n = len(V)
    mask = np.ones(n, bool)
    for key in ("carrier", "lower", "upper", "base"):
        b = plan["bins"][key]
        mask[max(0, b - 2):b + 3] = False
    mask[:2] = False
    vals = np.abs(V[mask])
    return float(np.median(vals)) if vals.size else 1e-18


# --------------------------------------------------------------------------------- runner
def run(vkey, ov=None, _print=True):
    """Fit the variant ONCE, run the system test, write+print the report. Returns the dict."""
    ov = ov or {}
    ref = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
    res = fit_model.fit_variant(vkey)
    rep = measure_system(res, vkey, ref, ov)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{vkey}.json").write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    if _print:
        _report(rep)
    return rep


def _report(rep):
    v = rep["variant"]
    print(f"\n{'='*92}\nSYSTEM TEST  LDO+buffer@carrier  GT vs emitted model  variant='{v}'\n{'='*92}")
    pf, pl = rep["profile"], rep["plan"]
    cal = "set" if pf.get("vrip_explicit") else "auto"
    print(f"  carrier f_c={pf['fc']/1e6:.3f}MHz  Delta={pf['delta']/1e6:.4f}MHz  corner={rep['corner']}"
          f"  I0={pf['ibuf']*1e6:.1f}uA  k={pf['k']:.3g}/V")
    print(f"  vrip={pf['vrip']*1e3:.3f}mV ({cal}; predicted vout ripple {pf.get('swing_frac',0)*100:.2f}%Vout)"
          f"   plan: dt={pl['dt']:.2e}s tstop={pl['tstop']*1e6:.2f}us N={pl['N']} bin={pl['binhz']/1e3:.2f}kHz"
          f"  f_hi={pf['f_hi']/1e6:.0f}MHz")

    if not rep["in_envelope"]:
        e = rep["envelope"]
        print(f"\n  >>> OUT_OF_ENVELOPE: f_c+Delta={e['fmax_test']/1e6:.1f}MHz exceeds the characterized "
              f"ceiling {e['f_hi']/1e6:.0f}MHz ({e['ratio']:.2f}x). Model Zout/PSRR there is EXTRAPOLATED "
              f"-> NO verdict (this is the R6#3 silent-false-pass guard, not a pass).")
    elif not rep["linear"]:
        ln = rep["linearity"]
        print(f"\n  >>> LARGE_SIGNAL: the GT is NONLINEAR at vrip={pf['vrip']*1e3:.3f}mV (baseband gain "
              f"shifts {ln['lin_err_db']:.1f}dB vs the vrip/{ln['klin']:.0f} probe, >{ln['tol_db']:.0f}dB). "
              f"Output ripple has pushed the GT past its linear range, where the LTI model (correctly) does "
              f"not follow -> NO verdict. Lower --vrip to measure the LTI deliverable.")

    print("\n  band            |  model/GT mag   phase   |  GT level   model level   (re carrier)")
    for key, lbl in (("carrier", "carrier f_c"), ("lower", "sideband f_c-D"),
                     ("upper", "sideband f_c+D"), ("base", "baseband  D")):
        d = rep["bands"][key]
        gate = rep["passes"][key]
        tag = "ok" if gate else ("**" if gate is not None else "  ")
        print(f"  {lbl:<15} | {d['mag_db']:+7.2f} dB {d['ph_deg']:+7.1f} {tag:>3} | "
              f"{d['gt_dbc']:+8.2f}dBc {d['model_dbc']:+8.2f}dBc")

    z = rep["zout_fc"]
    print(f"\n  Zout(f_c):  GT={z['gt']:.4f}ohm  model={z['model']:.4f}ohm  ({z['db']:+.2f} dB)")
    a = rep["asym"]
    print(f"  sideband asymmetry (upper-lower):  GT={a['gt_db']:+.2f}dB  model={a['model_db']:+.2f}dB")
    an = rep["analytic"]
    print(f"  analytic blocks: |Zout(fc)|={an['Zout_fc']:.4f}ohm  |PSRR(D)|={20*np.log10(an['PSRR_delta']+1e-30):.1f}dB"
          f"  -> expected sb_lower~{an['sb_lower_V']*1e9:.2f}nV")
    print(f"  mixer floor: sideband SNR (model) = {rep['sb_snr_db']:.1f} dB above the FFT floor "
          f"({rep['floor_V']*1e9:.2f}nV)")
    ln = rep["linearity"]
    print(f"  GT linearity: baseband gain change {ln['lin_err_db']:.2f}dB at vrip/{ln['klin']:.0f} "
          f"(tol {ln['tol_db']:.0f}dB) -> {'LINEAR' if rep['linear'] else 'NONLINEAR'}")

    if rep["in_envelope"] and rep["linear"]:
        print(f"\n  >>> verdict (first-cut |mag|<={GATE_MAG_DB}dB & |phase|<={GATE_PH_DEG}deg): "
              f"{'PASS' if rep['pass_'] else 'FAIL'}  "
              f"(carrier+sidebands: {sum(1 for p in rep['passes'].values() if p)}/4 within threshold)")


def _matrix(reps):
    cols = ["variant", "corner", "f_c(MHz)", "vrip(mV)", "env", "status", "carrier_dB",
            "sb_lo_dB", "sb_hi_dB", "sb_phase_hi", "Zout_fc_dB", "sb_snr_dB", "verdict"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in reps:
        if r is None:
            continue
        b = r["bands"]
        env = "in" if r["in_envelope"] else "OUT"
        gated = r["in_envelope"] and r["linear"]
        verdict = ("PASS" if r["pass_"] else "FAIL") if gated else "n/a"
        cells = [r["variant"], r["corner"], f"{r['profile']['fc']/1e6:.1f}",
                 f"{r['profile']['vrip']*1e3:.3f}", env, r["status"],
                 f"{b['carrier']['mag_db']:+.2f}", f"{b['lower']['mag_db']:+.2f}",
                 f"{b['upper']['mag_db']:+.2f}", f"{b['upper']['ph_deg']:+.1f}",
                 f"{r['zout_fc']['db']:+.2f}", f"{r['sb_snr_db']:.1f}", verdict]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    ap.add_argument("--all", action="store_true", help="run the whole variant registry")
    ap.add_argument("--strict", action="store_true", help="exit nonzero if any in-envelope verdict FAILs")
    ap.add_argument("--fc", type=float, default=None, help="carrier [Hz] (default: 0.6 x ceiling)")
    ap.add_argument("--delta", type=float, default=None, help="sideband offset [Hz]")
    ap.add_argument("--ibuf", type=float, default=None, help="buffer carrier current I0 [A]")
    ap.add_argument("--k", type=float, default=None, help="buffer supply-push coeff [1/V] (0 = no mixer)")
    ap.add_argument("--vrip", type=float, default=None, help="supply-ripple aggressor amplitude [V]")
    ap.add_argument("--corner", default=None, help="load corner key (default: nominal)")
    a = ap.parse_args()
    ov = {k: getattr(a, k) for k in ("fc", "delta", "ibuf", "k", "vrip", "corner")
          if getattr(a, k) is not None}

    keys = list(variants.VARIANTS.keys()) if a.all else [a.variant]
    reps = []
    for k in keys:
        try:
            reps.append(run(k, ov=ov))
        except Exception as e:                  # one bad variant must not kill the matrix
            print(f"!!! {k}: {type(e).__name__}: {e}")
            reps.append(None)

    if a.all:
        OUT.mkdir(parents=True, exist_ok=True)
        md = "# System-test matrix (LDO+buffer@carrier, GT vs emitted model)\n\n" + _matrix(reps) + "\n"
        (OUT / "systest_matrix.md").write_text(md, encoding="utf-8")
        print("\n" + md)
        print(f"wrote {OUT/'systest_matrix.md'} + per-variant JSON")

    if a.strict:
        # LARGE_SIGNAL / OUT_OF_ENVELOPE are NOT failures (verdict withheld); a strict FAIL is
        # only an in-envelope, GT-linear case that misses the band thresholds.
        bad = any(r is None or (r["in_envelope"] and r["linear"] and not r["pass_"]) for r in reps)
        raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
