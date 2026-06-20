"""Lock the held-out off-corner-LOAD noise gate: score._offgrid_noise_metrics (R4).

The model is fit + graded only at the 3 load corners, but its emitted noise interpolates section
amplitudes quadratic-in-ln(iload) over FROZEN poles -- so the spectrum at an intermediate load (the
most-exercised axis: PMU load lines sweep current continuously) is an untested 3-point extrapolant.
This metric grades the model's interpolated noise at GT-collected off-corner loads
(`noise_offgrid_<il>`) vs GT, as an OBSERVABILITY-only held-out gate (never fitted, never in the
composite). These are PURE-LOGIC tests: a stand-in ref + monkeypatched bench.measure_noise.

Run:  python3 -m pytest harness/test_offgrid_noise_metric.py -q
"""
import numpy as np

import bench
import score


class _Ref:
    """Minimal stand-in for the np.load ref object (.files + __getitem__)."""
    def __init__(self, files, d):
        self.files = files
        self._d = d

    def __getitem__(self, k):
        return self._d[k]


def _grid():
    fn = np.logspace(1, 8, 400)
    Sg = 1e-7 / np.sqrt(fn) + 1e-12 + 5e-9 / np.sqrt(1 + ((fn - 2e6) / 3e5) ** 2)
    return fn, Sg


def test_returns_none_without_offgrid_arrays():
    """Legacy refs (no noise_offgrid_*) -> no gate, no crash."""
    ref = _Ref(["noise_20u", "z_20u"], {})
    assert score._offgrid_noise_metrics(ref, "lib", "ldo_model", "") is None


def test_perfect_interpolation_scores_near_zero(monkeypatch):
    """Model noise == GT at the off-corner loads -> psd_rms ~ 0 on every held-out load."""
    fn, Sg = _grid()
    ref = _Ref(["noise_offgrid_49u", "noise_offgrid_174u"],
               {"noise_offgrid_49u": np.c_[fn, Sg], "noise_offgrid_174u": np.c_[fn, Sg]})
    monkeypatch.setattr(bench, "measure_noise", lambda *a, **k: (fn, Sg.copy()))
    m = score._offgrid_noise_metrics(ref, "lib", "ldo_model", "")
    assert m is not None and len(m["rows"]) == 2
    assert all(r["psd_rms"] < 1e-6 for r in m["rows"])
    assert m["worst_db"] < 1e-6


def test_offcorner_interpolation_error_is_surfaced(monkeypatch):
    """A real off-corner miss (model 20% high in the LF energy band) must register in worst_db."""
    fn, Sg = _grid()
    ref = _Ref(["noise_offgrid_49u"], {"noise_offgrid_49u": np.c_[fn, Sg]})
    monkeypatch.setattr(bench, "measure_noise", lambda *a, **k: (fn, Sg * 1.2))
    m = score._offgrid_noise_metrics(ref, "lib", "ldo_model", "")
    assert m["worst_db"] > 1.0
    assert m["worst_il"] == "49u"


def test_worst_il_selection_and_ascending_order(monkeypatch):
    """Two off-corner loads with UNEQUAL error: worst_il must be the larger-error load, and the rows
    must come out in ASCENDING current (49u before 174u) -- NOT lexical (which sorts 174 before 49)."""
    fn, Sg = _grid()
    ref = _Ref(["noise_offgrid_49u", "noise_offgrid_174u"],
               {"noise_offgrid_49u": np.c_[fn, Sg], "noise_offgrid_174u": np.c_[fn, Sg]})

    def fake(lib, subckt, il, xparams=""):
        return (fn, Sg.copy()) if il == "49u" else (fn, Sg * 1.5)   # 174u is the worse fit

    monkeypatch.setattr(bench, "measure_noise", fake)
    m = score._offgrid_noise_metrics(ref, "lib", "ldo_model", "")
    assert [r["il"] for r in m["rows"]] == ["49u", "174u"]          # ascending current, not lexical
    assert m["rows"][0]["psd_rms"] < m["rows"][1]["psd_rms"]
    assert m["worst_il"] == "174u"


def test_held_out_is_not_in_composite_weights():
    """R4 is observability-only -- there is deliberately NO composite weight for off-corner noise."""
    assert not any("offgrid" in k for k in score.W)


def test_iload_param_is_threaded_to_the_model(monkeypatch):
    """The model is measured AT the off-corner current (its `iload` param drives the interpolation)."""
    fn, Sg = _grid()
    seen = {}

    def fake(lib, subckt, il, xparams=""):
        seen["il"], seen["xp"] = il, xparams
        return fn, Sg.copy()

    monkeypatch.setattr(bench, "measure_noise", fake)
    ref = _Ref(["noise_offgrid_49u"], {"noise_offgrid_49u": np.c_[fn, Sg]})
    score._offgrid_noise_metrics(ref, "lib", "ldo_model", "slew_en=0")
    assert seen["il"] == "49u"
    assert "iload=49u" in seen["xp"]
