"""Validate the ghost-cap gate + z_hf cross-check in fit_model.fit_cout_esr.

Background (real 5.8GHz capless Cadence LDO, 2026-06-10): the user's import fit
to composite 268 with Cout "extracted" at 14nF/1mohm against a GT showing a
681ohm Zout peak at 10MHz -- mutually impossible (a real 14nF shunt is 1.1ohm
at 10MHz). Root causes covered here:
  A) a broken z_hf export (wrong node/scale: |Z| ~ mohm-scale garbage) poisons
     the extraction while every per-corner z plot looks fine, AND
  B) a genuinely capless part (HF tail = rds/parasitics, near-real Z) makes the
     -1/(w*ImZ) median land on a huge ghost cap.

Part 1 (REGRESSION): on every existing reference the gate must stay SILENT and
extraction stay bit-identical (margins printed).
Part 2 (scenario A): capless synthetic + garbage z_hf -> import guardrail warns,
fitter ignores z_hf, extraction lands in the pF range.
Part 3 (scenario B): capless synthetic whose HF tail is near-resistive in BOTH
z and z_hf -> ghost gate fires, envelope fallback recovers, model resonance
co-located with the GT peak, residuals sane.

Run:  .venv\\Scripts\\python.exe harness\\validate_capless.py
"""
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
for d in ("harness", "gui", "cadence"):
    p = str(ROOT / d)
    if p not in sys.path:
        sys.path.insert(0, p)

import fit_model  # noqa: E402

TWO_PI = 2 * np.pi
REFDIR = ROOT / "results" / "ref"


def _old_rule(f, Z):
    """The pre-gate extraction (phase-selected median + tail Re), verbatim."""
    cap = np.angle(Z) < -np.pi / 4
    sel = cap if cap.sum() >= 3 else (f > 0.3 * f[-1])
    Cc = float(np.median(-1.0 / (TWO_PI * f[sel] * Z[sel].imag)))
    tail = f > 0.3 * f[-1]
    Rc = float(max(np.median(Z[tail].real), 1e-3))
    return Cc, Rc


def _margins(f, Z, Cc, Rc):
    """(band-median ratio, full-sweep max ratio) of |Z| over the extracted branch --
    the two ghost-gate statistics; thresholds are 4 and 20."""
    cap = np.angle(Z) < -np.pi / 4
    sel = cap if cap.sum() >= 3 else (f > 0.3 * f[-1])
    band = sel & (Z.imag < 0)
    zb = np.abs(Rc + 1.0 / (1j * TWO_PI * f * Cc))
    med = float(np.median(np.abs(Z[band]) / zb[band])) if band.sum() >= 3 else float("nan")
    return med, float(np.max(np.abs(Z) / zb))


def part1_regression():
    print("== part 1: gate must stay silent on every existing reference ==")
    n = 0
    for npz in sorted(REFDIR.glob("*.npz")):
        ref = np.load(npz, allow_pickle=True)
        if "loads" not in ref.files:
            print(f"   {npz.stem:>16}: skipped (no corner layout)")
            ref.close()
            continue
        loads = [str(x) for x in ref["loads"]]
        nom = loads[len(loads) // 2]
        key = f"z_{nom}_hf" if f"z_{nom}_hf" in ref.files else f"z_{nom}"
        g = ref[key]
        f, Z = g[:, 0], g[:, 1] + 1j * g[:, 2]
        Co, Ro = _old_rule(f, Z)
        med, mx = _margins(f, Z, Co, Ro)
        ref.close()
        fit_model.load(vkey=npz.stem)
        Cn, Rn = fit_model.C, fit_model.RC
        if fit_model.CFT != 0.0:
            # the feedthrough gate fired -> Cout is now extracted from the C_ft-
            # de-embedded Z (the gate's purpose), so the old-rule comparison does not
            # apply. Allowed ONLY on the real Target-B digest (clean jwC tail at GHz);
            # every synthetic reference must keep the gate SILENT.
            assert npz.stem == "myldo_digest", \
                f"{npz.stem}: feedthrough gate fired on a synthetic reference"
            print(f"   {npz.stem:>16}: feedthrough gate fired "
                  f"(C_ft={fit_model.CFT*1e15:.1f}fF) -> Cout de-embedded "
                  f"{Co*1e12:.2f}->{Cn*1e12:.2f}pF  [real part, allowed]")
            continue
        assert fit_model.CFT == 0.0, f"{npz.stem}: feedthrough gate not silent"
        true_c = None
        try:
            tc = float(np.load(npz, allow_pickle=True)["meta_cout"])
            true_c = tc if np.isfinite(tc) and tc > 0 else None
        except (KeyError, ValueError):
            pass
        old_broken = (Co <= 0) or (true_c is not None and abs(np.log10(Co / true_c)) > 1)
        if old_broken:
            # the old rule itself was already wrong here (inductive-only test import,
            # or v10_3lc's 10nF-for-200pF multi-LC misread) -> the gate firing is the
            # FIX, not a regression. Composite impact measured separately (score.py).
            print(f"   {npz.stem:>16}: old rule BROKEN (C={Co*1e12:.1f}pF vs true "
                  f"{(true_c or float('nan'))*1e12:.1f}pF) -> now C={Cn*1e12:.2f}pF  "
                  f"band-med={med:5.2f} max={mx:6.2f}  [gate allowed]")
            assert Cn > 0, f"{npz.stem}: fallback still non-physical"
            continue
        same = (Co == Cn) and (Ro == Rn)
        print(f"   {npz.stem:>16}: C={Co*1e12:9.1f}pF ESR={Ro:7.3f}  "
              f"band-med={med:5.2f} max={mx:6.2f}  {'OK' if same else 'CHANGED!'}")
        assert same, f"{npz.stem}: gate changed extraction on an existing reference"
        n += 1
    print(f"   {n} references: extraction byte-identical, gate silent")
    # full fit on representative refs: the adaptive noise bank must NOT trigger
    # (M stays 6 -> the whole fit is computed exactly as before = byte-identical)
    for vk in ("base", "v3_miller", "v8_dlc"):
        fit_model.load(vkey=vk)
        fit_model.fit_all()
        assert len(fit_model.NFK) == 6, f"{vk}: noise bank adapted on an existing reference"
        assert fit_model.NOISE_MODE == "norton" and fit_model.NFKV == [], \
            f"{vk}: hybrid noise gate fired on an existing reference"
    print("   fit check (base/v3/v8): noise bank stayed at 6 sections (mode=norton)\n")


def _synth_capless(resistive_tail=False, noisy=False, feedthrough=None,
                   series_noise=False):
    """Arrays mimicking the user's capless part: rds-dominated Zout (0.8ohm LF floor,
    ~700ohm loop peak @10MHz UGB, slow decay to 40GHz), 70dB DC PSRR with a ~27MHz
    0dB notch. resistive_tail=True flattens the phase above 100MHz to ~-0.06deg
    (scenario B: near-real HF impedance that ghosts the -1/(w*ImZ) read).
    feedthrough=C_ft [F]: add a vin->vout feedthrough cap -- Zout becomes the C_ft-
    shunted Z (the Zout bench AC-grounds vin) and PSRR becomes (ic0 + jwC_ft)*Z_shunted
    with ic0 = H0/Z0 the un-fed-through injection (part 7's gate target).
    series_noise=True (part 8's gate target): loop-shaped output noise from a SERIES
    voltage bank ahead of the regulation branch, Sv^2 = Vn^2*|Zcl/ZA|^2 + (.2nA*|Zcl|)^2
    with ZA = R0/(1+loopT) (the rds-through-the-loop regulation branch, no Cpar --
    consistent with the part's own Zout build, so T = Zcl/ZA = Zol/R0). Two Norton-
    unfittable features (verified numerically: bank stalls at 4.62dB > trigger):
    |Zcl| rises ~prop. f over 10kHz..10MHz while Vn falls, so In = Sv/|Zcl| falls up
    to ~-30dB/dec (steeper than a Lorentzian bank's -20); and the small .2nA Norton
    floor lets the voltage white floor carry THROUGH the Zout peak, where In must DIP
    below its own HF level and come back -- a monotone-down + white current bank
    cannot (the real Target-B failure mode). The hybrid fits it to <0.4dB and
    recovers the true 500/3e4/1.5e6 Hz corners."""
    loads = ["100u", "200u", "300u"]
    nom = "200u"
    R0 = {"100u": 800.0, "200u": 430.0, "300u": 300.0}   # pass-device rds vs load
    CPAR = 2e-12                                          # on-chip output parasitic
    T0, F_UGB = 1000.0, 10e6                              # 60dB loop, 10MHz UGB
    # un-fed-through PSRR HF pole: 5e9 legacy. In the feedthrough scenario the core
    # PSRR must roll off earlier (5e8) -- with the 5e9 pole the synthetic's own
    # CPAR*H0 term contaminates the top 1.5 decades (Im(ic)/w spread 14%, tail-fit
    # RMS 27%, C_LS +25% off) and the gate *correctly* refuses an ambiguous tail.
    FPH = 5e8 if feedthrough is not None else 5e9

    def loopT(f):
        return T0 / ((1 + 1j * f / (F_UGB / T0)) * (1 + 1j * f / 30e6))

    def Z0f(il, f):
        """Un-fed-through closed-loop Zout (the legacy Zcl body)."""
        Zol = 1.0 / (1.0 / R0[il] + 1j * TWO_PI * f * CPAR)
        Z = Zol / (1 + loopT(f))
        if resistive_tail:
            hf = f > 1e8
            Z = np.where(hf, np.abs(Z) * np.exp(-1j * 0.001), Z)
        return Z

    def Zcl(il, f):
        Z = Z0f(il, f)
        if feedthrough is not None:        # C_ft shunts the output (vin AC-grounded)
            Z = 1.0 / (1.0 / Z + 1j * TWO_PI * f * feedthrough)
        return Z

    def Hpsrr(il, f):
        K = 0.316 * (1 + 1j * f / 8.5e6) / (1 + 1j * f / 2.66e7)
        H0 = K / (1 + loopT(f)) / (1 + 1j * f / FPH)
        if feedthrough is None:
            return H0
        ic0 = H0 / Z0f(il, f)              # un-fed-through equivalent injection
        return (ic0 + 1j * TWO_PI * f * feedthrough) * Zcl(il, f)

    f = np.logspace(3, np.log10(4e10), 240)
    fhf = np.logspace(3, np.log10(4e10), 320)
    A = {"loads": np.array(loads), "meta_cout": np.array(CPAR), "meta_esr": np.array(1e-3),
         "spur_F": np.array([]), "spur_twin0": np.array(0.0), "spur_binhz": np.array(15625.0)}
    for il in loads:
        iv = float(il.replace("u", "e-6"))
        Z = Zcl(il, f)
        H = Hpsrr(il, f)
        A[f"z_{il}"] = np.c_[f, Z.real, Z.imag]
        A[f"p_{il}"] = np.c_[f, H.real, H.imag]
        if series_noise:
            # voltage-domain bank (white + 3 Lorentzians @ 500Hz/30kHz/1.5MHz) through
            # the series divider T = Zcl/ZA + a white Norton floor at vout
            ZA = R0[il] / (1 + loopT(f))
            Vn2 = ((3e-8) ** 2 + (8e-6) ** 2 / (1 + (f / 500.0) ** 2)
                   + (5e-7) ** 2 / (1 + (f / 3e4) ** 2)
                   + (1.5e-7) ** 2 / (1 + (f / 1.5e6) ** 2))
            Sv = np.sqrt(Vn2 * np.abs(Z / ZA) ** 2 + (2e-10 * np.abs(Z)) ** 2)
            A[f"noise_{il}"] = np.c_[f, Sv]
        elif noisy:
            # the real-part noise export: LINEAR frequency grid (pnoise binning) ->
            # ~no samples below 100kHz, so without grid equalization the fit simply
            # never sees the steep RTN/flicker tail (-20dB @1kHz failure mode).
            fn = np.linspace(1e3, 4e10, 400)
            Zn = np.abs(Zcl(il, fn))
            In2 = ((2e-9) ** 2 + (5e-7) ** 2 / (1 + (fn / 1.5e3) ** 2) ** 2
                   + (3e-8) ** 2 * (1e3 / fn))
            Sv = np.sqrt(In2) * Zn
            A[f"noise_{il}"] = np.c_[fn, Sv]
        else:
            Sv = np.sqrt((3e-8) ** 2 + (1.2e-7) ** 2 * (1e3 / f) + (np.abs(Z) * 2e-9) ** 2)
            A[f"noise_{il}"] = np.c_[f, Sv]
        t = np.linspace(0, 25e-6, 200)
        A[f"trans_lin_{il}"] = np.c_[t, 1.05 - 0.8 * iv
                                     - 1e-3 * np.exp(-(t - 5e-6) / 2e-6) * (t > 5e-6)]
    Zh = Zcl(nom, fhf)
    Hh = Hpsrr(nom, fhf)
    A[f"z_{nom}_hf"] = np.c_[fhf, Zh.real, Zh.imag]
    A[f"p_{nom}_hf"] = np.c_[fhf, Hh.real, Hh.imag]
    t = np.linspace(0, 25e-6, 200)
    A[f"trans_big_{nom}"] = np.c_[t, 1.05 - 1e-2 * np.exp(-(t - 5e-6) / 2e-6) * (t > 5e-6)]
    A[f"trans_slew_{nom}"] = np.c_[t, 1.05 - 3e-2 * np.exp(-(t - 5e-6) / 2e-6) * (t > 5e-6)]
    idc = np.linspace(1e-6, 400e-6, 60)
    A["dc_loadreg"] = np.c_[idc, 1.05 - 0.8 * idc]
    vdc = np.linspace(1.1, 1.4, 40)
    A["dc_linereg"] = np.c_[vdc, 1.05 + 0 * vdc]
    iddc = np.linspace(1e-6, 1e-3, 80)
    A["dc_dropout"] = np.c_[iddc, np.maximum(1.05 - 0.8 * iddc, 0.2)]
    return A, loads, nom


def _run_pipeline(A, loads, nom, name):
    """CSV -> import_cadence -> fit -> residuals via the GUI core (same path the
    red-zone GUI takes). Returns (core, import warnings)."""
    from ldo_modeler import ModelerCore, Profile

    tmp = ROOT / "work" / f"capless_csv_{name}"
    tmp.mkdir(parents=True, exist_ok=True)
    files = {}
    for il in loads:
        for q in ("z", "p", "noise", "trans_lin"):
            p = tmp / f"{q}_{il}.csv"
            np.savetxt(p, np.asarray(A[f"{q}_{il}"], float), delimiter=",")
            files[(q, il)] = p
    for q, kk in (("z_hf", f"z_{nom}_hf"), ("p_hf", f"p_{nom}_hf")):
        if kk in A:
            p = tmp / f"{q}.csv"
            np.savetxt(p, np.asarray(A[kk], float), delimiter=",")
            files[(q, nom)] = p
    for tag in ("big", "slew"):
        p = tmp / f"trans_{tag}.csv"
        np.savetxt(p, np.asarray(A[f"trans_{tag}_{nom}"], float), delimiter=",")
        files[(f"trans_{tag}", nom)] = p
    for gk in ("dc_loadreg", "dc_linereg", "dc_dropout"):
        p = tmp / f"{gk}.csv"
        np.savetxt(p, np.asarray(A[gk], float), delimiter=",")
        files[(gk, None)] = p
    core = ModelerCore()
    core.profile = Profile(name=f"_capless_{name}", loads=loads, nominal=nom,
                           cout=float(A["meta_cout"]), esr=1e-3, vref=1.05,
                           spur_twin0=0.0, spur_binhz=15625.0)
    path, warns = core.import_data(files)
    for w in warns:
        print(f"   guardrail [{w['level']}] {w['quantity']}: {w['msg'][:90]}")
    core.fit()
    return core, path, warns


def _check_fit(core, nom):
    Cn, Rn = core.result.cout, core.result.esr
    print(f"   extracted: C={Cn*1e12:.2f}pF ESR={Rn:.3f}")
    assert 0.05e-12 < Cn < 100e-12, f"extraction not in the pF range: {Cn*1e12:.1f}pF"
    cg = core.gt_corner(nom)
    pm = core.predict_corner(nom)
    fz = cg["fz"]
    inb = fz <= 1e8
    fg = fz[inb][np.argmax(np.abs(cg["Zg"][inb]))]
    fm = fz[inb][np.argmax(np.abs(pm["Zm"][inb]))]
    print(f"   GT peak @ {fg/1e6:.2f}MHz  model peak @ {fm/1e6:.2f}MHz  (ratio {fm/fg:.2f})")
    assert 0.3 < fm / fg < 3.0, "model resonance not co-located with GT peak"
    perr = np.degrees(np.angle(pm["Zm"] * np.conj(cg["Zg"])))   # wrapped phase difference
    prms = float(np.sqrt(np.mean(perr ** 2)))
    print(f"   Zout phase RMS err = {prms:.1f}deg")
    assert prms < 90.0, f"Zout phase off ({prms:.0f}deg RMS) -- sign flip not corrected?"
    for r in core.fit_residuals():
        print(f"   {r['il']:>5}: zrms={r['zrms']:.2f} prms={r['prms']:.2f} npsd={r['npsd']:.2f} dB")
        assert r["zrms"] < 10.0, f"Zout residual still broken at {r['il']}"


def _cleanup(path):
    try:  # NpzFile holds the handle -> close before unlink on Windows
        if getattr(fit_model, "ref", None) is not None and hasattr(fit_model.ref, "close"):
            fit_model.ref.close()
        path.unlink()
    except OSError:
        pass


def part2_bad_zhf():
    print("== part 2 (scenario A): garbage z_hf export on a capless part ==")
    A, loads, nom = _synth_capless()
    g = A[f"z_{nom}_hf"]
    A[f"z_{nom}_hf"] = np.c_[g[:, 0], g[:, 1] * 1e-4, g[:, 2] * 1e-4]   # wrong node/scale
    core, path, warns = _run_pipeline(A, loads, nom, "badhf")
    assert any(w["quantity"].endswith("_hf") and w["level"] == "warn" for w in warns), \
        "import guardrail did not flag the broken z_hf"
    _check_fit(core, nom)
    _cleanup(path)
    print()


def part3_resistive_tail():
    print("== part 3 (scenario B): genuinely near-resistive HF tail (both sweeps) ==")
    A, loads, nom = _synth_capless(resistive_tail=True)
    core, path, _ = _run_pipeline(A, loads, nom, "restail")
    _check_fit(core, nom)
    _cleanup(path)
    print()


def part4_no_zhf():
    print("== part 4 (the user's actual setup): NO z_hf, z swept straight to 40GHz ==")
    A, loads, nom = _synth_capless(resistive_tail=True)
    for k in (f"z_{nom}_hf", f"p_{nom}_hf"):
        del A[k]
    core, path, warns = _run_pipeline(A, loads, nom, "nozhf")
    assert not any(w["quantity"] == "z_hf" for w in warns), \
        "missing-z_hf info should be suppressed when z reaches past 100MHz"
    _check_fit(core, nom)
    _cleanup(path)
    print()


def part5_sign_flip():
    print("== part 5 (Target-B trap): Zout exported with inverted sign (Z = -V/I) ==")
    A, loads, nom = _synth_capless(resistive_tail=True)
    for k in (f"z_{nom}_hf", f"p_{nom}_hf"):
        del A[k]
    for il in loads:
        g = A[f"z_{il}"]
        A[f"z_{il}"] = np.c_[g[:, 0], -g[:, 1], -g[:, 2]]     # the flipped export
    core, path, warns = _run_pipeline(A, loads, nom, "signflip")
    assert any(w["quantity"] == "z" and "INVERTED" in w["msg"] for w in warns), \
        "sign-flip guardrail did not fire"
    _check_fit(core, nom)
    _cleanup(path)
    print()


def part6_linear_noise_grid():
    print("== part 6 (Target-B noise): LINEAR-grid noise export + steep RTN/flicker tail ==")
    A, loads, nom = _synth_capless(resistive_tail=True, noisy=True)
    for k in (f"z_{nom}_hf", f"p_{nom}_hf"):
        del A[k]
    core, path, _ = _run_pipeline(A, loads, nom, "noisy")
    cg = core.gt_corner(nom)
    pm = core.predict_corner(nom)
    lf_err = abs(20 * np.log10((pm["Sm"][0] + 1e-30) / (cg["Sg"][0] + 1e-30)))
    print(f"   LF noise err @ {cg['fn'][0]:.3g}Hz = {lf_err:.1f}dB (was -20dB-class before "
          f"grid equalization)")
    assert lf_err < 6.0, f"flicker tail still underfit: {lf_err:.1f}dB at the lowest point"
    for r in core.fit_residuals():
        print(f"   {r['il']:>5}: zrms={r['zrms']:.2f} prms={r['prms']:.2f} npsd={r['npsd']:.2f} dB")
        assert r["npsd"] < 4.5, f"noise residual still high at {r['il']}"
    _cleanup(path)
    print()


def part7_feedthrough():
    print("== part 7: gated vin->vout feedthrough cap (C_ft) ==")
    A, loads, nom = _synth_capless(feedthrough=2e-12)
    core, path, _ = _run_pipeline(A, loads, nom, "cft")
    cft = fit_model.CFT
    print(f"   extracted C_ft = {cft*1e15:.1f}fF (true 2000.0fF)")
    assert cft != 0.0, "feedthrough gate did not fire on the feedthrough synthetic"
    assert abs(cft - 2e-12) <= 0.25 * 2e-12, f"C_ft off by >25%: {cft*1e15:.1f}fF"
    assert core.result.cft == cft, "FitResult.cft does not carry the gate value"
    for r in core.fit_residuals():
        print(f"   {r['il']:>5}: zrms={r['zrms']:.2f} prms={r['prms']:.2f} npsd={r['npsd']:.2f} dB")
        assert r["prms"] < 3.0, f"PSRR residual not sane at {r['il']}: {r['prms']:.2f}dB"
    lib, va = core.emit(outdir=ROOT / "work" / "capless_csv_cft")
    txt = lib.read_text()
    assert "Cft vin vout" in txt, "emitted .lib lacks the Cft instance"
    assert ".param CFTP=" in txt, "emitted .lib lacks the CFTP param"
    assert "Cft*ddt(V(vin, vout))" in va.read_text(), "emitted .va lacks the Cft contribution"
    print(f"   emitted {lib.name}: Cft instance + CFTP param present")
    _cleanup(path)
    # negative control: the same synthetic WITHOUT feedthrough must leave the gate silent
    A2, loads2, nom2 = _synth_capless()
    core2, path2, _ = _run_pipeline(A2, loads2, nom2, "cftneg")
    assert fit_model.CFT == 0.0, \
        f"gate fired without feedthrough (C_ft={fit_model.CFT*1e15:.1f}fF)"
    lib2, va2 = core2.emit(outdir=ROOT / "work" / "capless_csv_cftneg")
    assert "Cft" not in lib2.read_text() and "Cft" not in va2.read_text(), \
        "gated-off emit still mentions Cft"
    print("   negative control (no feedthrough): gate silent, emit Cft-free")
    _cleanup(path2)
    print()


def part8_hybrid_noise():
    print("== part 8: gated HYBRID noise structure (series voltage bank in branch A) ==")
    A, loads, nom = _synth_capless(series_noise=True)
    core, path, _ = _run_pipeline(A, loads, nom, "hybrid")
    assert fit_model.NOISE_MODE == "hybrid", \
        "hybrid gate did not fire on the loop-shaped series-noise synthetic"
    assert len(fit_model.NFKV) >= 1, "hybrid engaged but NFKV is empty"
    print(f"   hybrid engaged: {len(fit_model.NFKV)} sections @ fvk[Hz] = "
          + " ".join(f"{x:.3g}" for x in fit_model.NFKV))
    assert core.result.nmode == "hybrid" and core.result.nfkv == fit_model.NFKV, \
        "FitResult does not carry the hybrid mode/corners"
    for r in core.fit_residuals():
        print(f"   {r['il']:>5}: zrms={r['zrms']:.2f} prms={r['prms']:.2f} npsd={r['npsd']:.2f} dB")
        assert r["npsd"] < 2.5, f"hybrid noise residual too high at {r['il']}: {r['npsd']:.2f}dB"
    lib, va = core.emit(outdir=ROOT / "work" / "capless_csv_hybrid")
    txt = lib.read_text()
    assert "Evw  vreg" in txt and "vrgn" in txt, ".lib lacks the series-bank Evw/vrgn fragments"
    assert "Bra  vrgn nA" in txt, ".lib branch A not re-railed to vrgn"
    assert ".param snw" in txt, ".lib lacks the snw param"
    vtxt = va.read_text()
    assert "vrgn" in vtxt and "V(vrgn, vrg) <+ snw*V(nvw, gnd)" in vtxt, \
        ".va lacks the series voltage-bank realization"
    print(f"   emitted {lib.name}: vrgn/Evw fragments present; .va has the series bank")
    _cleanup(path)
    # negative control: part 6's noisy=True synth (steep Norton-fittable tail) must
    # still come out on the legacy Norton path
    A2, loads2, nom2 = _synth_capless(resistive_tail=True, noisy=True)
    for k in (f"z_{nom2}_hf", f"p_{nom2}_hf"):
        del A2[k]
    core2, path2, _ = _run_pipeline(A2, loads2, nom2, "hybridneg")
    assert fit_model.NOISE_MODE == "norton" and fit_model.NFKV == [], \
        "negative control: hybrid engaged on part 6's Norton-fittable synthetic"
    lib2, va2 = core2.emit(outdir=ROOT / "work" / "capless_csv_hybridneg")
    assert "vrgn" not in lib2.read_text() and "vrgn" not in va2.read_text(), \
        "gated-off emit still mentions vrgn"
    print("   negative control (part-6 noisy synth): mode=norton, emit vrgn-free")
    _cleanup(path2)
    print()


if __name__ == "__main__":
    part1_regression()
    part2_bad_zhf()
    part3_resistive_tail()
    part4_no_zhf()
    part5_sign_flip()
    part6_linear_noise_grid()
    part7_feedthrough()
    part8_hybrid_noise()
    print("validate_capless PASS")
