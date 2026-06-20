"""Lock the held-out off-nominal-TEMPERATURE noise gate: score._temp_noise_metrics (R5).

Noise is fit at the nominal temperature, but the emitted model scales EVERY noise section as pure
resistor-thermal kT (white_noise(4kT*$temperature)), while BSIM3 GT flicker is ~T-independent -- so
the model can over-/under-predict off-nominal. This grades the model's noise at GT-collected hot/cold
corners (`noise_temp_<Tlabel>_<il>`), observability-only (never fitted, never in the composite). These
are PURE-LOGIC tests: a stand-in ref + monkeypatched bench.measure_noise.

Run:  python3 -m pytest harness/test_temp_noise_metric.py -q
"""
import numpy as np

import bench
import score


class _Ref:
    def __init__(self, files, d):
        self.files = files
        self._d = d

    def __getitem__(self, k):
        return self._d[k]


def _grid():
    fn = np.logspace(1, 8, 400)
    Sg = 1e-7 / np.sqrt(fn) + 1e-12 + 5e-9 / np.sqrt(1 + ((fn - 2e6) / 3e5) ** 2)
    return fn, Sg


def test_temp_label_roundtrip():
    assert bench.temp_label(125) == "125" and bench.temp_label(-40) == "m40"
    for T in (-40, 0, 27, 125):
        assert bench.temp_from_label(bench.temp_label(T)) == T


def test_returns_none_without_temp_arrays():
    ref = _Ref(["noise_121u", "noise_offgrid_49u"], {})
    assert score._temp_noise_metrics(ref, "lib", "ldo_model", "") is None


def test_perfect_model_scores_near_zero(monkeypatch):
    fn, Sg = _grid()
    ref = _Ref(["noise_temp_m40_121u", "noise_temp_125_121u"],
               {"noise_temp_m40_121u": np.c_[fn, Sg], "noise_temp_125_121u": np.c_[fn, Sg]})
    monkeypatch.setattr(bench, "measure_noise", lambda *a, **k: (fn, Sg.copy()))
    m = score._temp_noise_metrics(ref, "lib", "ldo_model", "")
    assert m is not None and len(m["rows"]) == 2
    assert [r["T"] for r in m["rows"]] == [-40, 125]          # ascending temperature, parsed
    assert all(r["psd_rms"] < 1e-6 for r in m["rows"])


def test_temp_and_load_threaded_to_the_model(monkeypatch):
    """The model is measured AT the held-out temperature and nominal load (key parsed correctly)."""
    fn, Sg = _grid()
    seen = []

    def fake(lib, subckt, il, xparams="", temp=None):
        seen.append((il, temp, xparams))
        return fn, Sg.copy()

    monkeypatch.setattr(bench, "measure_noise", fake)
    ref = _Ref(["noise_temp_125_121u"], {"noise_temp_125_121u": np.c_[fn, Sg]})
    score._temp_noise_metrics(ref, "lib", "ldo_model", "slew_en=0")
    il, temp, xp = seen[0]
    assert il == "121u" and temp == 125 and "iload=121u" in xp


def test_worst_T_selection(monkeypatch):
    """Two temps with unequal error -> worst_T is the larger-error corner; rows ascending in T."""
    fn, Sg = _grid()
    ref = _Ref(["noise_temp_m40_121u", "noise_temp_125_121u"],
               {"noise_temp_m40_121u": np.c_[fn, Sg], "noise_temp_125_121u": np.c_[fn, Sg]})

    def fake(lib, subckt, il, xparams="", temp=None):
        return (fn, Sg * 1.3) if temp == 125 else (fn, Sg.copy())   # hot is the worse fit

    monkeypatch.setattr(bench, "measure_noise", fake)
    m = score._temp_noise_metrics(ref, "lib", "ldo_model", "")
    assert [r["T"] for r in m["rows"]] == [-40, 125]
    assert m["worst_T"] == 125 and m["rows"][1]["psd_rms"] > m["rows"][0]["psd_rms"]


def test_signed_lf_bias_is_reported(monkeypatch):
    """A model that OVER-predicts noise (flicker over-scaling at hot) yields a positive LF bias."""
    fn, Sg = _grid()
    ref = _Ref(["noise_temp_125_121u"], {"noise_temp_125_121u": np.c_[fn, Sg]})
    monkeypatch.setattr(bench, "measure_noise", lambda *a, **k: (fn, Sg * 1.2))   # model 20% high
    m = score._temp_noise_metrics(ref, "lib", "ldo_model", "")
    assert m["rows"][0]["ir_lf"] > 0


def test_held_out_is_not_in_composite_weights():
    assert not any("temp" in k for k in score.W)
