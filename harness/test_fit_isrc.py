"""Regression guard for the BEHAVIORAL current-source fit+emit: the single model
template (fit_isrc -> emit_isrc) must reproduce ALL >=8 diverse MOS-transistor
archetypes in-simulator (anti-overfit). Re-runs the model-vs-GT cross-validation
and asserts every archetype passes + the I-V/noise fits are good. Needs ngspice +
work_isrc/*.npz (run harness/isrc_char.py first). `python -m pytest test_fit_isrc.py -q`.
"""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from isrc_variants import VARIANTS                                     # noqa: E402
from fit_isrc import fit_isrc                                          # noqa: E402
import crossval_isrc as xv                                            # noqa: E402

WORK = HERE.parent / "work_isrc"


def test_iv_and_noise_fit_quality():
    for name in VARIANTS:
        p = fit_isrc(WORK / f"{name}.npz")
        assert p["iv_r2"] > 0.90, f"{name}: I-V fit R2={p['iv_r2']:.3f} too low"
        assert p["in_r2"] > 0.80, f"{name}: noise fit R2={p['in_r2']:.3f} too low"


def test_template_reproduces_all_archetypes():
    rows = [xv.crossval(n) for n in VARIANTS]
    bad = [r["name"] for r in rows if not r["ok"]]
    assert not bad, f"behavioral template failed to reproduce: {bad}"
    assert len(rows) >= 6


if __name__ == "__main__":
    test_iv_and_noise_fit_quality()
    test_template_reproduces_all_archetypes()
    print(f"behavioral fit: I-V + noise quality OK and {len(VARIANTS)}/{len(VARIANTS)} "
          "archetypes reproduced in-simulator")
