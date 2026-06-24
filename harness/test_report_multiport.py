"""Tests for harness/report_multiport.py -- the multi-port REPORT data layer the GUI
"Report" tab consumes (per-port GT-vs-model overlays + a copy-pasteable debug report).

Builds a REAL in-situ fit by reusing test_fit_multiport_depth's _sweep_npz synthesizer: a
multi-port npz with a voltage rail PLUS an in-situ current sink (y_<c>/pi_<c>/iv_<c> keys,
3 temps). The current overlay is driven from result['current'] / importmp.current_ports --
exactly the in-situ extraction path -- NOT the air-gap digest registry (which a real run
never writes). No simulator; pure-numpy structural + content assertions.
"""
import pathlib
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_multiport as FMP                      # noqa: E402
import report_multiport as RMP                   # noqa: E402
from test_fit_multiport_depth import _sweep_npz  # reuse the real synthesizer  # noqa: E402


def _fixture(tmp_path, with_temp=True):
    """A multi-port npz (voltage rail + in-situ current sink 'i500n') + manifest + fit."""
    npz, m = _sweep_npz(tmp_path, with_temp=with_temp, name="rptmp")
    res = FMP.fit_multiport(str(npz), m)
    return str(npz), m, res


# =============================================================== port_views structure
def test_port_views_order_and_counts(tmp_path):
    """Voltage rails first, then current sinks; one voltage view per v_out, one current view
    per manifest i_out (the IN-SITU current port, no digest embedding)."""
    npz, m, res = _fixture(tmp_path)
    views = RMP.port_views(res, npz, m)
    kinds = [v["kind"] for v in views]
    if "current" in kinds:
        assert kinds.index("current") > max(i for i, k in enumerate(kinds) if k == "voltage")
    vv = [v for v in views if v["kind"] == "voltage"]
    cv = [v for v in views if v["kind"] == "current"]
    assert [v["name"] for v in vv] == list(m["v_out"])      # one per v_out, in order
    assert [v["name"] for v in cv] == list(m["i_out"])      # one per i_out, in order
    assert cv and cv[0]["name"] == "i500n"                  # the in-situ sink surfaces


def test_voltage_view_arrays_match_and_finite(tmp_path):
    """Each voltage corner: matching-length fz/Zg/Zm (and fp/Hg/Hm, fn/Sg/Sm); Zm/Hm/Sm finite;
    per-supply psrr present; scores carry zrms/psrr/nrms."""
    npz, m, res = _fixture(tmp_path)
    views = RMP.port_views(res, npz, m)
    vv = [v for v in views if v["kind"] == "voltage"]
    assert vv, "no voltage rails"
    for v in vv:
        assert v["loads"] and set(v["corners"]) == set(v["loads"])
        assert v["cout"] > 0 and np.isfinite(v["esr"])
        for il, c in v["corners"].items():
            for ga, ma in (("fz", "Zm"), ("fp", "Hm"), ("fn", "Sm")):
                assert len(c[ga]) == len(c[ma]) and len(c[ga]) > 0
            assert len(c["fz"]) == len(c["Zg"]) == len(c["Zm"])
            assert np.all(np.isfinite(c["Zm"])), "Zm must be finite"
            assert np.all(np.isfinite(c["Hm"])) and np.all(np.isfinite(c["Sm"]))
            assert np.iscomplexobj(c["Zg"]) and np.iscomplexobj(c["Zm"])
            assert set(c["psrr_supplies"]) == set(v["supplies"])
            for s in v["supplies"]:
                assert "Hg" in c["psrr_supplies"][s] and "Hm" in c["psrr_supplies"][s]
            sc = c["scores"]
            assert {"zrms", "psrr", "nrms"} <= set(sc)
            assert set(sc["psrr"]) == set(v["supplies"])
            for s in v["supplies"]:
                rms_db, ph = sc["psrr"][s]
                assert np.isfinite(rms_db) and np.isfinite(ph)
        assert {"zrms", "prms", "nrms"} == set(v["worst"])


def test_voltage_scores_equal_fit_multiport_err(tmp_path):
    """The view's per-corner scores are REUSED from fit_multiport's err (no re-score)."""
    npz, m, res = _fixture(tmp_path)
    views = RMP.port_views(res, npz, m)
    for v in [x for x in views if x["kind"] == "voltage"]:
        errmap = {e["il"]: e for e in res["voltage"][v["name"]]["err"]}
        for il, c in v["corners"].items():
            assert c["scores"]["zrms"] == pytest.approx(errmap[il]["zrms"])
            assert c["scores"]["nrms"] == pytest.approx(errmap[il]["nrms"])


# =============================================================== in-situ current ports
def test_current_view_insitu_panels(tmp_path):
    """The in-situ sink carries iv + y + psrr + idcT panels (3 temps, iv swept), the aligned
    model arrays, per-supply current-PSRR, and a diff_metrics block -- driven by the npz keys,
    NO digest embedding."""
    npz, m, res = _fixture(tmp_path, with_temp=True)
    views = RMP.port_views(res, npz, m)
    cv = [v for v in views if v["kind"] == "current"]
    assert len(cv) == 1
    v = cv[0]
    assert v["pol"] in ("sink", "source")
    # every measured panel present; this fixture ran no coverage.inoise -> no 'noise' panel
    # (the measured-noise case is locked in test_fit_multiport_depth.
    #  test_current_noise_fit_emit_report_when_measured)
    assert set(v["present"]) == {"iv", "y", "psrr", "idcT"}
    assert "noise" not in v["present"]
    # model arrays align to the view's axes (same as ModelerCore.current_compare)
    vw = v["view"]
    assert len(v["models"]["iv"]) == len(vw["iv_v"]) > 0
    assert len(v["models"]["y"]) == len(vw["ac_f"]) > 0
    assert len(v["models"]["psrr"]) == len(vw["psrr_f"]) > 0
    assert len(v["models"]["idcT"]) == len(vw["temps"]) >= 2
    assert np.all(np.isfinite(v["models"]["iv"]))
    # per-supply current-PSRR panels carry GT + model + an rms
    assert "AVDD1P0" in v["psrr_supplies"]
    sp = v["psrr_supplies"]["AVDD1P0"]
    assert len(sp["f"]) == len(sp["Gg"]) == len(sp["Gm"]) and np.isfinite(sp["rms_db"])
    # metrics: noise channel nulled (no in-situ GT), admittance/PSRR finite
    mt = v["metrics"]
    assert {"idc_ua", "ivrms", "yrms", "prms", "sign_ok", "gdd_sign"} <= set(mt)
    assert np.isnan(mt["nrms"]), "current-noise has no in-situ GT -> nrms must be NaN"
    assert np.isfinite(mt["yrms"]) and np.isfinite(mt["prms"])
    assert any("current-noise" in n for n in v["notes"])
    # SIGN: the report's current-PSRR is dI/dVdd = -pi (importmp pi=-2e-7 here -> +2e-7), matching
    # what emit ships (_fit_current_largesignal fits gdd on -PI). gdd must be POSITIVE, not -.
    assert mt["gdd_nS"] > 0, "current-PSRR sign must be dI/dVdd (=-pi), the emitted convention"
    assert sp["Gg"][0].real > 0, "panel GT must use the dI/dVdd (=-pi) convention too"


def test_current_view_single_temp_drops_idcT(tmp_path):
    """One temperature -> no Idc(T) panel (can't fit a temp slope from a single point), but
    iv/y/psrr still present."""
    npz, m, res = _fixture(tmp_path, with_temp=False)
    cv = [v for v in RMP.port_views(res, npz, m) if v["kind"] == "current"]
    assert cv, "expected the in-situ current sink"
    pres = set(cv[0]["present"])
    assert "idcT" not in pres
    assert {"y", "psrr"} <= pres


def test_port_views_no_i_out(tmp_path):
    """A manifest with no i_out -> only voltage views (no current overlay channel)."""
    npz, m, res = _fixture(tmp_path)
    m2 = dict(m, i_out={})
    views = RMP.port_views(res, npz, m2)
    assert all(v["kind"] == "voltage" for v in views)
    assert [v["name"] for v in views] == list(m["v_out"])


# =============================================================== modeling grade
def test_grade_present_and_consistent(tmp_path):
    """Every port carries a grade {level 0/1/2, verdict, badge, reasons}; overall_grade folds
    them to the worst. A current sink with ~0 residuals (+sign OK) must be level-0 USABLE."""
    npz, m, res = _fixture(tmp_path)
    views = RMP.port_views(res, npz, m)
    for v in views:
        g = v["grade"]
        assert g["level"] in (0, 1, 2)
        assert g["badge"] in ("OK", "~", "!!")
        assert g["verdict"] == RMP._VERDICT[g["level"]]
    cv = [v for v in views if v["kind"] == "current"][0]
    assert cv["grade"]["level"] == 0, "a clean current fit (IVrms~0, sign OK) must grade USABLE"
    og = RMP.overall_grade(views)
    assert og["level"] == max(v["grade"]["level"] for v in views)
    assert og["badge"] == RMP._BADGE[og["level"]]


def test_grade_review_on_sign_flip_and_bad_fit(tmp_path):
    """A current-PSRR sign flip -> REVIEW regardless of magnitude; a big voltage RMS -> REVIEW."""
    npz, m, res = _fixture(tmp_path)
    views = RMP.port_views(res, npz, m)
    cv = [v for v in views if v["kind"] == "current"][0]
    cv["metrics"]["sign_ok"] = False
    g = RMP.grade_port(cv)
    assert g["level"] == 2 and "SIGN FLIP" in " ".join(g["reasons"]).upper()
    vv = [v for v in views if v["kind"] == "voltage"][0]
    vv["worst"]["zrms"] = 9.9                                  # well over the marginal bar
    assert RMP.grade_port(vv)["level"] == 2


# =============================================================== debug_report content
def test_debug_report_content(tmp_path):
    """Non-empty str with the header, every port pin, the worst-case rollup, the in-situ
    current notes, and the TO REPRODUCE footer."""
    npz, m, res = _fixture(tmp_path)
    txt = RMP.debug_report(res, npz, m)
    assert isinstance(txt, str) and txt.strip()
    assert "=== PMU MULTI-PORT MODEL DEBUG REPORT ===" in txt
    for v in RMP.port_views(res, npz, m):
        assert v["pin"] in txt, f"port pin {v['pin']} missing from report"
    assert "worst VOLTAGE" in txt and "worst CURRENT" in txt
    assert "TO REPRODUCE" in txt and "report_multiport.debug_report" in txt
    assert "manifest JSON (inlined" in txt                 # the manifest travels with the text
    assert "VOLTAGE RAIL" in txt and "Zrms[dB]" in txt
    assert "CURRENT SINK" in txt
    assert "current-noise: not measured in-situ" in txt   # the honest in-situ note


def test_debug_report_survives_degenerate_fit(tmp_path):
    """A deliberately degenerate voltage corner must NOT crash debug_report."""
    npz, m, res = _fixture(tmp_path)
    rail = next(iter(res["voltage"]))
    fit = res["voltage"][rail]
    bad_il = fit["err"][0]["il"]
    fit["P"][bad_il].update(R_a=0.0, L_a=0.0, R_pl=0.0)
    fit["cout"] = 0.0
    fit["err"][0]["zrms"] = float("nan")
    txt = RMP.debug_report(res, npz, m)              # must NOT raise
    assert "=== PMU MULTI-PORT MODEL DEBUG REPORT ===" in txt
    assert "TO REPRODUCE" in txt


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
