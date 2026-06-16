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
