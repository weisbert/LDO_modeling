"""GENERALIZATION MATRIX runner. For each variant: gen_reference -> fit_model
(with Cout/ESR auto-extract) -> score. Tabulates composite + the sub-metrics
that break + extracted-vs-true Cout + the spur-linearity gate.

Sequential by design: the bench work dirs are shared, so runs must not overlap.
Per-variant full logs + overlays + a markdown/JSON summary go to
results/generalization/.

    python run_matrix.py                       # all variants in the registry
    python run_matrix.py base v1_nmos v2_capless
"""
import sys
import io
import json
import shutil
import contextlib
import numpy as np

import ng
import variants
import gen_reference
import fit_model
import score as scoremod

OUT = ng.ROOT / "results" / "generalization"


def run_one(k, reuse=False):
    v = variants.get(k)
    name = "ldo_model" if k == "base" else f"ldo_{k}"
    lib = ng.ROOT / "model" / f"{name}.lib"
    ref = ng.ROOT / "results" / "ref" / f"{k}.npz"
    buf = io.StringIO()
    err = None
    summ = None
    cout = esr = None
    try:
        with contextlib.redirect_stdout(buf):
            if not (reuse and ref.exists()):       # refs are fitter-independent -> reuse if asked
                gen_reference.main(k)
            fit_model.load(k)
            P = fit_model.fit_all()
            cout, esr = fit_model.C, fit_model.RC
            fit_model.emit(P, lib)
            fit_model.emit_va(P, ng.ROOT / "model" / f"{name}.va",
                              ng.ROOT / "model" / f"{name}_dropout.tbl")
            summ = scoremod.score(str(lib), "ldo_model", refpath=str(ref))
    except Exception as e:  # a variant that fails to converge/fit shouldn't kill the matrix
        err = f"{type(e).__name__}: {e}"
    (OUT / f"{k}.log").write_text(buf.getvalue() + (f"\n!!! ERROR: {err}\n" if err else ""),
                                  encoding="utf-8")
    # preserve overlays under variant-named files
    for il in ("20u", "121u", "250u"):
        src = ng.ROOT / "results" / "score" / f"overlay_{il}.png"
        if src.exists():
            shutil.copy(src, OUT / f"{k}_overlay_{il}.png")
    row = dict(variant=k, note=v["note"], error=err,
               cout_true_pF=float(v["cout"]) * 1e12, esr_true=float(v["esr"]),
               cout_fit_pF=(cout * 1e12 if cout else None), esr_fit=(esr if esr else None))
    if summ:
        row["composite"] = summ["composite"]
        row["spur16"] = summ["spur16"]
        row["spur24"] = summ["spur24"]
        row["big_wrms"] = summ["big_wrms"]
        row["slew_wrms"] = summ["slew_wrms"]
        row["spur_n"] = summ.get("spur_n", 0)
        row["spur_worst_db"] = summ.get("spur_worst_db")
        row["spur_miss"] = summ.get("spur_missed")
        row["spur_false"] = summ.get("spur_false")
        row["zpass_ok"] = summ.get("zpass_synth_ok")
        row["minre_gt"] = summ.get("zout_minre_gt")
        # worst-corner sub-metrics (the breakers show here)
        for m in ("zrms", "zband", "zphase", "pkdb", "pband", "pphase", "wrms", "npsd"):
            row[m + "_max"] = max(abs(p[m]) for p in summ["per"])
        row["pkf_121"] = [p["pkf"] for p in summ["per"] if p["il"] == "121u"][0]
    return row


def md_table(rows):
    cols = [("variant", "{}"), ("composite", "{:.1f}"), ("zrms_max", "{:.2f}"),
            ("zband_max", "{:.2f}"), ("pkdb_max", "{:.1f}"), ("pkf_121", "{:.2f}"),
            ("pband_max", "{:.2f}"), ("pphase_max", "{:.0f}"), ("npsd_max", "{:.1f}"),
            ("wrms_max", "{:.0f}"), ("spur_n", "{:.0f}"), ("spur_worst_db", "{:.2f}"),
            ("zpass_ok", "{}"), ("minre_gt", "{:.2f}"),
            ("cout_fit_pF", "{:.0f}"), ("spur16", "{:.0f}")]
    head = "| " + " | ".join(c for c, _ in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines = [head, sep]
    for r in rows:
        cells = []
        for c, fmt in cols:
            val = r.get(c)
            if isinstance(val, bool):
                cells.append(str(val))
            elif isinstance(val, (int, float)):
                cells.append(fmt.format(val))
            elif isinstance(val, str):
                cells.append(val)
            else:
                cells.append("ERR" if r.get("error") else "-")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(keys, reuse=False):
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for k in keys:
        print(f"--- running variant '{k}' ...", flush=True)
        r = run_one(k, reuse=reuse)
        tag = r.get("error") or f"composite={r.get('composite')}"
        print(f"    done: {tag}", flush=True)
        rows.append(r)
    (OUT / "matrix.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    md = "# Generalization matrix\n\n" + md_table(rows) + "\n"
    (OUT / "matrix.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"\nwrote {OUT/'matrix.md'} , matrix.json , per-variant logs + overlays")


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "--reuse"]
    reuse = "--reuse" in sys.argv[1:]
    keys = argv or list(variants.VARIANTS.keys())
    main(keys, reuse=reuse)
