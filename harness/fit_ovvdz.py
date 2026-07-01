"""PURE-PYTHON derivation of the per-rail unload-discharge TRANSPARENCY FLOOR (ovVdz) -- the large-
signal-transparency closure of the shipped Route-1 branch-A discharge, with NO simulator (no Spectre,
no ALPS). Mirrors `fit_iassist.derive_iassist`: it runs INSIDE `fit_multiport`, after the LTI fit,
consuming quantities ALREADY in the extraction npz / manifest.

WHAT ovVdz IS (see METHODOLOGY §Large-signal Route 1 + §Refuted "small ovVdz"): the unload-discharge
dump engages for ANY output excursion reaching >vreg+ovVdz while branch A SOURCES -- which includes not
just the one-time unload transient but ALSO the positive half of a large-signal PERIODIC load/PSRR/spur
ripple. Since the model's PRIMARY product is the spur/PSRR/Zout SPECTRA, ovVdz MUST sit ABOVE the
largest legitimate periodic rail excursion, or the dump clips those positive peaks and DISTORTS the
spectra (local-Spectre: ovVdz=3 mV -> 16 mV clip-distortion on a 20 mV-above ripple). It is a
TRANSPARENCY FLOOR, not a DC-droop margin.

The GATE MECHANISM is settled (the shipped INSTANTANEOUS gate is Pareto-optimal -- a heavy/state gate
was refuted; METHODOLOGY §Refuted). This module ONLY auto-fits the FLOOR VALUE, replacing the hardcoded
chip-specific `emit_pmu_model._OVD_VDZ` (25 mV) with a per-rail value derived from THIS rail's own
characterized large-signal excursion:

    excursion = max_f |Zout(f)| * di_ripple            # the load-ripple term (always present)
              [ + sum_k vout_amp_k ]                    # opt-in: characterized spur tones (each IS an
                                                        #         above-vreg tone) -- 0 if no spur fit
              [ + max_f |H_psrr(f)| * vrip ]            # opt-in: supply-ripple spec -> output ripple
    ovVdz     = clamp( K * excursion,  [VDZ_MIN,  ALPHA*(supply - vreg)] )

ALL inputs are LOCAL (npz `z_<rail>`/`p_<rail>` + manifest coverage) -- this is a FIT change, NOT a
coverage change. The hardcoded 25 mV was exactly this done by hand: `2.5 * (50 uA coverage-lin di *
~197 Ohm Zout plateau) = 2.5 * ~10 mV`. This mechanizes that hand calc -- and reproduces it: PLL
~24.6 mV (197.1 Ohm * 50 uA * 2.5), VCO ~26.1 mV (52.1 Ohm * 200 uA * 2.5).

The ONE unknowable (a customer-deployment ripple beyond the characterized envelope) is handled by the
margin K and the per-rail `vfit['unload_discharge']` manual knob -- never by gathering data.

Opt-in / fallback discipline (mirrors the iassist seed): a rail with NO characterized Zout (legacy /
single-OP npz), or with the feature disabled, or with a MANUAL ovVdz already set, is left untouched ->
`emit_pmu_model._OVD_VDZ` (25 mV) remains the fallback and a manual value stays authoritative.
"""
import numpy as np

# ----------------------------------------------------------------------- derivation constants
# di_ripple = RIPPLE_FRAC * I_op: the largest LEGITIMATE periodic load ripple, as a fraction of the
# rail's operating load. 0.10 reproduces the documented WuR char (PLL 50 uA @ 500 uA OP, VCO 200 uA @
# 2 mA OP = 10% each; DATA §6). This is deliberately the PERIODIC-ripple di (coverage LIN regime), NOT
# the one-time BIG load step (500 uA/1 mA -- the transient the compressive assist owns; *plateau would
# blow the ~160 mV supply headroom, out of the small-signal envelope). Overridable per-rail via
# vfit['unload_discharge']['ripple_di'] (an absolute [A]) or ['ripple_frac'].
RIPPLE_FRAC = 0.10
K           = 2.5      # transparency MARGIN: floor = K x the characterized excursion (headroom for a
                       # deployment ripple slightly beyond the char + fit error in |Zout|).
ALPHA       = 0.5      # upper clamp = ALPHA x (supply - vreg): the floor is also where the unload
                       # overshoot settles, so cap it well under the headroom (else a large residual).
VDZ_MIN     = 5.0e-3   # lower floor [V]: a few mV, safely ABOVE the DC load-droop (R_a*Iload ~0.4 mV)
                       # and the worst abuse-sink droop (40 mA*R_a ~3.8 mV) so a sustained sink is inert.


# ----------------------------------------------------------------------- local-data readers
def zout_mag_max(view):
    """max over freq and over every characterized load/corner of |Zout(f)| [Ohm] from the raw npz
    `z_<label>` arrays in a split_ports voltage view. This is the CHARACTERIZED plateau of the rising
    resistive shelf (the model tracks it to <=0.34 dB -- DATA §5 -- and K=2.5 covers that gap). None
    when the view carries no Zout (legacy / single-OP npz) -> the caller falls back to _OVD_VDZ."""
    if not view:
        return None
    sp = view.get("npz") or {}
    zmax = None
    for il in view.get("loads", []):
        arr = sp.get(f"z_{il}")
        if arr is None:
            continue
        a = np.asarray(arr)
        if a.ndim != 2 or a.shape[1] < 3 or a.shape[0] == 0:
            continue
        mag = np.abs(a[:, 1] + 1j * a[:, 2])
        m = float(np.max(mag)) if mag.size else None
        if m is not None and np.isfinite(m):
            zmax = m if zmax is None else max(zmax, m)
    return zmax


def psrr_mag_max(view):
    """max over freq/load/supply of |H_psrr(f)| [V/V] from the raw npz `p_<label>` (primary supply) +
    the full per-supply set. Dimensionless supply->output gain. None when no PSRR characterized."""
    if not view:
        return None
    hmax = None

    def _upd(arr):
        nonlocal hmax
        a = np.asarray(arr)
        if a.ndim != 2 or a.shape[1] < 3 or a.shape[0] == 0:
            return
        mag = np.abs(a[:, 1] + 1j * a[:, 2])
        if mag.size:
            m = float(np.max(mag))
            if np.isfinite(m):
                hmax = m if hmax is None else max(hmax, m)

    sp = view.get("npz") or {}
    for il in view.get("loads", []):
        if f"p_{il}" in sp:
            _upd(sp[f"p_{il}"])
    for _s, byil in (view.get("supplies") or {}).items():
        for _il, arr in (byil or {}).items():
            _upd(arr)
    return hmax


def _op_current(manifest, o, vo, nom):
    """The rail's operating load I_op [A] the periodic ripple rides on. Priority: the coverage.transient
    step base `from` (the DC operating load during the transient char) -> the nominal fitted-corner load
    `iv` -> the median coverage.loads point. None when nothing local declares a load (-> no ripple term
    -> _OVD_VDZ fallback)."""
    steps = ((((manifest.get("coverage") or {}).get("transient") or {}).get(o) or {}).get("steps") or [])
    for st in steps:
        try:
            v = float(st["from"])
        except (KeyError, TypeError, ValueError):
            continue
        if v > 0:
            return v
    try:
        iv = float(vo["P"][nom].get("iv", 0.0))
        if iv > 0:
            return iv
    except (KeyError, TypeError, ValueError):
        pass
    pts = (((manifest.get("coverage") or {}).get("loads") or {}).get(o) or {}).get("points") or []
    pts = sorted(float(p) for p in pts if _finite_pos(p))
    return pts[len(pts) // 2] if pts else None


def _ripple_di(manifest, o, vo, nom, over):
    """The periodic ripple current di [A]. An explicit override wins: ['ripple_di'] (absolute [A]) then
    ['ripple_frac'] * I_op; else RIPPLE_FRAC * I_op. None when no I_op is derivable."""
    rd = _as_pos_float(over.get("ripple_di"))
    if rd is not None:
        return rd
    iop = _op_current(manifest, o, vo, nom)
    if iop is None:
        return None
    frac = _as_pos_float(over.get("ripple_frac")) or RIPPLE_FRAC
    return frac * iop


def _spur_sum(vo, vmf):
    """sum_k vout_amp_k [V] over characterized above-vreg spur tones -- each is literally a pre-measured
    output tone that must clear the transparency floor. Reads a `spurs` list from the fit result or the
    manifest rail; sums 'vout_amp'/'amp'. 0.0 when no spur characterization (the WuR rails)."""
    total = 0.0
    for src in (vo.get("spurs"), (vmf or {}).get("spurs")):
        if not isinstance(src, (list, tuple)):
            continue
        for sp in src:
            if isinstance(sp, dict):
                a = _as_pos_float(sp.get("vout_amp", sp.get("amp")))
            else:
                a = _as_pos_float(sp)
            if a is not None:
                total += a
        if total > 0:
            break
    return total


def _psrr_ripple(view, manifest, vmf, over):
    """max_f |H_psrr(f)| * vrip [V] -- the output ripple a characterized supply-ripple spec produces.
    Opt-in: vrip [V] from ['vrip'] on the unload_discharge override, the manifest rail, or
    coverage.supply_ripple. 0.0 when no supply-ripple spec is given (the default -- WuR has none)."""
    vrip = (_as_pos_float(over.get("vrip"))
            or _as_pos_float((vmf or {}).get("vrip"))
            or _as_pos_float(((manifest.get("coverage") or {}).get("supply_ripple"))))
    if not vrip:
        return 0.0
    hmax = psrr_mag_max(view)
    return hmax * vrip if hmax else 0.0


# ----------------------------------------------------------------------- small numeric helpers
def _finite_pos(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return 0.0 < v < 1e30


def _as_pos_float(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if 0.0 < v < 1e30 else None


# ----------------------------------------------------------------------- top-level (called by fit_multiport)
def derive_ovvdz(volt, views, manifest, *, supplies, nom_corner):
    """DERIVE each rail's unload-discharge transparency floor and write it to
    volt[rail]['unload_discharge']['ovVdz'] (the override channel emit_pmu_model._ovd_vals already
    reads). Mutates `volt`. Returns {rail: 'fit'|'manual'|'off'|'default'} where:
      - 'fit'     : derived from the characterized |Zout| * di_ripple (+ opt-in spur/PSRR)
      - 'manual'  : a per-rail ovVdz was already set (user/manifest) -> kept (highest priority)
      - 'off'     : the feature is disabled (unload_discharge False/0) -> left disabled
      - 'default' : no characterized Zout (legacy/single-OP) -> NOT written -> emit uses _OVD_VDZ (25 mV)

    `supplies` = the manifest supply list (for the primary supply DC -> headroom cap). `nom_corner` =
    the same nominal-corner picker fit_multiport hands derive_iassist. Any per-rail error is swallowed
    to 'default' -- a floor-derivation miss must NEVER break the model emit (mirrors derive_iassist)."""
    src = {}
    for o in volt:
        vo = volt[o]
        vmf = (manifest.get("v_out", {}) or {}).get(o) or {}
        # the unload_discharge config: an explicit value already on the fit result wins, else thread the
        # manifest knob onto the fit (mirrors slew_a/recovery); default-ON when neither is set.
        ud = vo.get("unload_discharge")
        if ud is None:
            ud = vmf.get("unload_discharge", True)
        if ud is False or ud == 0:                         # feature disabled -> plain stiff resistor
            vo["unload_discharge"] = False
            src[o] = "off"
            continue
        over = dict(ud) if isinstance(ud, dict) else {}
        if _as_pos_float(over.get("ovVdz")) is not None:    # a manual ovVdz is authoritative
            vo["unload_discharge"] = over
            src[o] = "manual"
            continue
        try:
            view = (views or {}).get(o)
            zmax = zout_mag_max(view)
            nom = nom_corner(vo["P"])
            di = _ripple_di(manifest, o, vo, nom, over)
            if zmax is None or di is None:                  # no local Zout/load -> _OVD_VDZ fallback
                src[o] = "default"
                continue
            exc_z = zmax * di
            exc_spur = _spur_sum(vo, vmf)
            exc_psrr = _psrr_ripple(view, manifest, vmf, over)
            exc = exc_z + exc_spur + exc_psrr
            ovvdz = K * exc
            # clamp to [VDZ_MIN, ALPHA*(supply - vreg)]. The headroom cap needs the primary-supply DC
            # and the rail's regulated setpoint; drop the cap (upper=inf) if either is unavailable.
            lo, hi = VDZ_MIN, float("inf")
            prim = (view or {}).get("primary_supply") or (supplies[0] if supplies else None)
            sdc = _as_pos_float(((manifest.get("supplies") or {}).get(prim) or {}).get("dc"))
            vreg = None
            try:
                vreg = float(vo["P"][nom].get("vreg"))
            except (KeyError, TypeError, ValueError):
                vreg = None
            if sdc is not None and vreg is not None and sdc > vreg:
                hi = ALPHA * (sdc - vreg)
            capped = "none"
            if ovvdz < lo:                                   # raise to the DC-droop floor first
                ovvdz, capped = lo, "floor"
            if ovvdz > hi:                                   # ... then the headroom ceiling WINS (physical
                ovvdz, capped = hi, "headroom"               #     cap; matters only if headroom < VDZ_MIN)
            over["ovVdz"] = ovvdz
            vo["unload_discharge"] = over
            vo["ovvdz_diag"] = dict(zmax_ohm=zmax, di_A=di, exc_z_V=exc_z, exc_spur_V=exc_spur,
                                    exc_psrr_V=exc_psrr, exc_V=exc, K=K, ovVdz_V=ovvdz,
                                    lo_V=lo, hi_V=(None if hi == float("inf") else hi), capped=capped)
            src[o] = "fit"
        except Exception as e:                              # noqa: BLE001 -- never break emit
            print(f"[fit] ovVdz rail {o}: derive SKIP ({e}) -> _OVD_VDZ default")
            src[o] = "default"
    return src
