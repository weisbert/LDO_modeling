"""insitu -- Mechanism A: ADE-native in-situ multi-port LDO extraction.

Re-routes the validated `cadence/extract_pmu.py` recipe (today: standalone `spectre
-64` CLI) through ADE-XL/Maestro, so the SAME extraction is (a) visible in Maestro,
(b) rides the company's existing ADE-XL Job-Setup -> cluster dispatch unchanged, and
(c) exercises the real PSF -> npz production path. Everything downstream of PSF (the
npz contract firewall, the pure-Python fit, the Verilog-A emit) stays
engine/launcher-agnostic.

Layers (each a thin wrapper over a reused asset; see MECHANISM_A_PLAN.md):
  manifest    P1  pin-role manifest: the designer-supplied contract (roles + nets +
                  stim) the whole flow is driven by. Capture-and-augment, never rebuild.
  augment     P2  SKILL via skillbridge: copy the designer TB -> <tb>_extract, append
                  acm_* AC sources/probes + ac/noise analyses + targeted saves.
  run         P3  run-drive: axlRunAllTests Submit -> poll axlGetRunStatus -> rename
                  (the simkit pvtRunner sequence, copied not imported). Resolves the
                  PSF tree (tries psf/ [ALPS] and netlist/ [Spectre]).
  importmp    P4  PSF -> generalized multi-port npz (wraps cadence/import_cadence).
                  GATE: must reproduce the trusted CLI results/ref/pmu_standin.npz.
  fit         P5  per-port fit (reuses harness/fit_model fitters) + report.
  cli         P6  `python -m insitu ...` headless end-to-end (acceptance criterion 1).
  extractcore P7  Qt-free ExtractCore for the GUI Extract tab (acceptance criterion 2).

This package sits under cadence/ next to its reused siblings (psf, spectre_run,
import_cadence, skill_lib). Importing it puts cadence/ on sys.path so those bare
imports resolve exactly as the existing modules expect (mirrors extract_pmu.py).
"""
import pathlib
import sys

CADENCE = pathlib.Path(__file__).resolve().parents[1]   # .../LDO_modeling/cadence
ROOT = CADENCE.parent                                    # .../LDO_modeling
SKILL_DIR = CADENCE / "skill"
MANIFEST_DIR = pathlib.Path(__file__).resolve().parent / "manifests"

# Make the reused cadence/ siblings (psf, spectre_run, import_cadence, skill_lib)
# AND harness/ importable by bare name, matching the repo's existing convention.
for _p in (str(CADENCE), str(ROOT / "harness")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

__all__ = ["CADENCE", "ROOT", "SKILL_DIR", "MANIFEST_DIR"]
__version__ = "0.1.0"
