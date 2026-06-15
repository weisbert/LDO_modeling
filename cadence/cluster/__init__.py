"""cadence.cluster -- the pure-CLI Donau+ALPS corner driver (Component A, Path-B).

Runs ONE process corner of the in-situ PMU LDO extraction end-to-end WITHOUT a live
Virtuoso/Maestro session: assemble the engine command (alps wrapper or spectre), wrap it
in a Donau `dsub` submit, poll the job to completion, and hand back the classic-PSF output
directory the rest of the pipeline (binpsf.py -> importmp -> fit) already consumes.

Mirrors the ADE run-drive (cadence/insitu/run.py) but on the cluster, off the company box.
Everything here is validated against cadence/ALPS_DONAU_NOTES.md (the research log): the
exact alps cmdline (§1c/§7), the dsub flag table (§2a''), the wrapper-not-raw lib-env fix
(§9), the engine model-tree selection (§1d), and the dual-engine table (§8).

Layers:
  alps_cli   build_sim_cmd(engine, ...) -> the engine invocation (alps wrapper | spectre)
  donau      DonauCfg + build_dsub_cmd / submit / poll / cancel (all via injectable runner)
  run_corner run_corner(...) -> psf_dir : submit -> poll -> verify -> return the PSF dir

NO simulator and NO dsub are required at IMPORT or TEST time -- every subprocess call goes
through an injectable `runner`, so unit tests inject a fake. The real runner (a thin
subprocess wrapper) only fires on the company box.
"""
from .alps_cli import (
    ALPS_WRAPPER_DEFAULT,
    build_sim_cmd,
)
from .donau import (
    DonauCfg,
    DonauError,
    SubprocessRunner,
    build_dsub_cmd,
    cancel,
    map_state,
    poll,
    submit,
)
# NB: we re-export RunCornerError but NOT the run_corner FUNCTION here -- re-exporting it
# would shadow the `run_corner` SUBMODULE on the package namespace, so a caller doing
# `from cadence.cluster import run_corner` would silently get the function, not the module.
# The pinned interface is module-qualified -- `cadence.cluster.run_corner.run_corner(...)` --
# so callers import the submodule; importing the package eagerly loads it (below).
from . import run_corner          # noqa: F401  (ensure the submodule is importable/attr-present)
from .run_corner import RunCornerError

__all__ = [
    "ALPS_WRAPPER_DEFAULT",
    "build_sim_cmd",
    "DonauCfg",
    "DonauError",
    "SubprocessRunner",
    "build_dsub_cmd",
    "submit",
    "poll",
    "cancel",
    "map_state",
    "run_corner",        # the SUBMODULE (cluster.run_corner.run_corner is the entry point)
    "RunCornerError",
]
