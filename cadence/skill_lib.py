"""Reusable skillbridge driver for Target B: get emitted behavioral models into a
Cadence library as Verilog-A cellviews, driven from Python over the live Virtuoso
bridge. Mirrors the file-level recipe used by the box's own dreg_gen tool
(write veriloga.va + master.tag, then ddUpdateLibList).

    from skill_lib import ensure_lib, import_va_cellview
    ensure_lib("LDO_model_lab")                                  # ddCreateLib (idempotent)
    import_va_cellview("LDO_model_lab", "ldo_model",
                       "../model/ldo_model.va", tbl="../model/ldo_model_dropout.tbl")

Needs the skillbridge server live in Virtuoso (cadence/env.sh world; see skill_tools/skillbridge).
"""
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_LIB_PATH = ROOT / "cadence" / "cds"      # self-contained lib location


def _ws():
    from skillbridge import Workspace
    return Workspace.open()


def ensure_lib(name="LDO_model_lab", path=None):
    """ddCreateLib if absent (no tech: behavioral/schematic only). Returns the lib path."""
    ws = _ws()
    libs = [str(ws['ddGetObjName'](l)) for l in ws['ddGetLibList']()]
    if name in libs:
        obj = ws['ddGetObj'](name)
        return str(ws['ddGetObjReadPath'](obj))
    path = str(path or (DEFAULT_LIB_PATH / name))
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    ws['ddCreateLib'](name, path)
    ws['ddUpdateLibList']()
    return path


def import_va_cellview(lib, cell, va_path, tbl=None, view="veriloga"):
    """Create/refresh a Verilog-A cellview <lib>/<cell>/<view> from a .va file.
    Writes veriloga.va + master.tag (+ optional $table_model .tbl) and rescans libs.
    Returns the cellview dir."""
    ws = _ws()
    libpath = pathlib.Path(ensure_lib(lib))
    cvdir = libpath / cell / view
    cvdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(va_path, cvdir / "veriloga.va")
    if tbl:
        shutil.copyfile(tbl, cvdir / pathlib.Path(tbl).name)
    (cvdir / "master.tag").write_text("-- Master.tag File, Rev:1.0\nveriloga.va\n")
    ws['ddUpdateLibList']()
    ok = bool(ws['ddGetObj'](lib, cell, view))
    if not ok:
        raise RuntimeError(f"cellview {lib}/{cell}/{view} did not register")
    return str(cvdir)


def list_cells(lib):
    ws = _ws()
    obj = ws['ddGetObj'](lib)
    return [str(ws['ddGetObjName'](c)) for c in (ws['ddGetObjChildren'](obj) or [])]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Import an emitted .va as a Cadence veriloga cellview")
    ap.add_argument("--lib", default="LDO_model_lab")
    ap.add_argument("--cell", default="ldo_model")
    ap.add_argument("--va", default=str(ROOT / "model" / "ldo_model.va"))
    ap.add_argument("--tbl", default=str(ROOT / "model" / "ldo_model_dropout.tbl"))
    a = ap.parse_args()
    print("lib path:", ensure_lib(a.lib))
    print("cellview:", import_va_cellview(a.lib, a.cell, a.va, tbl=a.tbl))
    print("cells in lib:", list_cells(a.lib))
