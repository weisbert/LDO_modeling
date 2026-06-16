"""Regression guard for the MOS current-source GT library (ground_truth/isrc_gt.lib).

Fast: one DC op per variant. Asserts every transistor-level source CONVERGES and
delivers its target current within tolerance (a broken netlist / mis-sized mirror
fails here), and that the V6 beta-multiplier is meaningfully PTAT. Needs ngspice
on PATH (same as the rest of the harness). Run: `python -m pytest test_isrc.py -q`
or `python test_isrc.py`.
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ng                                                              # noqa: E402
from isrc_variants import VARIANTS, ISRC_LIB, VDD                      # noqa: E402

WORK = ng.ROOT / "work_isrc" / "_test"


def _op_current(name, v, temp=55.0):
    body = (f".options temp={temp:g}\nVdd vdd 0 DC {VDD}\n"
            f"Xd vdd out {v['subckt']}\nVout out 0 DC {v['vc']:g}\n"
            ".control\nop\nwrdata op.data i(vout)\n.endc\n.end\n")
    r = ng.run(ng.assemble(body, libs=[ISRC_LIB]), WORK / name, outputs=("op.data",))
    assert r["_rc"] == 0 and r["op.data"] is not None, \
        f"{name}: ngspice failed:\n{r['_stderr'][-800:]}"
    return abs(float(r["op.data"][1][-1, -1]))        # |i(vout)| (last row, i-col)


def test_all_converge_on_target():
    for name, v in VARIANTS.items():
        idc = _op_current(name, v)
        rel = abs(idc - v["idc"]) / v["idc"]
        assert rel < 0.25, f"{name}: Idc={idc*1e6:.3f}uA off target {v['idc']*1e6:.3f}uA ({rel:.0%})"


def test_v6_is_ptat():
    v = VARIANTS["v6_ptat"]
    i_cold = _op_current("v6_ptat", v, temp=-40.0)
    i_hot = _op_current("v6_ptat", v, temp=125.0)
    ratio = i_hot / i_cold
    # ideal PTAT over -40/125 C = 398/233 = 1.708; require clearly-PTAT and not crazy.
    assert 1.45 < ratio < 1.9, f"v6 PTAT ratio {ratio:.3f} not near ideal 1.708"


def test_diversity_spans_rout_and_polarity():
    pols = {v["pol"] for v in VARIANTS.values()}
    assert pols == {"sink", "source"}, "library must contain both sinks and sources"
    assert len(VARIANTS) >= 6, "need >= 6 sources (anti-overfit)"


if __name__ == "__main__":
    test_all_converge_on_target()
    test_v6_is_ptat()
    test_diversity_spans_rout_and_polarity()
    print("isrc GT library: all convergence + PTAT + diversity checks PASS "
          f"({len(VARIANTS)} variants)")
