"""P5 -- multi-port fit + report for the in-situ extraction (Mechanism A).

A real PMU exposes SEVERAL modeled ports (two voltage-output LDOs that share a VREF/bias,
plus current-sink outputs). fit_model.py models ONE voltage output (Zout + one PSRR +
noise). This module GENERALIZES to multi-port by REUSING fit_model's proven per-output
fitters -- it does NOT reimplement them:

  voltage output  o : fit_model.fit_cout_esr / fit_zout / fit_psrr (x each supply) /
                      fit_noise_bank   -- the identical building blocks, in a loop.
  current sink    c : a small NEW fit -- admittance Y(s)=g0+sC and current-PSRR pi(s)
                      (low order; a sink is a near-ideal conductance + parasitic cap).

Why the building blocks and not fit_variant(): fit_variant -> fit_all needs a DC
load-regulation sweep + current-labeled load corners (ng.amps), which an in-situ
small-signal extraction does not carry. The building blocks are pure (arrays in, params
out), so we drive them directly over an npz-like per-output VIEW, saving/restoring
fit_model's module globals around each output so outputs never cross-contaminate
(PLL Cout=1n vs VCO Cout=4.7n live in C/RC -> set per output).

The report breaks out CURRENT-port error SEPARATELY from voltage-port error (a debug
requirement: a current-sink model that is off must be visible, not averaged away).

    python -m harness.fit_multiport --variant pmu_standin_ade --manifest pmu_top
    # or:  python harness/fit_multiport.py --variant pmu_standin --manifest pmu_top
"""
import contextlib
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_model as FM          # the per-output fitters we reuse  # noqa: E402


class _NpzLike(dict):
    """A dict that also answers `k in obj.files` (fit_model probes ref.files)."""
    @property
    def files(self):
        return list(self.keys())


@contextlib.contextmanager
def _fm_globals():
    """Save/restore the fit_model module globals we mutate, so each output (and the
    caller's process) is isolated."""
    keys = ("ref", "LOADS", "NOMINAL", "C", "RC", "CFT", "VREF", "NFK", "MNOISE",
            "NOISE_MODE", "NFKV", "NSPUR_F", "NSPUR_PH")
    saved = {k: getattr(FM, k) for k in keys}
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(FM, k, v)


# ----------------------------------------------------------------- voltage outputs
def _fit_voltage_output(o, view, supplies, vout_dc=0.8):
    """Fit one voltage output from its single-port view (split_ports output).
    view = {"npz": {z_<il>,p_<il>,noise_<il>,loads,meta_*}, "supplies": {s:{il:arr}}, ...}.
    Returns dict(P={il:params}, nfk, cout, esr, err=[per-corner per-metric], supplies=[...])."""
    sp = view["npz"]
    loads = view["loads"]
    nom = loads[len(loads) // 2]
    with _fm_globals():
        FM.ref = _NpzLike(sp)
        FM.LOADS = list(loads)
        FM.NOMINAL = nom
        FM.CFT = 0.0
        FM.fit_cft()                       # gate (stays silent on the stand-in)
        FM.C, FM.RC = FM.fit_cout_esr()    # this output's physical Cout/ESR
        cout, esr = FM.C, FM.RC
        zfits, P, err = {}, {}, []
        for il in loads:
            gz = sp[f"z_{il}"]; fz = gz[:, 0]; Z = gz[:, 1] + 1j * gz[:, 2]
            R_a, L_a, R_pl, R_b, L_b = FM.fit_zout(fz, Z)
            zfits[il] = (R_a, L_a, R_pl, R_b, L_b)
            iv = FM._amps(il) if _amps_ok(il) else 0.0
            P[il] = dict(iv=iv, R_a=R_a, L_a=L_a, R_pl=R_pl, R_b=R_b, L_b=L_b,
                         vreg=vout_dc + R_a * iv)
            # PSRR per supply -- the primary supply's params live on P[il]; all supplies'
            # fits are kept in per-supply dicts for the report.
            psrr_params = {}
            for s in supplies:
                gp = view["supplies"][s][il]
                fp = gp[:, 0]; H = gp[:, 1] + 1j * gp[:, 2]
                G, Q = FM.fit_psrr(fp, H, R_a, L_a, R_pl, R_b, L_b)
                psrr_params[s] = (G, Q)
            prim = view.get("primary_supply") or supplies[0]
            G, Q = psrr_params[prim]
            P[il].update(G0=G[0], G1=G[1], w1=G[2], G2=G[3], w2=G[4], G3=G[5], w3=G[6],
                         pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3], _psrr=psrr_params)
        # joint Norton-@vout noise bank over corners (reads FM.ref noise_<il>, FM.C/RC)
        NB = FM.fit_noise_bank(zfits)
        nfk = list(NB["fk"])
        for il in loads:
            P[il]["gnw"] = NB["gw"][il]
            for k in range(len(nfk)):
                P[il][f"gn{k+1}"] = NB["gk"][il][k]
        # per-corner per-metric error (model vs GT), using the SAME transfer fns as the fit
        for il in loads:
            e = dict(il=il)
            gz = sp[f"z_{il}"]; fz = gz[:, 0]; Zg = gz[:, 1] + 1j * gz[:, 2]
            Zm = FM.zmodel(fz, P[il]["R_a"], P[il]["L_a"], P[il]["R_pl"],
                           P[il]["R_b"], P[il]["L_b"])
            e["zrms"] = _rms_db(Zm, Zg)
            e["psrr"] = {}
            for s in supplies:
                gp = view["supplies"][s][il]; fp = gp[:, 0]; Hg = gp[:, 1] + 1j * gp[:, 2]
                G, Q = P[il]["_psrr"][s]
                Hm = FM.psrr_model(fp, P[il]["R_a"], P[il]["L_a"], P[il]["R_pl"],
                                   P[il]["R_b"], P[il]["L_b"], G, Q)
                sel = fp >= 1e3
                e["psrr"][s] = (_rms_db(Hm, Hg),
                                float(np.degrees(np.sqrt(np.mean(
                                    np.angle(Hm[sel] / Hg[sel]) ** 2)))))
            gn = sp[f"noise_{il}"]; fn = gn[:, 0]; Sg = gn[:, 1]
            Sm = FM.noise_model_sv(P[il], fn, FM.zmodel(fn, *zfits[il]),
                                   nfk=nfk, nmode="norton")
            e["nrms"] = float(np.sqrt(np.mean(
                (20 * np.log10((Sm + 1e-30) / (Sg + 1e-30))) ** 2)))
            err.append(e)
    return dict(P=P, nfk=nfk, cout=cout, esr=esr, err=err, supplies=list(supplies))


def _amps_ok(il):
    try:
        FM._amps(il); return True
    except Exception:
        return False


def _rms_db(model, gt):
    return float(np.sqrt(np.mean((20 * np.log10(np.abs(model) / np.abs(gt))) ** 2)))


# ----------------------------------------------------------------- current sinks
def _fit_admittance(f, Y):
    """Y(s) ~ g0 + s*Cp  (sink output conductance + parasitic cap), complex LS in
    [g0, Cp]. Degenerate-safe: <2 points -> constant g0 only; a non-physical negative
    parasitic cap is clamped to 0 (a sink cap cannot be negative). Returns (g0, Cp, rms_db)."""
    w = 2 * np.pi * f
    if f.size < 2:                                    # rank-deficient -> constant model
        g0 = float(np.mean(Y).real)
        return g0, 0.0, _rms_db(np.full_like(Y, g0), Y)
    A = np.c_[np.ones_like(w), 1j * w]                # [1, jw]
    x, *_ = np.linalg.lstsq(A, Y, rcond=None)
    g0, Cp = float(x[0].real), max(float(x[1].real), 0.0)   # clamp: parasitic cap >= 0
    Ym = g0 + 1j * w * Cp
    return g0, Cp, _rms_db(Ym, Y)


def _fit_cpsrr(f, PI):
    """current-PSRR pi(s) ~ c0 + s*c1 (low order; near-flat for a behavioral sink).
    Degenerate-safe: <2 points -> complex constant c0 only. Returns (c0, c1, rms_db)."""
    w = 2 * np.pi * f
    if f.size < 2:
        c0 = complex(np.mean(PI))
        return c0, 0j, _rms_db(np.full_like(PI, c0), PI)
    A = np.c_[np.ones_like(w), 1j * w]
    x, *_ = np.linalg.lstsq(A, PI, rcond=None)
    c0, c1 = complex(x[0]), complex(x[1])
    PIm = c0 + 1j * w * c1
    return c0, c1, _rms_db(PIm, PI)


def _fit_current_ports(cports, supplies):
    """Fit each current sink's admittance + current-PSRR. Returns a list of report rows."""
    rows = []
    for c, cp in cports.items():
        for il in cp["loads"]:
            row = dict(sink=c, il=il)
            if il in cp["y"]:
                g = cp["y"][il]; f = g[:, 0]; Y = g[:, 1] + 1j * g[:, 2]
                g0, Cp, yrms = _fit_admittance(f, Y)
                row.update(g0=g0, Cp=Cp, yrms=yrms, ydc=float(np.abs(Y[0])))
            row["pi"] = {}
            for (s, il2), arr in cp["pi"].items():
                if il2 != il:
                    continue
                f = arr[:, 0]; PI = arr[:, 1] + 1j * arr[:, 2]
                c0, c1, prms = _fit_cpsrr(f, PI)
                row["pi"][s] = dict(rms=prms, dc=float(np.abs(PI[0])))
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- driver
def fit_multiport(npz_path, manifest, vout_dc=None):
    """Fit every modeled port of a multi-port npz. Returns a structured result dict:
    {voltage: {o: <fit>}, current: [rows], meta}. Pure-Python; no simulator."""
    from insitu import importmp as IM
    ref = IM.load_multiport(npz_path)
    m = manifest
    vmap = vout_dc or {}
    views = IM.split_ports(ref, m)
    cports = IM.current_ports(ref, m)
    supplies = list(m["supplies"])
    volt = {}
    for o in m["v_out"]:
        vdc = vmap.get(o, 0.8)
        volt[o] = _fit_voltage_output(o, views[o], supplies, vout_dc=vdc)
        # carry the designer's GUI symbol pin name (set by build_manifest) so the model
        # cell's PORT is the pin, not our internal role key. Default: the role key itself
        # (the stand-in manifest carries no 'pin', so it stays 'pll'/'vco' etc.).
        volt[o]["pin"] = m["v_out"][o].get("pin", o)
    curr = _fit_current_ports(cports, m["current_psrr_supplies"])
    for r in curr:
        r["pin"] = m["i_out"].get(r["sink"], {}).get("pin", r["sink"])
    # provenance for the emit banner (emit_pmu_va reads these off meta by default, so
    # step_emit needs no new args). All optional / defensive -- a coverage-free or
    # hand-built manifest leaves them None and the banner falls back to 'unspecified'.
    try:
        from insitu import manifest as _Mp
        cov = m.get("coverage") or {}
        coverage_tier = cov.get("tier")
        temps = list(cov.get("temps") or [])
        op_temp = temps[len(temps) // 2] if temps else None
        # union load envelope over every v_out's declared load_points (None when none declared)
        all_loads = []
        for o in m.get("v_out", {}):
            all_loads += [float(x) for x in _Mp.load_points(m, o)]
        valid_load = (min(all_loads), max(all_loads)) if all_loads else None
        op_iload = all_loads[0] if all_loads else None
    except Exception:                              # noqa: BLE001 -- provenance is best-effort
        coverage_tier = valid_load = op_iload = op_temp = None
    return dict(voltage=volt, current=curr,
                meta=dict(name=pathlib.Path(npz_path).stem,
                          loads=[str(x) for x in ref["loads"]],
                          supplies=supplies,
                          coverage_tier=coverage_tier, valid_load=valid_load,
                          op_iload=op_iload, op_temp=op_temp))


def export_single_port_refs(npz_path, manifest, vout_dc=None, outdir=None):
    """Write each voltage output as a SINGLE-port npz (results/ref/<variant>_<o>.npz) that
    the EXISTING ModelerCore / fit_model.fit_variant / emit consume UNCHANGED -- so the GUI
    Fit/Compare tabs and the Verilog-A emit work per output with ZERO new fit/emit code.

    The in-situ OP (one iload, set by the designer's TB) maps to fit_model's iload axis:
    the corner key is the manifest iload (e.g. '500u').

    ANTI-FOOTGUN (stage 2a): we DO NOT fabricate DC. When the SOURCE multi-port npz carries
    a REAL dropout sweep for output o (key 'dc_<o>' or 'dc_<o>_<load>', shape [Iload, Vout]
    from importmp's 'dropout' derive), we carry it through as fit_model's dc_loadreg AND
    dc_dropout (the same real load sweep of the regulated output -- the in-situ sweep does
    not distinguish the two, so both read the one real curve). When the npz has NO real dc
    array for o (a small-signal-only T0 export), we OMIT dc_loadreg/dc_dropout ENTIRELY ->
    the single-port emit emits NO dropout/load-reg/current-limit term (honest scope), rather
    than a flat fabricated stand-in. dc_linereg has no in-situ line-reg sweep yet -> always
    omitted (never fabricated) unless a real one is present. Returns {output: path}.

    NOTE on axes: multi-PVT-corner single-port modeling (PVT != iload) is handled by the
    multiport report's own per-load loop; this single-port export targets the GUI's
    one-DUT-at-a-time path and uses the nominal corner."""
    from insitu import importmp as IM
    ref = IM.load_multiport(npz_path)
    m = manifest
    vmap = vout_dc or {}
    views = IM.split_ports(ref, m)
    loads = [str(x) for x in ref["loads"]]
    nom = loads[len(loads) // 2]
    outdir = pathlib.Path(outdir) if outdir else (ROOT / "results" / "ref")
    outdir.mkdir(parents=True, exist_ok=True)
    stem = pathlib.Path(npz_path).stem
    out_paths = {}
    for o, v in views.items():
        sp = v["npz"]
        meta = m["v_out"][o]
        iload = float(meta.get("iload", 500e-6))
        ilkey = _amps_to_key(iload)
        vdc = vmap.get(o, meta.get("vout_dc", 0.8))
        rec = {"loads": np.array([ilkey]),
               f"z_{ilkey}": sp[f"z_{nom}"],
               f"p_{ilkey}": sp[f"p_{nom}"],
               f"noise_{ilkey}": sp[f"noise_{nom}"],
               "meta_cout": sp.get("meta_cout", np.array(np.nan)),
               "meta_esr": sp.get("meta_esr", np.array(np.nan)),
               "meta_port": np.array(o), "meta_vout_dc": np.array(vdc)}
        # REAL DC only -- no fabrication. The dropout sweep lands in the FULL multi-port ref
        # (split_ports does not carry it into the per-output view), keyed 'dc_<o>' or
        # 'dc_<o>_<load>', shape [Iload, Vout]. When present, feed it to fit_model as BOTH
        # dc_loadreg and dc_dropout (the one real load sweep of the regulated output). When
        # absent -> emit NOTHING for the DC term (small-signal-only scope; the consumers in
        # fit_model gracefully skip the dropout/load-reg branch). dc_linereg: no in-situ
        # line-reg sweep -> omitted unless a real one is present.
        dckey = next((k for k in ref if k == f"dc_{o}" or k.startswith(f"dc_{o}_")), None)
        if dckey is not None:
            dc_real = np.asarray(ref[dckey])
            rec["dc_loadreg"] = dc_real
            rec["dc_dropout"] = dc_real
        lrkey = next((k for k in ref
                      if k == f"linereg_{o}" or k.startswith(f"linereg_{o}_")), None)
        if lrkey is not None:
            rec["dc_linereg"] = np.asarray(ref[lrkey])
        p = outdir / f"{stem}_{o}.npz"
        np.savez(p, **rec)
        out_paths[o] = p
    return out_paths


def _amps_to_key(a):
    """amps -> a corner key fit_model.ng.amps round-trips ('500u','1m',...)."""
    for suf, sc in (("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if a >= sc:
            v = a / sc
            return (f"{v:g}{suf}")
    return f"{a:g}"


def emit_models(npz_path, manifest, vout_dc=None, modeldir=None):
    """Best-effort: export per-output single-port refs, then fit+emit each via the EXISTING
    fit_model path -> model/<variant>_<o>.va (+ .lib + dropout .tbl). Returns
    {output: {"va","lib"} | {"error"}}. Never raises: a per-output emit failure is reported,
    not fatal (the report is the always-on deliverable)."""
    refs = export_single_port_refs(npz_path, manifest, vout_dc=vout_dc)
    modeldir = pathlib.Path(modeldir) if modeldir else (ROOT / "model")
    modeldir.mkdir(parents=True, exist_ok=True)
    out = {}
    for o, refp in refs.items():
        try:
            with _fm_globals():
                fr = FM.fit_variant(refp.stem, nominal=None, vref=1.05)
                lib = modeldir / f"{refp.stem}.lib"
                va = modeldir / f"{refp.stem}.va"
                tbl = modeldir / f"{refp.stem}_dropout.tbl"
                FM.emit(fr.P, lib)
                FM.emit_va(fr.P, va, tbl)
            out[o] = {"va": va, "lib": lib, "ref": refp}
        except Exception as e:        # noqa: BLE001 -- emit is best-effort by design
            out[o] = {"error": f"{type(e).__name__}: {e}", "ref": refp}
    return out


def report(res):
    """Human report: voltage-port table, then a SEPARATE current-port table."""
    L = []
    L.append(f"=== Multi-port fit report: {res['meta']['name']} ===")
    L.append(f"loads={res['meta']['loads']}  supplies={res['meta']['supplies']}")
    L.append("")
    L.append("--- VOLTAGE OUTPUTS (Zout / PSRR per supply / noise) ---")
    sups = res["meta"]["supplies"]
    hdr = f"{'out':>5} {'load':>6} {'Cout[pF]':>9} {'ESR':>6} {'Zrms[dB]':>9}"
    for s in sups:
        hdr += f" {'P_'+s+'[dB]':>10} {'P_'+s+'[deg]':>10}"
    hdr += f" {'Nrms[dB]':>9}"
    L.append(hdr)
    for o, fit in res["voltage"].items():
        for e in fit["err"]:
            line = (f"{o:>5} {e['il']:>6} {fit['cout']*1e12:9.1f} {fit['esr']:6.3f} "
                    f"{e['zrms']:9.3f}")
            for s in sups:
                pr, pd = e["psrr"][s]
                line += f" {pr:10.3f} {pd:10.2f}"
            line += f" {e['nrms']:9.3f}"
            L.append(line)
    L.append("")
    L.append("--- CURRENT SINKS (admittance / current-PSRR) -- reported SEPARATELY ---")
    chdr = f"{'sink':>7} {'load':>6} {'g0[S]':>11} {'Cp[F]':>11} {'Yrms[dB]':>9}"
    pis = sorted({s for r in res["current"] for s in r.get("pi", {})})
    for s in pis:
        chdr += f" {'pi_'+s+'[dB]':>11}"
    L.append(chdr)
    for r in res["current"]:
        line = (f"{r['sink']:>7} {r['il']:>6} {r.get('g0', float('nan')):11.3e} "
                f"{r.get('Cp', float('nan')):11.3e} {r.get('yrms', float('nan')):9.3f}")
        for s in pis:
            line += f" {r['pi'].get(s, {}).get('rms', float('nan')):11.3f}"
        L.append(line)
    # worst-case rollup (voltage vs current kept separate)
    vz = [e["zrms"] for fit in res["voltage"].values() for e in fit["err"]]
    vp = [pr for fit in res["voltage"].values() for e in fit["err"]
          for pr, _ in e["psrr"].values()]
    vn = [e["nrms"] for fit in res["voltage"].values() for e in fit["err"]]
    cy = [r["yrms"] for r in res["current"] if "yrms" in r]
    cp = [d["rms"] for r in res["current"] for d in r.get("pi", {}).values()]
    L.append("")
    L.append(f"worst VOLTAGE  : Zout {max(vz, default=0):.2f}dB  PSRR {max(vp, default=0):.2f}dB"
             f"  noise {max(vn, default=0):.2f}dB")
    L.append(f"worst CURRENT  : Y {max(cy, default=0):.2f}dB  current-PSRR {max(cp, default=0):.2f}dB")
    return "\n".join(L)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Multi-port in-situ fit + report")
    ap.add_argument("--variant", required=True, help="results/ref/<variant>.npz stem")
    ap.add_argument("--manifest", required=True, help="pin-role manifest name/path")
    ap.add_argument("--report-out", default=None, help="write the text report here")
    ap.add_argument("--export-refs", action="store_true",
                    help="also write per-output single-port refs (results/ref/<v>_<o>.npz)")
    ap.add_argument("--emit", action="store_true",
                    help="also emit per-output Verilog-A via the existing fit_model path")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "cadence"))
    from insitu import manifest as _M
    m = _M.load(a.manifest)
    npz = ROOT / "results" / "ref" / f"{a.variant}.npz"
    res = fit_multiport(npz, m)
    txt = report(res)
    print(txt)
    if a.report_out:
        pathlib.Path(a.report_out).write_text(txt + "\n")
        print(f"\nwrote {a.report_out}")
    if a.export_refs or a.emit:
        refs = export_single_port_refs(npz, m)
        print("\nper-output single-port refs:")
        for o, p in refs.items():
            print(f"  {o}: {p}")
    if a.emit:
        em = emit_models(npz, m)
        print("\nper-output Verilog-A emit:")
        for o, r in em.items():
            print(f"  {o}: " + (str(r["va"]) if "va" in r else f"FAILED -- {r['error']}"))
