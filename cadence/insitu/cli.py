"""P6 -- headless end-to-end CLI (acceptance criterion 1).

    python -m insitu doctor   --manifest pmu_top
    python -m insitu augment  --manifest pmu_top
    python -m insitu run-only --manifest pmu_top [--backend spectre_cli|ade]
    python -m insitu import   --manifest pmu_top [--backend ...]
    python -m insitu run      --manifest pmu_top [--backend ...] [--session fnxSession0]

`run` chains augment(ade only) -> run -> PSF -> npz -> fit, with NO human/Claude
intervention, exits non-zero on any gate failure, and prints a one-screen summary. The
default backend is `spectre_cli` (deterministic dev fixture, reproduces the trusted gold);
`--backend ade` drives the real Maestro run (rides Job Setup -> cluster on the company box).
"""
import argparse
import pathlib
import sys

from . import manifest as _manifest, importmp as _imp, run as _run, augment as _augment, ROOT

GOLD = ROOT / "results" / "ref" / "pmu_standin.npz"


# ------------------------------------------------------------------------- doctor
def doctor(m, session="fnxSession0"):
    """Print session + Qt + DUT availability (the P0 deliverable)."""
    print(f"insitu doctor -- manifest '{m['name']}'")
    print(_manifest.summary(m))
    # numpy/scipy
    import numpy
    print(f"  numpy {numpy.__version__}")
    # Qt (the GUI binding; the dev VM may lack it -- --selftest stays Qt-free)
    try:
        import PyQt5  # noqa: F401
        print("  PyQt5: present")
    except Exception as e:                                     # noqa: BLE001
        print(f"  PyQt5: ABSENT ({type(e).__name__}) -- GUI runs on the experimental box; "
              "headless paths stay Qt-free")
    # live skillbridge session + DUT
    try:
        from skillbridge import Workspace
        ws = Workspace.open()
        sdb = ws["axlGetMainSetupDB"](session)
        tests = ws["axlGetTests"](sdb)[1] if sdb else None
        print(f"  skillbridge: live; ADE-XL session '{session}' setupDB={sdb} tests={tests}")
        d = m["dut"]
        obj = ws["ddGetObj"](d["lib"], d["cell"])
        print(f"  DUT {d['lib']}/{d['cell']}: {'present' if obj else 'MISSING'}; "
              f"TB {d['tb_lib']}/{d['tb_cell']}")
    except Exception as e:                                     # noqa: BLE001
        print(f"  skillbridge/session: unavailable ({type(e).__name__}: {e}) -- "
              "spectre_cli backend still works headless")
    print("  CLI fixture PSF:", "present" if (_run.WORK_CLI).exists() else "absent "
          "(run `--backend spectre_cli --regenerate` or cadence/extract_pmu.py)")
    return 0


# ----------------------------------------------------------------------- gate
def gate_vs_gold(npz_dict, tol=1e-6):
    """Compare the produced multi-port arrays to the trusted CLI gold within tolerance.
    Returns (passed, worst_err, detail). Missing gold -> skipped (passed=None)."""
    if not GOLD.exists():
        return None, 0.0, "no gold reference present -- gate skipped"
    import numpy as np
    gold = np.load(GOLD, allow_pickle=True)
    gkeys = [k for k in gold.files if not k.startswith("meta_") and k != "loads"]
    worst, worst_k = 0.0, None
    for k in gkeys:
        if k not in npz_dict:
            return False, float("inf"), f"missing array {k}"
        a, b = npz_dict[k], gold[k]
        if a.shape != b.shape:
            return False, float("inf"), f"shape mismatch {k}: {a.shape} vs {b.shape}"
        num = np.abs(a[:, 1:] - b[:, 1:]); den = np.abs(b[:, 1:]) + 1e-30
        err = float(min(np.max(num / den), np.max(num)))      # rel OR abs (near-zero couple)
        if err > worst:
            worst, worst_k = err, k
    return worst <= tol, worst, f"worst={worst:.2e} @ {worst_k}"


# ----------------------------------------------------------------------- chain
def produce_npz(m, backend, session, regenerate):
    """run -> PSF -> multi-port npz dict + write results/ref/<name>_<backend>.npz."""
    import numpy as np
    r = _run.run(m, backend=backend, session=session, regenerate=regenerate)
    arrays = _imp.from_psf_multiport(psf_map=r.get("psf_map"), root=None, manifest=m,
                                     load="nom", probe_aliases=r.get("probe_aliases"))
    out = {"loads": np.array(["nom"]), **arrays,
           "meta_backend": np.array(backend), "meta_note": np.array("ADE-native in-situ")}
    path = ROOT / "results" / "ref" / f"{m['name']}_{backend}.npz"
    np.savez(path, **out)
    return path, out, r


def cmd_run(m, backend, session, regenerate, tol):
    print(f"== insitu run ({backend}) ==")
    if backend == "ade":
        print("augment: building extraction TB on the live session ...")
        _augment.build(m)
    path, npz, r = produce_npz(m, backend, session, regenerate)
    print(f"run: {len(r['psf_map'])} PSF dirs -> npz {path.name} ({len(npz)-3} arrays)")
    passed, worst, detail = gate_vs_gold(npz, tol=tol)
    gate_str = ("SKIP" if passed is None else ("PASS" if passed else "FAIL"))
    print(f"gate vs gold: {gate_str}  ({detail})")
    # fit + report
    sys.path.insert(0, str(ROOT / "harness"))
    import fit_multiport as FMP
    res = FMP.fit_multiport(path, m)
    print()
    print(FMP.report(res))
    if passed is False:
        print("\nFAIL: ADE-path npz does not match the trusted CLI gold -- stop and investigate.")
        return 2
    return 0


# ----------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="insitu", description="In-situ LDO extraction (Mechanism A)")
    ap.add_argument("cmd", choices=["doctor", "augment", "run-only", "import", "run"])
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--backend", default="spectre_cli", choices=["spectre_cli", "ade"])
    ap.add_argument("--session", default="fnxSession0")
    ap.add_argument("--regenerate", action="store_true",
                    help="spectre_cli: re-run extract_pmu to (re)produce PSF")
    ap.add_argument("--tol", type=float, default=1e-6, help="gate tolerance vs the gold")
    a = ap.parse_args(argv)
    m = _manifest.load(a.manifest)

    if a.cmd == "doctor":
        return doctor(m, session=a.session)
    if a.cmd == "augment":
        if a.backend != "ade":
            print("augment applies to the ade backend (builds Test_PMU_extract on the session)")
        _augment.build(m)
        return 0
    if a.cmd == "run-only":
        r = _run.run(m, backend=a.backend, session=a.session, regenerate=a.regenerate)
        print(f"backend={r['backend']}  {len(r['psf_map'])} PSF dirs")
        for tag, p in sorted(r["psf_map"].items()):
            print(f"  {tag:16s} {p}")
        return 0
    if a.cmd == "import":
        path, npz, _ = produce_npz(m, a.backend, a.session, a.regenerate)
        passed, worst, detail = gate_vs_gold(npz, tol=a.tol)
        print(f"wrote {path}  ({len(npz)-3} arrays); gate "
              f"{'SKIP' if passed is None else ('PASS' if passed else 'FAIL')} ({detail})")
        return 0 if passed is not False else 2
    if a.cmd == "run":
        return cmd_run(m, a.backend, a.session, a.regenerate, a.tol)
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
