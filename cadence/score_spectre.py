"""Score an emitted behavioral model against a reference npz — under Spectre 18.1.

ngspice isn't installed on this box, so harness/score.py (which re-runs the model
in ngspice) can't grade locally. This driver reuses score.py's *exact* metric
logic but monkeypatches its measurement backend to Spectre (cadence/bench_spectre).

    python score_spectre.py --variant va_rt          # grade model/ldo_va_rt.va vs results/ref/va_rt.npz
    python score_spectre.py --lib <path.va> --ref <ref.npz> [--subckt ldo_model]
"""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "cadence"))

import score                      # noqa: E402
import bench_spectre              # noqa: E402

score.bench = bench_spectre       # swap ngspice backend -> Spectre


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="va_rt")
    ap.add_argument("--lib", default=None, help="candidate .va (default model/ldo_<variant>.va)")
    ap.add_argument("--subckt", default="ldo_model")
    ap.add_argument("--ref", default=None, help="reference npz (default results/ref/<variant>.npz)")
    ap.add_argument("--xparams", default="")
    a = ap.parse_args()
    name = "ldo_model" if a.variant == "base" else f"ldo_{a.variant}"
    lib = a.lib or str(ROOT / "model" / f"{name}.va")
    refpath = a.ref or str(ROOT / "results" / "ref" / f"{a.variant}.npz")
    score.SCOREDIR = ROOT / "results" / "score" / a.variant   # per-variant plots (parallel-safe)
    print(f"scoring  lib={lib}\n         ref={refpath}  subckt={a.subckt}")
    score.score(lib, a.subckt, a.xparams, refpath=refpath)


if __name__ == "__main__":
    main()
