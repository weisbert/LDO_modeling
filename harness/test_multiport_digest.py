"""Air-gap GT digest for the multi-port debug report: the report must be SELF-CONTAINED --
pasting it (no npz attachment) must rebuild an npz-equivalent ref and let fit_multiport
reproduce the same report locally. This is the multi-port twin of report.py's [7] / current
_digest's [8d], so the red-zone modeler never needs the (air-gapped) npz.

Reuses test_fit_multiport_depth._sweep_npz (voltage rail + in-situ current sink, 3 temps).
No simulator; pure-numpy round-trip.
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_multiport as FMP                      # noqa: E402
import report_multiport as RMP                   # noqa: E402
from test_fit_multiport_depth import _sweep_npz  # reuse the real synthesizer  # noqa: E402


def _build(tmp_path, with_temp=True):
    npz, m = _sweep_npz(tmp_path, with_temp=with_temp, name="mpd")
    m["_path"] = str(tmp_path / "mpd_manifest.json")       # exercise the origin-manifest line
    res = FMP.fit_multiport(str(npz), m)
    txt = RMP.debug_report(res, str(npz), m)
    return str(npz), m, res, txt


# ----------------------------------------------------------------- footer is self-contained
def test_footer_is_self_contained(tmp_path):
    _npz, _m, _res, txt = _build(tmp_path)
    assert RMP._MPD_BEGIN in txt and RMP._MPD_END in txt   # the GT digest travels in the text
    assert "NO attachment needed" in txt
    assert "attach the npz" not in txt.lower()             # the old footer is gone
    # the existing report contract (test_report_multiport) still holds
    assert "TO REPRODUCE" in txt and "report_multiport.debug_report" in txt
    assert "manifest JSON (inlined" in txt


# ----------------------------------------------------------------- digest carries every key
def test_digest_rebuilds_all_fit_keys(tmp_path):
    npz, m, _res, txt = _build(tmp_path)
    from insitu import importmp as IM
    raw = IM.load_multiport(npz)
    reb = RMP.parse_multiport_digest(txt)
    # every key fit_multiport reads (z_/p_/noise_/y_/pi_/iv_) must round-trip, same shape cols
    kinds = ("z_", "p_", "noise_", "y_", "pi_", "iv_")
    want = {k for k in raw if str(k).startswith(kinds)}
    assert want, "fixture carried no fit keys"
    for k in want:
        assert k in reb, f"digest lost key {k}"
        assert reb[k].shape[1] == np.asarray(raw[k]).shape[1], f"col mismatch on {k}"
    # loads + meta round-trip
    assert list(reb["loads"]) == [str(x) for x in raw["loads"]]
    assert "meta_temp" in reb and "meta_iload_pll" in reb
    assert np.allclose(np.asarray(reb["meta_iload_pll"], float),
                       np.asarray(raw["meta_iload_pll"], float), equal_nan=True)


def test_digest_round_trips_current_noise():
    """@noise_i carries the current-output noise GT (A/rtHz) so a pasted report can rebuild +
    RETUNE the in_white/in_kf fit -- closing the reproducibility gap when coverage.inoise ran (the
    digest previously emitted @y/@pi/@iv for sinks but NOT their noise, so noise_i was unfixable
    from a paste)."""
    f = np.logspace(1, 8, 60)
    In = np.sqrt(1e-12 ** 2 + 1e-22 / f)                       # white + flicker
    ref = {"loads": np.array(["tt_25c"]),
           "noise_i_i500n_lpf_tt_25c": np.c_[f, In]}
    m = {"name": "t", "v_out": {}, "supplies": {},
         "i_out": {"i500n_lpf": {"net": "N", "pin": "P"}}, "current_psrr_supplies": []}
    txt = "\n".join(RMP.emit_multiport_digest(ref, m))
    assert "@noise_i i500n_lpf tt_25c" in txt, "current-noise GT not emitted to the digest"
    reb = RMP.parse_multiport_digest(txt)
    k = "noise_i_i500n_lpf_tt_25c"
    assert k in reb, "current-noise key did not round-trip through the digest"
    assert reb[k].shape[1] == 2 and reb[k].shape[0] >= 10      # [f, In], log-resampled
    assert abs(reb[k][-1, 1] / In[-1] - 1.0) < 0.3            # HF white floor preserved


# ----------------------------------------------------------------- manifest round-trips
def test_parse_manifest_round_trips(tmp_path):
    _npz, m, _res, txt = _build(tmp_path)
    m2 = RMP.parse_manifest(txt)
    assert m2["name"] == m["name"]
    assert set(m2["v_out"]) == set(m["v_out"])
    assert set(m2["i_out"]) == set(m["i_out"])
    assert list(m2["current_psrr_supplies"]) == list(m["current_psrr_supplies"])
    assert "_path" not in m2                               # private keys are stripped


# ----------------------------------------------------------------- the full reproduce loop
def test_paste_reproduces_report(tmp_path):
    """emit -> parse -> fit_multiport -> debug_report, from the TEXT alone, reproduces the
    same grades and worst-case scores (within log-resample tolerance)."""
    _npz, _m, res, txt = _build(tmp_path)

    ref_path = RMP.digest_to_npz(txt, str(tmp_path / "repro.npz"))
    m2 = RMP.parse_manifest(txt)
    res2 = FMP.fit_multiport(ref_path, m2)
    txt2 = RMP.debug_report(res2, ref_path, m2)            # must not raise; must be reproducible

    v0 = RMP.port_views(res, _npz, _m)
    v2 = RMP.port_views(res2, ref_path, m2)
    g0 = {v["pin"]: v["grade"]["badge"] for v in v0}
    g2 = {v["pin"]: v["grade"]["badge"] for v in v2}
    assert g0 == g2, f"grades changed across the air gap: {g0} vs {g2}"

    # worst-case voltage scores reproduce within a tolerance (log-resampling is lossy but the
    # fit shape is preserved; the synthetic fixture is smooth so this is tight)
    def _worst_v(views):
        vv = [v for v in views if v["kind"] == "voltage"]
        return (max((v["worst"]["zrms"] for v in vv), default=0.0),
                max((v["worst"]["prms"] for v in vv), default=0.0),
                max((v["worst"]["nrms"] for v in vv), default=0.0))
    z0, p0, n0 = _worst_v(v0)
    z2, p2, n2 = _worst_v(v2)
    assert abs(z0 - z2) < 1.5, f"Zrms drift {z0:.3f} -> {z2:.3f} dB"
    assert abs(p0 - p2) < 1.5, f"PSRR drift {p0:.3f} -> {p2:.3f} dB"
    assert abs(n0 - n2) < 1.5, f"noise drift {n0:.3f} -> {n2:.3f} dB"

    # current I-V is kept whole in the digest -> ivrms reproduces tightly
    def _civ(views):
        cv = [v for v in views if v["kind"] == "current"]
        return {v["pin"]: v["metrics"].get("ivrms", np.nan) for v in cv}
    c0, c2 = _civ(v0), _civ(v2)
    for pin in c0:
        a, b = c0[pin], c2[pin]
        if np.isfinite(a) and np.isfinite(b):
            assert abs(a - b) < max(2.0, 0.20 * abs(a)), f"{pin} IVrms {a:.2f} -> {b:.2f} %"

    # second-generation report is ITSELF self-contained (digest survives a round trip)
    assert RMP._MPD_BEGIN in txt2 and "NO attachment needed" in txt2


# ----------------------------------------------------------------- high-Q resonance survives
def test_zout_resonance_survives_digest():
    """The real pll/vco rails are high-Q Zout peaks (~10 MHz) -- the one feature the Zout fit
    is judged on. A naive log-grid would step over it; the digest must carry the peak verbatim.
    (The _sweep_npz fixture is peak-free, so this guards the load-bearing path directly.)"""
    for Q in (3, 8):
        f = np.logspace(1, np.log10(5e8), 155)            # like the real ac dec=20 sweep
        w = 2 * np.pi * f
        w0 = 2 * np.pi * 10e6
        Z = (1j * w * 0.02) / (1 - (w / w0) ** 2 + 1j * w / (Q * w0)) + 0.5
        fr, Zr = RMP._z_resample(f, Z)
        near = np.sum((fr > f[np.argmax(np.abs(Z))] / 2.5) & (fr < f[np.argmax(np.abs(Z))] * 2.5))
        loss = abs(20 * np.log10(np.abs(Zr).max() / np.abs(Z).max()))
        assert near >= 10, f"Q={Q}: peak under-sampled ({near} pts)"
        assert loss < 0.6, f"Q={Q}: peak magnitude lost ({loss:.2f} dB)"


def test_voltage_model_carried_for_faithful_verify(tmp_path):
    """A near-capless / messy silicon rail has an ill-conditioned Zout fit that a REFIT of the
    lossy digest does NOT reproduce (envelope-cap fallback -> wrong pole). The digest therefore
    CARRIES the box's voltage MODEL (@zmodel/@psrrmodel/@noisemodel) so voltage_views_from_digest
    reproduces the report's scores WITHOUT refitting -- and demonstrably closer than the refit."""
    npz, m = _sweep_npz(tmp_path, with_temp=False, name="vmcarry")
    ref = dict(np.load(npz, allow_pickle=True))
    f = np.logspace(1, np.log10(5e8), 160)
    w = 2 * np.pi * f
    Z = (1j * w * 0.02) / (1 - (w / (2 * np.pi * 10e6)) ** 2 + 1j * w / (9 * 2 * np.pi * 10e6)) + 0.1
    Z = Z * (1 + 0.04 * np.sin(np.log(f) * 7))                  # measurement-like ripple
    for k in [k for k in ref if k.startswith("z_pll_")]:
        ref[k] = np.c_[f, Z.real, Z.imag]
    p = tmp_path / "vmcarry.npz"
    np.savez(p, **ref)
    res = FMP.fit_multiport(str(p), m)
    il = list({e["il"] for e in res["voltage"]["pll"]["err"]})[0]
    box = next(v for v in RMP.port_views(res, str(p), m) if v["name"] == "pll")
    box_zrms = box["corners"][il]["scores"]["zrms"]

    txt = RMP.debug_report(res, str(p), m)
    assert "@zmodel pll" in txt and "@psrrmodel pll" in txt and "@noisemodel pll" in txt

    # carried model reproduces the box score (no refit); old digests carry none -> empty
    vv = RMP.voltage_views_from_digest(txt, m)
    assert "pll" in vv and il in vv["pll"]
    carr = vv["pll"][il]["scores"]["zrms"]
    assert abs(carr - box_zrms) < 0.4, f"carried-model zrms {carr} != box {box_zrms}"
    assert np.all(np.isfinite(np.abs(vv["pll"][il]["Zm"])))

    # and it is CLOSER to the box than refitting the lossy digest would be
    RMP.digest_to_npz(txt, str(tmp_path / "rebuilt.npz"))
    res2 = FMP.fit_multiport(str(tmp_path / "rebuilt.npz"), m)
    refit = next(v for v in RMP.port_views(res2, str(tmp_path / "rebuilt.npz"), m)
                 if v["name"] == "pll")["corners"][il]["scores"]["zrms"]
    assert abs(carr - box_zrms) <= abs(refit - box_zrms), "carried model must beat the refit"


def test_voltage_views_empty_without_carried_model(tmp_path):
    """A digest with NO carried model (emit called without views, e.g. a pre-fix report) -> the
    faithful-verify accessor returns {} rather than guessing. Backward-compatible."""
    npz, m = _sweep_npz(tmp_path, with_temp=False, name="nomodel")
    digest = "\n".join(RMP.emit_multiport_digest(np.load(npz, allow_pickle=True), m))  # views=None
    assert "@zmodel" not in digest
    assert RMP.voltage_views_from_digest(digest, m) == {}


# ----------------------------------------------------------------- degenerate / empty inputs
def test_parse_ignores_non_digest_text():
    assert RMP.parse_multiport_digest("hello\nno digest here\n") == {}
    import pytest
    with pytest.raises(ValueError):
        RMP.digest_to_npz("nothing here", "/tmp/never.npz")
