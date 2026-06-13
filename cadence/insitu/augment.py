"""P2 -- ADE augmentation engine: build the in-situ extraction testbench (the heart).

Drives cadence/skill/insitu_augment.il over the live skillbridge session to turn the
designer's working TB into an extraction copy <tb>_extract by APPENDING -- never
rebuilding -- extraction stimuli/probes at the manifest-tagged nets:

  v_out  o -> 1 A AC isource (mag=acm_v_out_<o>) at the output net          (Zout/couple)
  supply s -> the EXISTING supply source's acm set to acm_supply_<s>        (PSRR/cur-PSRR)
  i_out  c -> OUR named probe vsource (dc bias, mag=acm_i_out_<c>)          (admittance)

All acm_* design variables default 0, so the shared in-situ OP is untouched; the run
side sets exactly one to 1 per point (AC superposition -> identifiable transfers).

This module only needs a live Virtuoso session for the actual build; build_plan() is
pure and headless (used by the doctor / GUI preview / dry runs).
"""
import pathlib

from . import SKILL_DIR, manifest as _manifest

ISOURCE = ("analogLib", "isource")
VSOURCE = ("analogLib", "vsource")


def _ws():
    from skillbridge import Workspace
    return Workspace.open()


def design_vars(m):
    """Every acm_* design variable the augment creates, default 0 (declared on the run
    test). One per stimulus the manifest can drive."""
    dv = {}
    for o in m["v_out"]:
        dv[_manifest.acm_var("v_out", o)] = 0.0
    for s in m["supplies"]:
        dv[_manifest.acm_var("supply", s)] = 0.0
    for c in m["i_out"]:
        dv[_manifest.acm_var("i_out", c)] = 0.0
    return dv


def build_plan(m):
    """Pure, session-free description of what build() will do (for doctor/preview/dry-run):
    a list of (action, detail) tuples."""
    d = m["dut"]
    plan = [("copy", f"{d['tb_lib']}/{d['tb_cell']} -> {d['tb_lib']}/{d['extract_cell']}")]
    for o, v in m["v_out"].items():
        plan.append(("isource", f"Iext_{o} (gnd!,{v['net']}) acm={_manifest.acm_var('v_out', o)}"))
    for c, v in m["i_out"].items():
        probe = _manifest._probe_name(m, c)
        plan.append(("probe", f"{probe} ({v['net']},gnd!) dc={v['dc']} "
                              f"acm={_manifest.acm_var('i_out', c)}"))
    for s, v in m["supplies"].items():
        src = v.get("tb_src")
        plan.append(("supply-acm", f"{src or '<auto>'} ({v['net']}) acm={_manifest.acm_var('supply', s)}"
                                   + ("" if src else "  [WARN: no tb_src in manifest]")))
    plan.append(("vars", ", ".join(design_vars(m))))
    plan.append(("save", "dbSave " + d["extract_cell"]))
    return plan


def build(m, ws=None, verbose=True):
    """Build/refresh <tb>_extract on the live session per the manifest. Idempotent
    (dbCopyCellView overwrite=t rebuilds the copy each call). Returns dict(extract_cell,
    design_vars, plan). Requires a live skillbridge session."""
    ws = ws or _ws()
    ws["load"](str(SKILL_DIR / "insitu_augment.il"))
    d = m["dut"]
    tb_lib, tb_cell, ext_cell = d["tb_lib"], d["tb_cell"], d["extract_cell"]

    # close any dangling open copy (a prior interrupted build leaves it open in edit
    # mode, which blocks dbCopyCellView overwrite). Idempotency guard.
    for ocv in (ws["dbGetOpenCellViews"]() or []):
        try:
            if ocv.cellName == ext_cell:
                ws["dbClose"](ocv)
        except Exception:                                     # noqa: BLE001
            pass

    # 1) copy the designer TB -> extraction copy (capture-and-augment)
    ws["insituCopyTB"](tb_lib, tb_cell, ext_cell, tb_lib, "schematic")
    cv = ws["dbOpenCellViewByType"](tb_lib, ext_cell, "schematic", "schematic", "a")
    try:
        y = 0.0
        # 2) v_out injection isources (PLUS=gnd!, MINUS=out -> +1A into out, matches CLI)
        for o, v in m["v_out"].items():
            acm = _manifest.acm_var("v_out", o)
            ws["insituAddSource"](cv, ISOURCE[0], ISOURCE[1], f"Iext_{o}", [20.0, y],
                                  [["PLUS", m["ground"]], ["MINUS", v["net"]]],
                                  [["acm", "string", acm]])
            y -= 2.0
        # 3) i_out named probe vsources (PLUS=sink, MINUS=gnd!; read <probe>:p)
        for c, v in m["i_out"].items():
            probe = _manifest._probe_name(m, c)
            acm = _manifest.acm_var("i_out", c)
            ws["insituAddSource"](cv, VSOURCE[0], VSOURCE[1], probe, [24.0, y],
                                  [["PLUS", v["net"]], ["MINUS", m["ground"]]],
                                  [["dc", "string", repr(float(v["dc"]))],
                                   ["acm", "string", acm]])
            y -= 2.0
        # 4) make the existing supply sources AC-able
        for s, v in m["supplies"].items():
            src = v.get("tb_src")
            if not src:
                raise _manifest.ManifestError(
                    f"supplies.{s} has no 'tb_src' -- augment needs the TB source instance "
                    f"name driving net {v['net']} to set its acm. Add it to the manifest.")
            ws["insituSetSupplyAcm"](cv, src, _manifest.acm_var("supply", s))
        # 5) check connectivity (extracts the by-name labels) + save. A non-zero schCheck
        #    ERROR count means an appended source did NOT bind to its DUT net -> the run
        #    would be mis-wired; fail loudly instead of persisting/running it.
        chk = ws["insituSaveCV"](cv)
        if chk and int(chk[0]) > 0:
            raise RuntimeError(
                f"augment: schCheck reported {int(chk[0])} error(s) on {ext_cell} "
                f"(appended stimuli may not bind to the DUT nets) -- aborting before run")
    except Exception:
        try:
            ws["dbClose"](cv)
        except Exception:                                     # noqa: BLE001
            pass
        raise
    out = dict(extract_cell=ext_cell, design_vars=design_vars(m), plan=build_plan(m))
    if verbose:
        print(f"augment: built {tb_lib}/{ext_cell} with {len(out['design_vars'])} acm vars")
    return out


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Build the in-situ extraction TB (Mechanism A P2)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, do not touch Cadence")
    a = ap.parse_args()
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from insitu import manifest as M
    m = M.load(a.manifest)
    if a.dry_run:
        for act, det in build_plan(m):
            print(f"  {act:12s} {det}")
    else:
        build(m)
