"""One-shot Spectre pipeline for a single GT variant: extract -> fit -> score,
emitting ONE machine-readable JSON line. Used by the Phase-2 cross-sim matrix
(parallel-safe via per-variant LDO_SPECTRE_WORK).

    python run_variant.py v4_ffpsrr
    -> {"variant":"v4_ffpsrr","ok":true,"composite":3.9,"zrms_max":0.12,...}
"""
import os
import sys
import json
import pathlib

KEY = sys.argv[1]
# isolate Spectre scratch BEFORE importing spectre_run (WORK is bound at import)
os.environ.setdefault("LDO_SPECTRE_WORK", f"work_{KEY}")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "cadence"))

import extract_ref            # noqa: E402
import fit_model              # noqa: E402
import score                  # noqa: E402
import bench_spectre          # noqa: E402

NAME = f"{KEY}_spectre"


def main():
    # ---- extract (Spectre) ----
    dut, v = extract_ref.gt_variant_dut(KEY)
    extract_ref.extract(dut, NAME, cout=v["cout"], esr=v["esr"])

    # ---- fit (scipy, pure) ----
    fit_model.load(NAME)
    P = fit_model.fit_all()
    mdir = ROOT / "model"
    lib = mdir / f"ldo_{NAME}.lib"
    va = mdir / f"ldo_{NAME}.va"
    fit_model.emit(P, lib)
    fit_model.emit_va(P, va, mdir / f"ldo_{NAME}_dropout.tbl")

    # ---- score (Spectre) ----
    score.bench = bench_spectre
    score.SCOREDIR = ROOT / "results" / "score" / NAME
    summ = score.score(str(va), "ldo_model", "",
                       refpath=str(ROOT / "results" / "ref" / f"{NAME}.npz"))
    per = summ["per"]
    mx = lambda k: max(abs(p[k]) for p in per)
    out = dict(variant=KEY, ok=True, composite=round(summ["composite"], 2),
               zrms_max=round(mx("zrms"), 3), zband_max=round(mx("zband"), 3),
               pkdb_max=round(mx("pkdb"), 2), pkf_121=round(per[1]["pkf"], 2),
               pband_max=round(mx("pband"), 3), pphase_max=round(mx("pphase"), 1),
               npsd_max=round(mx("npsd"), 2), wrms_max=round(mx("wrms"), 1),
               cout_fit_pF=round(float(fit_model.C) * 1e12, 0),
               zpass_ok=bool(summ["zpass_synth_ok"]))
    return out


if __name__ == "__main__":
    try:
        out = main()
    except Exception as e:
        out = dict(variant=KEY, ok=False, error=repr(e)[:500])
    print("RESULT_JSON " + json.dumps(out))
