"""Component B -- symbol-pin -> TB-net resolver (Python side).

The GUI hands us SYMBOL PIN NAMES off the DUT instance in the PMU testbench
(AVDD1P0, VDD0P8_DIG, IBP_POLY_500N_VCO_Fit, ...). The manifest the rest of the
flow is driven by speaks in actual TB NET names, so those pins must be resolved to
the nets the DUT instance terminals connect to. This module drives the SKILL helper
cadence/skill/resolve_nets.il over a live skillbridge session and returns a plain
{pin: net} dict -- exactly the `netmap` that component C (build_manifest) consumes.

Public API (pinned cross-component interface):
    resolve_nets(tb_lib, tb_cell, tb_view, inst_name, pins, session=None)
        -> {pin: net}
    load_skill(session) -> the live ws (sources resolve_nets.il)

`session` is the injection point for the live Virtuoso bridge:
  * a live skillbridge Workspace (or any object exposing ws["fn"](...) call style)
    -> used directly (lets tests inject a mock; lets callers reuse an open bridge).
  * None -> we try to open a real bridge (skillbridge.Workspace.open()); if
    skillbridge is not importable or no server is listening, we raise
    ResolveUnavailable. This is the offline case: NO live Virtuoso here, so the
    resolver refuses cleanly instead of pretending.

There is no offline fallback that fabricates nets -- a net we cannot read from the
live DB is reported by the SKILL side as the literal marker "<unresolved>" and
passed straight through, so a resolver gap is visible (not guessed).

Company-box usage (CIW, with the skillbridge server running):
    ; once per Virtuoso session, from the CIW:
    load("/abs/.../cadence/skill/resolve_nets.il")
    ; then from Python (a shell with skillbridge + a Workspace id matching the CIW):
    from insitu.resolve import resolve_nets
    netmap = resolve_nets("PMU_TB_lib", "pmu_tb", "schematic", "I0",
                          ["AVDD1P0", "VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO",
                           "IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                           "IBP_PTAT_TUNE_1P5U_VCO"])
    # -> {"AVDD1P0": "<net>", "VDD0P8_DIG": "<net>", ...}
The same call is reachable directly in the CIW:
    insResolveNets("PMU_TB_lib" "pmu_tb" "schematic" "I0" list("AVDD1P0" ...))
"""
import pathlib

from . import SKILL_DIR

#: the SKILL helper this module drives (sourced via load_skill)
RESOLVE_IL = SKILL_DIR / "resolve_nets.il"

#: SKILL marker for a pin whose net could not be read (no instTerm, or net nil on an
#: unchecked schematic, or a missing instance/cellview). Passed through verbatim so a
#: gap is visible to component C rather than silently dropped or guessed.
UNRESOLVED = "<unresolved>"


class ResolveUnavailable(RuntimeError):
    """Raised when net resolution needs a live Virtuoso/skillbridge session and none
    is available (skillbridge not importable, no server listening, or session=None in
    an offline environment). A clean, catchable signal -- the caller can degrade to a
    designer-supplied netmap rather than crash."""


def _open_ws(timeout=8.0):
    """Open a real skillbridge Workspace, or raise ResolveUnavailable. Three failure modes
    all collapse to ResolveUnavailable so the offline path is a single catchable failure:
      * skillbridge not importable (package absent),
      * Workspace.open() raises (connection refused / bad config),
      * Workspace.open() BLOCKS (skillbridge installed but no CIW server listening -- the
        connect hangs). We bound it with a worker-thread deadline so a missing server fails
        FAST instead of hanging the caller. The abandoned worker thread (if any) is daemon-
        like via shutdown(wait=False); it costs nothing once the process exits."""
    try:
        from skillbridge import Workspace
    except Exception as e:                                   # noqa: BLE001
        raise ResolveUnavailable(
            "skillbridge is not importable -- no live Virtuoso bridge "
            f"({e!r})") from e
    import concurrent.futures as _f
    ex = _f.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(Workspace.open)
    try:
        return fut.result(timeout=timeout)
    except _f.TimeoutError as e:
        raise ResolveUnavailable(
            f"timed out ({timeout:.0f}s) opening a skillbridge Workspace -- is the Virtuoso "
            "CIW skillbridge server running? (a missing server makes the connect block)") from e
    except Exception as e:                                   # noqa: BLE001
        raise ResolveUnavailable(
            "could not open a skillbridge Workspace -- is the Virtuoso "
            f"skillbridge server running? ({e!r})") from e
    finally:
        ex.shutdown(wait=False)             # never block on a hung connect


def load_skill(session):
    """Source cadence/skill/resolve_nets.il into the live session so insResolveNets is
    defined, and return the ws used. `session` may be a live ws (used as-is) or None
    (open a real bridge -> ResolveUnavailable offline). Idempotent: re-loading the .il
    just redefines the functions."""
    ws = session if session is not None else _open_ws()
    if not RESOLVE_IL.is_file():
        raise FileNotFoundError(f"resolver SKILL not found: {RESOLVE_IL}")
    ws["load"](str(RESOLVE_IL))
    return ws


def resolve_nets(tb_lib, tb_cell, tb_view, inst_name, pins, session=None):
    """Map each symbol pin on the DUT instance to the TB net it connects to.

    Parameters
    ----------
    tb_lib, tb_cell, tb_view : str
        The testbench cellview to open (read-only) in the live DB.
    inst_name : str
        The DUT instance name inside that cellview.
    pins : list[str]
        Symbol pin names (scalars or bus pins like "ctrl<3:0>").
    session : live ws | None
        A live skillbridge Workspace to reuse, or None to open one. With no live
        session available this raises ResolveUnavailable (no offline fabrication).

    Returns
    -------
    dict[str, str]
        {pin -> net}. Bus pins are expanded to bits by the SKILL side, so each bit
        gets its own entry (e.g. "ctrl<3>" .. "ctrl<0>"). A pin whose net could not
        be read maps to the literal marker UNRESOLVED ("<unresolved>").

    Raises
    ------
    ResolveUnavailable
        When no live Virtuoso/skillbridge session is available.
    """
    ws = load_skill(session)
    pins = list(pins)
    pairs = ws["insResolveNets"](tb_lib, tb_cell, tb_view, inst_name, pins)
    # SKILL returns a list of (pin net) 2-element lists; normalise to a dict. Be
    # defensive about the wire form (lists/tuples) coming back over the bridge.
    out = {}
    for pair in (pairs or []):
        p = list(pair)
        if len(p) >= 2:
            out[str(p[0])] = str(p[1])
    return out


__all__ = ["resolve_nets", "load_skill", "ResolveUnavailable", "UNRESOLVED",
           "RESOLVE_IL"]
