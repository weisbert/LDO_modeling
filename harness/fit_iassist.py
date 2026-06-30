"""PURE-PYTHON derivation of the per-rail compressive current-assist (iaG, iaV) -- the large-signal
closure of the LTI Zout fit, with NO simulator (no Spectre, no ALPS).

The per-rail LTI Zout is fit to the SMALL-SIGNAL output impedance, so a load-step dip = di*Z is
LINEAR in di; the real loop is class-AB, so the silicon dip is SUB-linear / STIFFENING (z_pll:
linear 163.6 mV/mA vs GT 107/91/81). The assist injects a compressive current
    i_assist(verr) = iaG * tanh( verr*|verr| / iaV^2 ),   verr = vreg - vout
which is ODD with f'(0)=0 EXACT (-> AC/PSRR/noise bit-identical) and SATURATES at iaG (class-AB),
bending the modeled dip sub-linear to match the GT.

WHY pure-Python (the architecture): the dip<->param map is the model's own nonlinear transient, but
that transient is a SMALL, KNOWN linear RLC network (branch-A ladder + the external decap) with one
nonlinear feedback term -- so it is solved here as an ODE (`predict_dip`), NOT re-simulated. This
  * needs NO second simulation: the GT load-step dips come straight from the coverage.transient
    waveforms ALREADY in the extraction npz (the first ALPS/Donau run), and the model dip is the ODE;
  * runs INSIDE the pure-Python fit (fit_multiport) -- no Spectre/ALPS, no firewall break, runs on
    the box / GUI / CI identically.
`predict_dip` is validated <0.6% against Spectre on both WuR rails (baseline + assisted).
"""
import os
import numpy as np

VREF = 0.8                              # nominal regulated setpoint [V] the GT dip is measured from


def _load_npz(npz):
    """np.load a path-like (str OR os.PathLike, e.g. pathlib.Path); pass through an already-loaded
    NpzFile/dict unchanged. A bare `isinstance(npz, str)` SILENTLY treats a pathlib.Path as
    pre-loaded data (it is neither str nor dict) -> the assist falls back to the seed on every
    Path-passing caller (the GUI run path + the emit/fit CLIs). Centralize the check so it can't
    drift between gt_dips_from_npz and derive_iassist."""
    return np.load(npz, allow_pickle=True) if isinstance(npz, (str, os.PathLike)) else npz


# --------------------------------------------------------------- the model's dip (pure-Python ODE)
def predict_dip(Ra, sections, Cext, i_from, i_to, iaG=0.0, iaV=0.3, vreg=VREF,
                t0=1e-6, edge=1e-9, tstop=1.5e-6):
    """The behavioral rail's load-step dip [mV] from the branch-A ladder Zout + external decap Cext,
    with the compressive assist. `sections` = [(L1,R1),(L2,R2),...] the (L||R) ladder o->...->nN,
    then Ra from nN to the vreg reference. Inductor currents + Vout are states; internal ladder nodes
    are solved algebraically each step. iaG=0 -> the bare-LTI baseline. No simulator."""
    from scipy.integrate import solve_ivp
    L = np.array([s[0] for s in sections], float)
    G = np.array([1.0 / s[1] for s in sections], float)
    Gra = 1.0 / Ra
    N = len(sections)

    def algebraic(ivec, Vo):
        # KCL at each ladder node n_{k+1} (k=0..N-1); the last node ties to vreg through Ra.
        A = np.zeros((N, N)); b = np.zeros(N)
        for k in range(N):
            A[k, k] += G[k]
            if k > 0:
                A[k, k - 1] -= G[k]
            else:
                b[k] += G[k] * Vo
            if k < N - 1:
                A[k, k] += G[k + 1]; A[k, k + 1] -= G[k + 1]
                b[k] += ivec[k] - ivec[k + 1]
            else:
                A[k, k] += Gra
                b[k] += ivec[k] + Gra * vreg
        return np.linalg.solve(A, b)

    def iload(t):
        return i_from if t < t0 else i_from + (i_to - i_from) * min(1.0, (t - t0) / edge)

    def iassist(Vo):
        verr = vreg - Vo
        return iaG * np.tanh(verr * abs(verr) / (iaV * iaV)) if iaG > 0 else 0.0

    def f(t, x):
        ivec = x[:N]; Vo = x[N]
        V = algebraic(ivec, Vo)
        Vprev = np.concatenate(([Vo], V[:-1]))
        di = (Vprev - V) / L
        dVo = (iassist(Vo) - iload(t) - ivec[0] - G[0] * (Vo - V[0])) / Cext
        return list(di) + [dVo]

    x0 = list(np.full(N, -i_from)) + [vreg - i_from * Ra]
    # LSODA + a modest max_step (samples the ~ns edge + the ~10-50ns dip without forcing 1ns steps
    # everywhere) -> ~2-4 ms/solve, validated <1.6 mV vs Spectre on both rails.
    sol = solve_ivp(f, (0, t0 + tstop), x0, method="LSODA", max_step=5 * edge, rtol=1e-6, atol=1e-10)
    return (vreg - float(sol.y[N].min())) * 1e3


# --------------------------------------------------------------- GT dips (standard-flow data)
def gt_dips_from_npz(npz, rail, i_from, vref=VREF):
    """{di: dip_V} for `rail` from the coverage.transient GT waveforms in the npz (`tr_<rail>_<lab>m_*`,
    col0=time col1=Vout; dip = vref - min(Vout)), keyed by di = i_to - i_from. `npz` is a path or a
    loaded NpzFile/dict. {} when no tr_ waveforms (single-OP / legacy npz)."""
    d = _load_npz(npz)
    keys = d.files if hasattr(d, "files") else list(d)
    out = {}
    pre = f"tr_{rail}_"
    for k in keys:
        if not k.startswith(pre):
            continue
        lab = k[len(pre):].split("_")[0]
        if not lab.endswith("m"):
            continue
        try:
            i_to = float(lab[:-1]) * 1e-3
        except ValueError:
            continue
        out[round(i_to - i_from, 12)] = vref - float(np.min(np.asarray(d[k])[:, 1]))
    return dict(sorted(out.items()))


# --------------------------------------------------------------- the 2-param fit (pure-Python)
def _grid(lo, hi, n, log=False):
    if log:
        return list(np.exp(np.linspace(np.log(lo), np.log(hi), n)))
    return list(np.linspace(lo, hi, n))


def fit_rail(Ra, sections, Cext, i_from, gt_dips, vreg=VREF,
             iaG_range=(5.0e-4, 1.2e-2), iaV_range=(0.10, 0.50), n=8, refine=True):
    """Solve (iaG, iaV) so the predicted dips match the GT dips. Grid -> local refine, then a
    MINIMAL-INTERVENTION tie-break (the 2 params are partly degenerate -- a valley fits the same
    dips -- so among solutions within MARGIN of the best RMS, take the SMALLEST iaG = the gentlest
    assist). Reports a held-out-across-amplitudes prediction (fit the outer steps, predict the
    middle). Returns {iaG, iaV, _diag} or None when <2 GT dips."""
    targets = sorted(gt_dips.items())                    # [(di, dip_V), ...]
    if len(targets) < 2:
        return None
    DI = [di for di, _ in targets]
    GT = [dip * 1e3 for _, dip in targets]               # mV

    def dips(iaG, iaV):
        return [predict_dip(Ra, sections, Cext, i_from, i_from + di, iaG, iaV, vreg) for di in DI]

    def rms(d, idx):
        return float(np.sqrt(np.mean([(d[i] - GT[i]) ** 2 for i in idx])))

    def pick(scored):
        best = min(s[3] for s in scored)
        margin = max(1.5, 0.10 * best)                   # within 1.5 mV (or 10%) RMS of the best
        return min((s for s in scored if s[3] <= best + margin), key=lambda s: s[0])

    scored = [(g, v, d, rms(d, range(len(GT))))
              for g in _grid(*iaG_range, n, log=True) for v in _grid(*iaV_range, n)
              for d in [dips(g, v)]]
    bg, bv, bd, _ = pick(scored)
    if refine:
        scored += [(g, v, d, rms(d, range(len(GT))))
                   for g in _grid(bg * 0.6, bg * 1.5, 5, log=True)
                   for v in _grid(max(0.05, bv - 0.08), bv + 0.08, 5) for d in [dips(g, v)]]
        bg, bv, bd, _ = pick(scored)
    edge = (abs(bg - iaG_range[0]) / bg < 0.02 or abs(bg - iaG_range[1]) / bg < 0.02
            or abs(bv - iaV_range[0]) < 1e-3 or abs(bv - iaV_range[1]) < 1e-3)
    held = None
    if len(GT) >= 3:
        outer = [0, len(GT) - 1]; mid = len(GT) // 2
        ho = min(scored, key=lambda s: rms(s[2], outer))
        held = {"fit_di": [DI[i] for i in outer], "predict_di": DI[mid],
                "pred_dip": ho[2][mid], "gt_dip": GT[mid],
                "err_pct": 100.0 * (ho[2][mid] - GT[mid]) / GT[mid]}
    return {"iaG": bg, "iaV": bv,
            "_diag": {"di": DI, "gt_dip": GT, "model_dip": bd, "rms_mV": rms(bd, range(len(GT))),
                      "held_out": held, "on_boundary": bool(edge)}}


# --------------------------------------------------------------- top-level (called by fit_multiport)
def _sections_of(P_corner):
    """[(L1,R1),...] branch-A ladder for a fitted per-corner param dict: the first (L_a,R_pl) shelf
    plus every extra higher-order section."""
    secs = [(float(P_corner["L_a"]), float(P_corner["R_pl"]))]
    for e in (P_corner.get("extra") or []):
        secs.append((float(e[0]), float(e[1])))
    return secs


def _seed(vmf):
    """A manifest `iassist` SEED dict {iaG, iaV[, floor, gfloor]} (validated) or None -- the fallback
    when there is no coverage.transient GT to derive from (legacy/single-OP npz)."""
    if not isinstance(vmf, dict):
        return None
    ia = vmf.get("iassist")
    if not isinstance(ia, dict):
        return None
    try:
        g = float(ia.get("iaG", 0.0)); v = float(ia.get("iaV", 0.0))
    except (TypeError, ValueError):
        return None
    if not (0.0 < g < 1e30 and 0.0 < v < 1e30):
        return None
    out = {"iaG": g, "iaV": v}
    out.update(_manifest_floor(vmf))
    return out


def _manifest_floor(vmf):
    """{floor[, gfloor]} from a manifest rail's iassist -- the DEEP backstop knob, INDEPENDENT of
    whether iaG/iaV were derived or seeded. The DERIVED assist carries only iaG/iaV, so this must be
    merged onto it too (else a manifest `floor` only ever reaches the seed/legacy path). Empty dict
    when absent/invalid."""
    if not isinstance(vmf, dict):
        return {}
    ia = vmf.get("iassist")
    if not isinstance(ia, dict):
        return {}
    out = {}
    for k in ("floor", "gfloor"):
        if k in ia:
            try:
                out[k] = float(ia[k])
            except (TypeError, ValueError):
                pass
    return out


def derive_iassist(volt, npz, manifest, *, nom_corner):
    """DERIVE each rail's assist from the coverage.transient GT (pure-Python) and attach it to
    volt[rail]['iassist']; fall back to the manifest seed when no GT dips are present. Mutates `volt`.
    Returns the {rail: 'fit'|'seed'|'none'} source map. `nom_corner` = fit_multiport._nom_corner.
    Any per-rail error is swallowed to the seed (a fit miss must never break the model emit)."""
    cov = (manifest.get("coverage") or {})
    tr = cov.get("transient") or {}
    Cext = float(cov.get("cdecap", 20e-12))
    try:
        data = _load_npz(npz)
    except Exception:
        data = None
    src = {}
    floor_default = None                                 # backstop OFF by default
    for o in volt:
        seed = _seed((manifest.get("v_out", {}) or {}).get(o) or {})
        derived = None
        try:
            steps = ((tr.get(o) or {}).get("steps")) or []
            if data is not None and steps:
                i_from = float(steps[0]["from"])
                gt = gt_dips_from_npz(data, o, i_from)
                if len(gt) >= 2:
                    P = volt[o]["P"][nom_corner(volt[o]["P"])]
                    r = fit_rail(float(P["R_a"]), _sections_of(P), Cext, i_from, gt,
                                 vreg=float(P.get("vreg", VREF)))
                    if r is not None:
                        derived = {"iaG": r["iaG"], "iaV": r["iaV"]}
                        volt[o]["iassist_diag"] = r["_diag"]
        except Exception as e:                           # noqa: BLE001
            print(f"[fit] iassist rail {o}: derive SKIP ({e}) -> seed")
            derived = None
        ia = derived or seed
        if ia is None:
            src[o] = "none"; continue
        # the deep backstop floor is a manifest knob independent of iaG/iaV derivation; the
        # DERIVED dict carries only iaG/iaV, so merge floor/gfloor from the manifest onto it
        # (the seed path already includes them; this is idempotent there).
        ia.update(_manifest_floor((manifest.get("v_out", {}) or {}).get(o) or {}))
        if floor_default is not None:
            ia.setdefault("floor", floor_default)
        volt[o]["iassist"] = ia
        src[o] = "fit" if derived else "seed"
    return src
