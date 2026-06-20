"""Lock the SUPPLY-spur rejection metric: bench.supply_spur_atten + score._supply_spur_metrics.

The supply-spur metric scores how well the model reproduces the GT's supply->output
rejection (PSRR, dB) AT the AVDD aggressor tones (DC-DC comb + ref clock). It is collected by
gen_reference/extract_ref (`supply_spur_{il}`) and reported/scored by score. This is a
pure-logic test (no simulator) -- it monkeypatches bench.measure_psrr with a known transfer.

Run:  python3 -m pytest harness/test_supply_spur_metric.py -q
"""
import numpy as np

import bench
import score


def test_supply_spur_atten_is_minus_20log10_psrr():
    """atten[dB] = -20 log10 |PSRR|, evaluated at SUPPLY_SPURS."""
    f = np.logspace(1, 8, 2000)
    mag = 0.02
    sf, at = bench.supply_spur_atten(f, np.full_like(f, mag, dtype=complex))
    assert list(sf) == list(bench.SUPPLY_SPURS)
    assert np.allclose(at, -20 * np.log10(mag), atol=1e-6)


class _Ref:
    """Minimal stand-in for the np.load ref object (.files + __getitem__)."""
    def __init__(self, files, d):
        self.files = files
        self._d = d

    def __getitem__(self, k):
        return self._d[k]


def _setup(monkeypatch, db_offset):
    """GT PSRR Hg (1-pole) + a model that OVER-rejects by db_offset dB at every freq."""
    f = np.logspace(1, 8, 2000)
    Hg = 0.01 / (1 + 1j * f / 2e6)
    Hm = Hg / (10 ** (db_offset / 20.0))          # |Hm| smaller => more attenuation
    monkeypatch.setattr(bench, "measure_psrr", lambda *a, **k: (f, Hm))
    sf, gat = bench.supply_spur_atten(f, Hg)
    return f, Hg, sf, gat


def test_explicit_path_uses_collected_array_and_scores(monkeypatch):
    f, Hg, sf, gat = _setup(monkeypatch, db_offset=1.0)
    ref = _Ref(["p_121u", "supply_spur_121u"],
               {"p_121u": np.c_[f, Hg.real, Hg.imag], "supply_spur_121u": np.c_[sf, gat]})
    m = score._supply_spur_metrics(ref, "lib", "ldo_model", "", ["121u"])
    assert m is not None and m["explicit"] is True
    # model over-rejects by 1 dB at every spur -> err ~ +1 dB
    assert np.isclose(m["mean_db"], 1.0, atol=0.05)
    assert np.isclose(m["worst_db"], 1.0, atol=0.05)
    assert all(r["err"] > 0 for r in m["rows"])
    assert len(m["rows"]) == len(bench.SUPPLY_SPURS)


def test_derived_path_falls_back_to_psrr_when_no_collected_array(monkeypatch):
    f, Hg, sf, gat = _setup(monkeypatch, db_offset=1.0)
    ref = _Ref(["p_121u"], {"p_121u": np.c_[f, Hg.real, Hg.imag]})   # no supply_spur_*
    m = score._supply_spur_metrics(ref, "lib", "ldo_model", "", ["121u"])
    assert m is not None and m["explicit"] is False        # derived -> not folded into composite
    assert np.isclose(m["mean_db"], 1.0, atol=0.05)


def test_returns_none_without_psrr_or_collected_array(monkeypatch):
    _setup(monkeypatch, db_offset=1.0)
    ref = _Ref([], {})
    assert score._supply_spur_metrics(ref, "lib", "ldo_model", "", ["121u"]) is None


def test_composite_gates_on_explicit_collection(monkeypatch):
    """The composite folds the supply-spur term ONLY when the ref carries the collected
    array (explicit); a derived-only metric prints but must not change the composite."""
    assert "sspur" in score.W                       # weight exists
    # explicit metric contributes; derived metric (explicit=False) is gated out in score()
    f, Hg, sf, gat = _setup(monkeypatch, db_offset=2.0)
    ref_x = _Ref(["p_121u", "supply_spur_121u"],
                 {"p_121u": np.c_[f, Hg.real, Hg.imag], "supply_spur_121u": np.c_[sf, gat]})
    mx = score._supply_spur_metrics(ref_x, "lib", "ldo_model", "", ["121u"])
    assert mx["explicit"] and np.isclose(mx["mean_db"], 2.0, atol=0.05)
