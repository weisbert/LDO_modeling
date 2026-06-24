"""Air-gap reproduction loop for the CURRENT ports:

  device GT (work_isrc/*.npz)  -> embed in a ref  -> report.py [8] text
       -> paste -> digest_import.parse_current_digest -> fit_isrc

proves: (1) report carries every current port, (2) a pasted report rebuilds a
fit_isrc-ready port whose behavioral params reproduce the on-box fit within the
digest's resampling tolerance (the whole point: copy the report, reproduce locally).
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import current_digest as CD          # noqa: E402
import fit_isrc                      # noqa: E402
import report                        # noqa: E402

WORK = HERE.parent / "work_isrc"
# one sink + one source -> exercises both polarities / both knee directions / PSRR sign
PORTS = {"IBP_POLY_1P8U": "v1_nmos_simple", "IBP_PTAT_SRC": "v4_pmos_simple"}


def _ref_with_currents():
    ref = {"loads": np.array(["121u"]),
           "z_121u": np.c_[np.logspace(1, 8, 40), np.ones(40) * 0.1, np.zeros(40)],
           "p_121u": np.c_[np.logspace(1, 8, 40), np.ones(40) * 1e-3, np.zeros(40)],
           "noise_121u": np.c_[np.logspace(1, 8, 40), np.ones(40) * 1e-8],
           "meta_cout": np.array(np.nan), "meta_esr": np.array(np.nan)}
    for pin, var in PORTS.items():
        src = {k: v for k, v in np.load(WORK / f"{var}.npz", allow_pickle=True).items()}
        CD.embed_port(ref, pin, src)
    return ref


def test_embed_roundtrip_keys():
    ref = _ref_with_currents()
    assert CD.list_iports(ref) == list(PORTS)
    for pin in PORTS:
        v = CD.port_view(ref, pin)
        assert v["iv_v"].ndim == 1 and v["ac_y"].dtype == complex
        assert v["rout"] > 0 and np.isfinite(v["cp"])


def test_report_section_has_every_port():
    ref = _ref_with_currents()
    lines = CD.current_section_lines(ref)
    txt = "\n".join(lines)
    assert "[8] CURRENT PORTS" in txt
    for pin in PORTS:
        assert pin in txt and f"# iport {pin}" in txt
    # sink and source both present with their polarity
    assert "sink" in txt and "source" in txt


def test_section_appears_in_full_report():
    ref = _ref_with_currents()
    # a minimal FitResult stand-in is awkward; assert via build_report's current hook only
    # by checking current_section_lines is non-empty AND that a port-free ref yields []
    assert CD.current_section_lines({"loads": np.array(["121u"])}) == []
    assert CD.current_section_lines(ref)


def test_paste_back_reproduces_fit():
    """The load-bearing test: parse the emitted digest, fit_isrc the rebuilt port, and
    compare to the fit on the ORIGINAL GT. Behavioral params must match within the
    log-resample tolerance."""
    ref = _ref_with_currents()
    txt = "\n".join(CD.current_section_lines(ref))
    rebuilt = CD.parse_current_digest(txt)
    assert set(rebuilt) == set(PORTS)
    for pin in PORTS:
        p_gt = fit_isrc.fit_isrc(CD.port_view(ref, pin))      # on-box fit
        p_rb = fit_isrc.fit_isrc(rebuilt[pin])                # local fit from the paste
        assert p_gt["pol"] == p_rb["pol"]
        # DC bias current: anchored at vc -> essentially exact through the digest
        assert abs(p_rb["idc"] - p_gt["idc"]) <= 0.03 * abs(p_gt["idc"]) + 1e-9
        # PSRR SIGN must survive the gap (the bug-class we hit on emit)
        assert (p_rb["gdd"] >= 0) == (p_gt["gdd"] >= 0)
        # output conductance (rout) within 10% across the resample
        assert abs(p_rb["g0"] - p_gt["g0"]) <= 0.10 * abs(p_gt["g0"]) + 1e-12
        # compliance knee location within 15%
        assert abs(p_rb["vknee"] - p_gt["vknee"]) <= 0.15 * abs(p_gt["vknee"]) + 1e-3
        # PTAT temp ratio within 1%
        assert abs(p_rb["ptat"] - p_gt["ptat"]) <= 0.01 * abs(p_gt["ptat"])


def test_diff_metrics_self_fit_is_clean():
    """fit-then-predict on the same GT: IV/PSRR/noise residuals must be small and the
    PSRR sign self-consistent (the scorecard's tolerances mean what they say)."""
    ref = _ref_with_currents()
    for pin in PORTS:
        v = CD.port_view(ref, pin)
        m = CD.diff_metrics(v, fit_isrc.fit_isrc(v))
        assert m["sign_ok"]
        assert m["ivrms"] < 6.0          # behavioral knee captures the device I-V
        assert m["ptat_rms"] < 2.0


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  [ok] {fn.__name__}")
    print("all current-report tests passed")


# ----------------------------------------------------------- cPSRR observability gate
def _mk_isrc_view(psrr_g_fn, idc=0.5e-6, g0=1.25e-9):
    """A minimal fit_isrc-schema view (clean sink I-V + given supply-coupling pi)."""
    f = np.logspace(1, np.log10(5e8), 60)
    Vo = np.linspace(0.0, 1.8, 19)
    return dict(name="t", pol="sink", vc=1.28, vdd=1.0,
                iv_v=Vo, iv_i=idc * np.tanh(np.clip(Vo / 0.15, 0, None) ** 2),
                ac_f=f, ac_y=g0 + 1j * 2 * np.pi * f * 2e-15, rout=1.0 / g0, cp=2e-15,
                psrr_f=f, psrr_g=psrr_g_fn(f), nz_f=f, nz_in=np.full_like(f, 1e-15),
                temps=np.array([55.0]), idcT=np.array([idc]))


def test_cpsrr_observability_gate_floor_not_graded():
    """A high-Z bias ref: ~0 DC supply coupling + capacitive HF rise -> the flat-gdd model's
    log-RMS explodes, but |gdd|/g0 << 1e-3 so it is UNOBSERVABLE -> not graded/signed, diagnosed
    as a metric artifact (not a defect)."""
    v = _mk_isrc_view(lambda f: 1e-13 + 1j * 2 * np.pi * f * 2e-15)   # ~0 DC, jw*2fF rise
    m = CD.diff_metrics(v, fit_isrc.fit_isrc(v))
    assert m["cpsrr_observable"] is False and m["gdd_g0"] < CD.CPSRR_OBS_GATE
    assert m["sign_ok"] is True                                   # no sign flag at the floor
    assert m["prms"] > 50.0                                       # raw rms IS huge...
    dg = CD._diagnose(m)
    assert any("UNOBSERVABLE" in s for s in dg)                  # ...but flagged as not-a-defect
    assert not any("magnitude off by" in s for s in dg)


def test_cpsrr_observability_gate_real_coupling_still_graded():
    """A genuinely observable supply coupling (|gdd|/g0 >> 1e-3) stays graded + signed."""
    v = _mk_isrc_view(lambda f: np.full(f.shape, 5e-10) + 0j)     # gdd=0.5nS vs g0=1.25nS
    m = CD.diff_metrics(v, fit_isrc.fit_isrc(v))
    assert m["cpsrr_observable"] is True and m["gdd_g0"] >= CD.CPSRR_OBS_GATE


# ----------------------------------------------------------- data-driven I-V knee side
def test_iv_knee_side_detected_from_data():
    """The compliance knee SIDE is detected from the curve, DECOUPLED from pol: a flat-then-
    collapse-at-rail reference (the real WuR shape) is 'hi' (not the legacy low-side), a flat
    reference is 'none', a rise-from-zero NMOS-sink shape is 'lo'. idc + low IVrms in all cases."""
    Vo = np.linspace(0.0, 1.8, 19)
    idc = 0.5e-6

    def _fit(I, vc):                                      # _fit_iv + the vc/pol predict_iv needs
        return {**fit_isrc._fit_iv(Vo, I, vc=vc, pol="sink", rout=8e8), "vc": vc, "pol": "sink"}
    # (a) high-side ceiling: flat ~idc, collapses to 0 near 1.78 (real WuR ref shape)
    hi = idc * (1.0 - 1.0 / (1.0 + np.exp(-(Vo - 1.74) / 0.02)))
    p_hi = _fit(hi, 1.28)
    assert p_hi["knee_side"] == "hi" and 1.6 < p_hi["vhi"] <= 1.8
    assert abs(p_hi["idc"] - idc) / idc < 0.05
    # (b) flat reference within compliance -> NO knee
    p_fl = fit_isrc._fit_iv(np.linspace(0, 0.8, 9), np.full(9, idc), vc=0.667, pol="sink", rout=3e9)
    assert p_fl["knee_side"] == "none"
    # (c) NMOS-sink rise-from-zero -> low-side knee (legacy)
    lo = idc * np.tanh((Vo / 0.1) ** 2)
    p_lo = _fit(lo, 1.0)
    assert p_lo["knee_side"] == "lo"
    # the model reproduces each shape tightly (the whole point: 63% -> <2%)
    for p, I in ((p_hi, hi), (p_lo, lo)):
        m = fit_isrc.predict_iv(p, Vo)
        rms = np.sqrt(np.mean(((m - I) / (np.median(np.sort(I)[-8:]) + 1e-30)) ** 2)) * 100
        assert rms < 3.0, f"{p['knee_side']} knee IVrms {rms:.1f}%"


def test_iv_knee_keepbest_rejects_spurious_and_partial():
    """KEEP-BEST vs no-knee hardening (adversarial-verify findings): a noisy FLAT ref never flaps
    to a spurious knee, and a high-side collapse that does NOT complete in the sweep prefers 'none'
    over a broken fitted-ceiling fit."""
    rng = np.random.default_rng(1)
    Vo = np.linspace(0.0, 0.8, 9)
    for _ in range(20):                                  # flat + 6% noise -> stable 'none' (no flap)
        I = 1.5e-6 * (1 + 0.06 * rng.standard_normal(9))
        p = fit_isrc._fit_iv(Vo, I, vc=0.667, pol="sink", rout=3e9)
        assert p["knee_side"] == "none"
    Vs = np.linspace(0.0, 1.05, 120)                     # ceiling (1.10) ABOVE the sweep top
    Is = 1e-6 * np.tanh(((1.10 - Vs) / 0.05) ** 1.5)     # only PARTIALLY collapses (to ~0.3)
    p = {**fit_isrc._fit_iv(Vs, Is, vc=0.5, pol="sink", rout=8e8), "vc": 0.5, "pol": "sink"}
    m = fit_isrc.predict_iv(p, Vs)
    assert np.sqrt(np.mean(((m - Is) / np.median(np.sort(Is)[-8:])) ** 2)) * 100 < 5.0


def test_emit_bridge_carries_decoupled_knee():
    """current_crow_from_isrc_fit must carry knee_side/vhi so the spectre cross-val VA emits the
    SAME decoupled knee as production: a sink with a HIGH-side ceiling emits the hi gate, not lo."""
    import emit_pmu_model as EPM
    f = np.logspace(1, 8, 40)
    Vo = np.linspace(0.0, 1.8, 19); idc = 0.5e-6
    hi = idc * (1.0 - 1.0 / (1.0 + np.exp(-(Vo - 1.74) / 0.02)))
    view = dict(name="t", pol="sink", vc=1.28, vdd=1.0, iv_v=Vo, iv_i=hi,
                ac_f=f, ac_y=1.25e-9 + 0j, rout=8e8, cp=2e-15,
                psrr_f=f, psrr_g=np.zeros(40, complex), nz_f=f, nz_in=np.full(40, 1e-15),
                temps=np.array([55.0]), idcT=np.array([idc]))
    p = fit_isrc.fit_isrc(view)
    assert p["knee_side"] == "hi"
    crow = EPM.current_crow_from_isrc_fit(p, pin="IB", tnom_c=25.0)
    assert crow["knee_side"] == "hi" and crow["vhi"] > 1.6
    blk = EPM._current_block_largesignal("IB", crow, "AVDD", "VSS")
    assert "IB_vhi - V(IB,VSS)" in blk["body"]            # HIGH-side gate (was silently low-side)
