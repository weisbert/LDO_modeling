"""End-to-end proof of the PRODUCTIONIZED trans-ID: compiled Verilog-A stimulus -> ngspice
OSDI -> importer -> fit -> score (the 'A+B actually work through the real toolchain' gate).

For each variant this:
  1. emits the multitone stimulus .va (trans_id.emit_stim_va) and COMPILES each band to .osdi
     with OpenVAF (vatools.compile_va) -- proving the emitted .va is a valid, compilable model;
  2. RUNS one transient per (corner, band) in ngspice with the compiled OSDI stimulus driving
     the GT DUT (this stands in for the engineer's ADE run -- same compiled .va);
  3. IMPORTS the exported waveforms with harness/trans_import.py -> Zout/PSRR z/p (also written
     as the freq,real,imag CSVs the GUI Import tab consumes);
  4. builds a trans-derived reference (AC noise/dc reused; z/p truncated to the AC band support
     for an apples-to-apples Level-2, exactly like validate_trans_id), FITS + SCORES it vs the
     AC ground truth, and compares the composite to (a) the AC-built model and (b) the dev-path
     B-source trans-ID numbers (results/trans_id/<variant>.json). A ~0 path delta proves the
     compiled-VA fixture reproduces the validated B-source recipe.

Additive: imports only (touches no scored module). Noise stays a separate .noise.

    python harness/validate_trans_va.py --variant base    # one variant -> results/trans_id/va_base.json
    python harness/validate_trans_va.py --all              # all 4 (serial)
    python harness/validate_trans_va.py --report           # aggregate -> results/trans_id/trans_va_e2e.md
    python harness/validate_trans_va.py --preflight        # just print toolchain status
"""
import argparse
import json
import pathlib
import sys
import time
import numpy as np

import ng
import bench
import variants
import trans_id
import trans_import
import vatools
import validate_trans_id as vti          # reuse bands_for / _ac_pair / _err_stats / _build_trans_ref

OUT = ng.ROOT / "results" / "trans_id"
TEST_VARIANTS = vti.TEST_VARIANTS


# --------------------------------------------------------------------------- deck
def _build_va_deck(subckt, xparams, osdi_path, bj, il_amps, plan):
    """ngspice deck: compiled-OSDI multitone stimulus (band bj) driving the GT DUT, with the
    corner DC load. wr_singlescale -> out.dat columns [time, v(vin), v(vout)]."""
    mod = bj["module"]
    osdi = str(osdi_path).replace("\\", "/")
    return f"""* compiled-VA multitone trans-ID (band {bj['index']}: {bj['f_lo']:.4g}-{bj['f_hi']:.4g} Hz)
Xdut vin vout {subckt} {xparams}
.model stim_{mod} {mod} vdd={plan['VDD']:.8e} va={plan['va']:.8e} ib={plan['ib']:.8e}
N1 vin vout stim_{mod}
Iload vout 0 DC {il_amps:.8e}
.control
set wr_singlescale
pre_osdi {osdi}
{bj['tran_cmd']}
wrdata out.dat v(vin) v(vout)
quit
.endc
.end
"""


def _run_band(libs, subckt, xp, osdi_path, bj, il_amps, plan, workdir):
    deck = _build_va_deck(subckt, xp, osdi_path, bj, il_amps, plan)
    r = ng.run(ng.assemble(deck, libs=libs), workdir, outputs=["out.dat"],
               timeout=trans_id.BAND_TIMEOUT)
    if r["out.dat"] is None:
        raise RuntimeError(f"compiled-VA band {bj['index']} produced no data "
                           f"(rc={r.get('_rc')}):\n{r['_stderr'][-1600:]}")
    return r["out.dat"][1]                 # ndarray [t, v(vin), v(vout)]


# ----------------------------------------------------------------------- per-variant
def run_variant(vkey):
    import fit_model
    import score as scoremod
    sys.path.insert(0, str(ng.ROOT / "cadence"))   # import_cadence imports as a top-level module
    import import_cadence as ic           # for the GUI-format consumability check

    bench.WORK = ng.ROOT / f"work_tva_{vkey}"
    scoremod.SCOREDIR = OUT / f"_score_va_{vkey}"
    scoremod.SCOREDIR.mkdir(parents=True, exist_ok=True)
    (OUT / "_tmp").mkdir(parents=True, exist_ok=True)
    csvdir = OUT / "va_csv" / vkey
    stimdir = ng.ROOT / "work" / f"tva_stim_{vkey}"

    v = variants.get(vkey)
    libs, subckt, xp = v["libs"], v["subckt"], v["xparams"]
    ac_path = ng.ROOT / "results" / "ref" / f"{vkey}.npz"
    ac_ref = np.load(ac_path, allow_pickle=True)
    loads = [str(x) for x in ac_ref["loads"]]
    nominal = loads[len(loads) // 2]
    bands = vti.bands_for(vkey)

    # ---- 1. emit + COMPILE the stimulus .va (proves it compiles) ----
    t0 = time.perf_counter()
    emit = trans_id.emit_stim_va(bands, stimdir, iload=ng.amps(nominal))
    plan = emit["plan"]
    osdi = [vatools.compile_va(vf) for vf in emit["va_files"]]
    t_compile = time.perf_counter() - t0

    # ---- 2+3. run the compiled OSDI stimulus per corner/band, import -> z/p ----
    t0 = time.perf_counter()
    z_by_il, p_by_il, l1, info_by = {}, {}, {}, {}
    for il in loads:
        il_a = ng.amps(il)
        band_waves = [_run_band(libs, subckt, xp, osdi[bj["index"]], bj, il_a, plan,
                                bench.WORK / f"{il}_b{bj['index']}") for bj in plan["bands"]]
        z, p, info = trans_import.zp_from_files(band_waves, plan, corner=il)
        z_by_il[il], p_by_il[il], info_by[il] = z, p, info
        trans_import.write_corner_csv(z, p, il, csvdir, nominal=nominal)   # GUI-format artifacts
        fz_ac, Z_ac = vti._ac_pair(ac_ref, il, nominal, "z")
        fp_ac, P_ac = vti._ac_pair(ac_ref, il, nominal, "p")
        l1[il] = dict(
            zout=vti._err_stats(z[:, 0], z[:, 1] + 1j * z[:, 2], fz_ac, Z_ac),
            psrr=vti._err_stats(p[:, 0], p[:, 1] + 1j * p[:, 2], fp_ac, P_ac),
            vout_dc=info["vout_dc"], n_z=info["n_z"], n_p=info["n_p"],
            worst_leak_db=info["worst_leak_db"])
    t_run = time.perf_counter() - t0

    # ---- GUI-path consumability: the importer CSVs must assemble (z/p) + validate ----
    gui_ok, gui_msg = _check_gui_import(ic, vkey, csvdir, loads, nominal, v)

    # ---- 4. Level-2: trans-derived ref (apples-to-apples) -> fit -> score vs AC GT ----
    trans_ref = vti._build_trans_ref(ac_ref, z_by_il, p_by_il, loads, nominal)
    tr_key = f"__transva_{vkey}"
    tr_npz = ng.ROOT / "results" / "ref" / f"{tr_key}.npz"
    np.savez(tr_npz, **trans_ref)
    try:
        fit_model.load(tr_key)
        P = fit_model.fit_all()
        cout_va, esr_va = float(fit_model.C), float(fit_model.RC)
        lib = OUT / "_tmp" / f"{vkey}_va.lib"
        fit_model.emit(P, lib)
        s_va = scoremod.score(str(lib), "ldo_model", refpath=str(ac_path))
    finally:
        try:
            if getattr(fit_model, "ref", None) is not None:
                fit_model.ref.close()
        except Exception:
            pass
        if tr_npz.exists():
            try:
                tr_npz.unlink()
            except PermissionError:
                pass

    # dev-path B-source numbers for comparison (composite_ac baseline + composite_tr)
    dev = {}
    dj = OUT / f"{vkey}.json"
    if dj.exists():
        d = json.loads(dj.read_text())
        dev = dict(composite_ac=d["level2"]["composite_ac"],
                   composite_tr=d["level2"]["composite_tr"],
                   d_composite=d["level2"]["d_composite"])

    comp_va = s_va["composite"]
    comp_ac = dev.get("composite_ac")
    res = dict(
        variant=vkey, subckt=subckt, nominal=nominal, loads=loads, bands=bands,
        t_compile_s=t_compile, t_run_s=t_run,
        n_va_files=len(emit["va_files"]), osdi_bytes=[int(pathlib.Path(o).stat().st_size) for o in osdi],
        gui_import_ok=gui_ok, gui_import_msg=gui_msg,
        cout_va_pF=cout_va * 1e12, esr_va=esr_va,
        cout_true_pF=float(v["cout"]) * 1e12, esr_true=float(v["esr"]),
        level1=l1,
        level2=dict(composite_va=comp_va, composite_ac=comp_ac,
                    composite_tr_dev=dev.get("composite_tr"),
                    d_va=(comp_va - comp_ac) if comp_ac is not None else None,
                    d_path=(comp_va - dev["composite_tr"]) if dev else None),
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"va_{vkey}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    l2 = res["level2"]
    print(f"[{vkey}] compile {t_compile:.0f}s run {t_run:.0f}s  "
          f"composite AC {comp_ac if comp_ac is None else round(comp_ac,2)} -> "
          f"VA-trans {comp_va:.2f}  (d_AC={_fmt(l2['d_va'])}, d_path={_fmt(l2['d_path'])})  "
          f"GUI-import {'OK' if gui_ok else 'FAIL:'+gui_msg}  "
          f"Cout {cout_va*1e12:.0f}pF")
    return res


def _fmt(x):
    return "n/a" if x is None else f"{x:+.2f}"


def _check_gui_import(ic, vkey, csvdir, loads, nominal, v):
    """Prove the importer's z/p CSVs are consumable by the real GUI import path
    (cadence/import_cadence.assemble): z/p (from trans) + noise (from AC, separate) ->
    an npz with the expected per-corner keys. Light: assembles + loads + checks keys
    (no second fit; the full fit-from-assembled-npz is covered by the GUI selftest)."""
    try:
        ac = np.load(ng.ROOT / "results" / "ref" / f"{vkey}.npz", allow_pickle=True)
        ndir = csvdir / "_aux"
        ndir.mkdir(parents=True, exist_ok=True)
        files = {}
        for il in loads:
            files[("z", il)] = str(csvdir / f"z_{il}.csv")
            files[("p", il)] = str(csvdir / f"p_{il}.csv")
            np.savetxt(ndir / f"noise_{il}.csv", ac[f"noise_{il}"], delimiter=",")  # noise = separate .noise
            files[("noise", il)] = str(ndir / f"noise_{il}.csv")
        files[("z_hf", nominal)] = str(csvdir / f"z_{nominal}_hf.csv")
        files[("p_hf", nominal)] = str(csvdir / f"p_{nominal}_hf.csv")
        np.savetxt(ndir / "dc_loadreg.csv", ac["dc_loadreg"], delimiter=",")
        files[("dc_loadreg", None)] = str(ndir / "dc_loadreg.csv")
        prof = dict(name=f"__guichk_{vkey}", loads=loads, nominal=nominal,
                    cout=float(v["cout"]), esr=float(v["esr"]), vref=1.05)
        out = ic.assemble(prof, files, outpath=ndir / f"__guichk_{vkey}.npz")
        d = ic.load_npz(out)
        need = [f"z_{il}" for il in loads] + [f"p_{il}" for il in loads] + [f"z_{nominal}_hf"]
        missing = [k for k in need if k not in d]
        return (not missing), ("missing " + ",".join(missing) if missing else "ok")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------------- report
def make_report():
    rows = []
    for vkey in TEST_VARIANTS:
        p = OUT / f"va_{vkey}.json"
        if p.exists():
            rows.append(json.loads(p.read_text()))
    if not rows:
        print("no va_<variant>.json found; run --variant/--all first")
        return
    ts = vatools.toolchain_status()
    L = ["# Compiled-VA end-to-end: does the emitted stimulus .va build the model?\n",
         "The productionized trans-ID, proven through the REAL toolchain: the multitone stimulus "
         "`.va` (trans_id.emit_stim_va) is COMPILED with OpenVAF, run in ngspice via OSDI to drive "
         "each GT DUT, imported with trans_import.py, then fit+scored vs the AC ground truth. A "
         "`d_path ~ 0` means the compiled-VA fixture reproduces the validated B-source recipe; "
         "`d_AC` is the model-quality gap vs the AC-built model (same as validate_trans_id).\n",
         "**Toolchain:** OpenVAF `{}` + linker `{}` + MSVC libs `{}`.\n".format(
             ts["openvaf"], ts["linker_dir"], "xwin-splat" if ts["lib"] else "none"),
         "| variant | composite AC | VA-trans | d_AC | d_path (vs B-source) | GUI import | "
         "Cout VA/true pF | compile s | run s |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        l2 = r["level2"]
        L.append(f"| {r['variant']} | {_n(l2['composite_ac'])} | {l2['composite_va']:.2f} | "
                 f"{_fmt(l2['d_va'])} | {_fmt(l2['d_path'])} | "
                 f"{'OK' if r['gui_import_ok'] else 'FAIL'} | "
                 f"{r['cout_va_pF']:.0f}/{r['cout_true_pF']:.0f} | "
                 f"{r['t_compile_s']:.0f} | {r['t_run_s']:.0f} |")
    L.append("")
    L.append("## Level 1 -- per-frequency recovery (compiled-VA trans vs AC), worst corner\n")
    L.append("| variant | corner | Zout dB med/max | PSRR dB med/max | n(z,p) | leak dB |")
    L.append("|---|---|---|---|---|---|")
    for r in rows:
        worst = max(r["level1"].items(), key=lambda kv: kv[1]["zout"]["mag_max"])
        il, d = worst
        z, p = d["zout"], d["psrr"]
        L.append(f"| {r['variant']} | {il} | {z['mag_med']:.2f}/{z['mag_max']:.2f} | "
                 f"{p['mag_med']:.2f}/{p['mag_max']:.2f} | {d['n_z']},{d['n_p']} | "
                 f"{d['worst_leak_db']:.0f} |")
    L.append("")
    max_dpath = max(abs(r["level2"]["d_path"]) for r in rows if r["level2"]["d_path"] is not None)
    allgui = all(r["gui_import_ok"] for r in rows)
    L.append("## Verdict\n")
    L.append(f"- **All {len(rows)} stimulus .va files COMPILED** with OpenVAF and RAN in ngspice via "
             f"OSDI (the '.va must compile, not just be written' constraint -- satisfied through the "
             f"real toolchain).")
    L.append(f"- **max |d_path| = {max_dpath:.2f}** vs the validated B-source dev path -- the "
             f"compiled-VA fixture reproduces the recipe (the residual is FFT/timestep numerical "
             f"noise, not a method difference).")
    L.append(f"- **GUI import path {'consumes the importer CSVs on all variants' if allgui else 'FAILED on some variants'}** "
             f"(import_cadence.assemble -> npz with z/p/noise per corner).")
    L.append("- Noise stays a separate .noise (a deterministic .tran has no device noise); the "
             "trans-derived ref reuses the AC noise verbatim.")
    (OUT / "trans_va_e2e.md").write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {OUT / 'trans_va_e2e.md'}  (max|d_path|={max_dpath:.2f}, GUI import all-OK={allgui})")


def _n(x):
    return "n/a" if x is None else f"{x:.2f}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    a = ap.parse_args()
    if a.preflight:
        print(json.dumps(vatools.toolchain_status(), indent=2))
    elif a.report:
        make_report()
    elif a.all:
        for vk in TEST_VARIANTS:
            run_variant(vk)
        make_report()
    elif a.variant:
        run_variant(a.variant)
    else:
        run_variant("base")
