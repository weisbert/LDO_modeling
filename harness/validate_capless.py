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
    print(f"   {n} references: extraction byte-identical, gate silent\n")


def _synth_capless(resistive_tail=False):
    """Arrays mimicking the user's capless part: rds-dominated Zout (0.8ohm LF floor,
    ~700ohm loop peak @10MHz UGB, slow decay to 40GHz), 70dB DC PSRR with a ~27MHz
    0dB notch. resistive_tail=True flattens the phase above 100MHz to ~-0.06deg
    (scenario B: near-real HF impedance that ghosts the -1/(w*ImZ) read)."""
    loads = ["100u", "200u", "300u"]
    nom = "200u"
    R0 = {"100u": 800.0, "200u": 430.0, "300u": 300.0}   # pass-device rds vs load
    CPAR = 2e-12                                          # on-chip output parasitic
    T0, F_UGB = 1000.0, 10e6                              # 60dB loop, 10MHz UGB

    def loopT(f):
        return T0 / ((1 + 1j * f / (F_UGB / T0)) * (1 + 1j * f / 30e6))

    def Zcl(il, f):
        Zol = 1.0 / (1.0 / R0[il] + 1j * TWO_PI * f * CPAR)
        Z = Zol / (1 + loopT(f))
        if resistive_tail:
            hf = f > 1e8
            Z = np.where(hf, np.abs(Z) * np.exp(-1j * 0.001), Z)
        return Z

    def Hpsrr(il, f):
        K = 0.316 * (1 + 1j * f / 8.5e6) / (1 + 1j * f / 2.66e7)
        return K / (1 + loopT(f)) / (1 + 1j * f / 5e9)

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


if __name__ == "__main__":
    part1_regression()
    part2_bad_zhf()
    part3_resistive_tail()
    part4_no_zhf()
    print("validate_capless PASS")
