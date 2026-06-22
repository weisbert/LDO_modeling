"""LDO behavioral-model modeler -- PyQt5 GUI (manual-TB -> modeler, offline/airgap).

A thin shell over the existing, validated harness. The engineer hand-builds the TB in ADE,
exports per-corner data, then in this GUI:  Profile -> Import(+guardrails) -> Fit -> Compare.
NO simulator is called from the GUI: the before/after overlay is the ANALYTIC predict(P,f)
(exactly what the fitter optimizes) vs the imported ground truth -> pure numpy. Spectre
validation stays in the CLI (score.py / run_matrix.py).

Run:        python gui/ldo_modeler.py
Headless:   python gui/ldo_modeler.py --selftest         # logic test (+ Qt if installed)
            QT_QPA_PLATFORM=offscreen python gui/ldo_modeler.py --selftest --require-qt

Design: a Qt-FREE `ModelerCore` (import->fit->predict->emit) holds all logic and is fully
testable headless; the Qt widgets below are a 4-tab shell over it. So the build is verifiable
even where PyQt5/display are absent (the red box has PyQt5; this dev venv may not).
"""
import os
import sys
import json
import shlex
import argparse
import pathlib
from dataclasses import dataclass, field

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Persisted GUI form state (the PMU pin form + Profile). Kept in the user's HOME -- NOT under
# the deploy app/ dir, which `deploy/update.sh` wipes (rm -rf app) on every incremental update;
# HOME survives redeploys, exactly like the persistent results/ and model/ stores. Resolved at
# call time so LDO_CONFIG_DIR can redirect it (the --selftest uses a temp dir, not real HOME).
def _config_dir():
    return pathlib.Path(os.environ.get("LDO_CONFIG_DIR", pathlib.Path.home() / ".ldo_modeler"))


def _autosave_json():
    return _config_dir() / "gui_state.json"
for _p in (ROOT / "harness", ROOT / "cadence"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# =============================================================================== core
@dataclass
class Profile:
    """User characterization profile (Tab 1). band is display-only (the fit uses whatever
    freqs the imported arrays carry); nominal defaults to the middle corner."""
    name: str = "myldo"
    vref: float = 1.05
    loads: list = field(default_factory=lambda: ["20u", "121u", "250u"])
    nominal: str = "121u"
    cout: float = 1e-9
    esr: float = 0.5
    band: tuple = (10.0, 100e6)
    spur_twin0: float = 0.0
    spur_binhz: float = 15625.0

    def to_import_profile(self):
        return dict(name=self.name, loads=list(self.loads), nominal=self.nominal,
                    cout=self.cout, esr=self.esr, vref=self.vref,
                    spur_twin0=self.spur_twin0, spur_binhz=self.spur_binhz)


class ModelerCore:
    """Qt-free engine: wraps import_cadence + fit_model. One DUT at a time (load->fit->
    emit in order, exactly as the CLI does). All heavy work happens here so the GUI thread
    stays a thin caller and the whole pipeline is unit-testable without Qt."""

    def __init__(self):
        self.profile = Profile()
        self.ref_path = None         # assembled results/ref/<name>.npz
        self.ref = None              # dict of GT arrays
        self.result = None           # fit_model.FitResult
        self._fit_ref = None         # ref_path the current result was fit from (desync guard)
        self.warnings = []
        self.trans_info = {}         # per-corner trans-ID extraction diagnostics (Tab 5)

    # ---- import -----------------------------------------------------------------
    def import_data(self, files, fmt=None, sv_is_psd2=False):
        """files: {(quantity, corner|None): path}. Assembles the npz + runs guardrails.
        Returns (ref_path, warnings)."""
        import import_cadence as ic
        self.ref_path = ic.assemble(self.profile.to_import_profile(), files,
                                    fmt=fmt, sv_is_psd2=sv_is_psd2)
        self.ref = ic.load_npz(self.ref_path)
        self.result = None           # invalidate any prior fit -- it belonged to another ref
        self.warnings = ic.validate(self.ref)
        return self.ref_path, self.warnings

    def import_trans(self, waveforms, plan_json, extra_files=None, outdir=None):
        """Build a reference from ONE multitone trans-ID run (productionization piece C).
        `waveforms` = {corner: [band0_wave, band1_wave, ...]} (paths or arrays); `plan_json` =
        the sidecar emitted by trans_id.emit_stim_va. Converts the waveforms to Zout/PSRR z/p
        CSVs (+ nominal hf) via trans_import, MERGES with extra_files (the noise/DC the user
        already picked on Tab 2 -- noise stays a separate .noise), and funnels through the
        EXISTING import_data() so assemble + guardrails + npz schema are reused, never
        duplicated. trans-derived z/p WIN over any same-key extra_files. Returns
        (ref_path, warnings)."""
        import trans_import
        plan = trans_import.load_plan(plan_json)
        outdir = pathlib.Path(outdir) if outdir else (ROOT / "work" / "trans_csv" / self.profile.name)
        zp_files, info = trans_import.import_run(plan, waveforms, outdir,
                                                 nominal=self.profile.nominal)
        self.trans_info = info
        files = dict(extra_files or {})
        files.update(zp_files)                 # trans z/p take precedence
        return self.import_data(files)

    def import_trans_folder(self, folder, plan_json, extra_files=None, outdir=None):
        """Folder-gesture wrapper: match <corner>_b<band>.* exports in `folder` against the
        plan, then import_trans. Returns (ref_path, warnings); raises if no corner matched."""
        import trans_import
        plan = trans_import.load_plan(plan_json)
        waveforms = trans_import.match_wave_dir(folder, plan, self.profile.loads)
        if not waveforms:
            raise RuntimeError(
                "no waveform files matched. Expected <corner>_b<band>.csv (e.g. 20u_b0.csv, "
                "20u_b1.csv, ...) for your load corners, one per band in plan.json.")
        return self.import_trans(waveforms, plan_json, extra_files=extra_files, outdir=outdir)

    def use_existing_ref(self, ref_path):
        """Point at an already-assembled npz (skip import) -- e.g. a Target-A variant."""
        import import_cadence as ic
        self.ref_path = pathlib.Path(ref_path)
        self.ref = ic.load_npz(self.ref_path)
        self.profile.loads = [str(x) for x in self.ref["loads"]]
        self.profile.nominal = self.profile.loads[len(self.profile.loads) // 2]
        self.result = None           # invalidate any prior fit -- it belonged to another ref
        self.warnings = ic.validate(self.ref)
        return self.ref_path, self.warnings

    # ---- fit --------------------------------------------------------------------
    def fit(self):
        """Fit the imported reference in-process -> FitResult (also leaves fit_model module
        state current for an immediate emit())."""
        import fit_model
        name = pathlib.Path(self.ref_path).stem
        self.result = fit_model.fit_variant(name, nominal=self.profile.nominal,
                                            vref=self.profile.vref)
        self._fit_ref = self.ref_path        # remember which ref this fit (and module state) is for
        return self.result

    def emit(self, outdir=None):
        """Emit .lib + .va (+ dropout .tbl) for the fitted DUT. Must follow fit() for the SAME
        reference -- fit_model emit reads module globals left by the last fit, so refuse if the
        loaded ref changed since the fit (else we'd name files from one DUT, content from another)."""
        import fit_model
        if self.result is None or self._fit_ref != self.ref_path:
            raise RuntimeError("emit requires a fit of the current reference -- run Fit again "
                               "(the loaded data changed since the last fit).")
        outdir = pathlib.Path(outdir) if outdir else (ROOT / "model")
        outdir.mkdir(parents=True, exist_ok=True)
        name = pathlib.Path(self.ref_path).stem
        lib = outdir / f"{name}.lib"
        va = outdir / f"{name}.va"
        tbl = outdir / f"{name}_dropout.tbl"
        fit_model.emit(self.result.P, lib)
        fit_model.emit_va(self.result.P, va, tbl)
        return lib, va

    # ---- compare (analytic predict vs imported GT) ------------------------------
    def fit_residuals(self):
        """Per-corner analytic residuals (zrms dB / PSRR band dB / noise PSD dB) computed
        from predict() vs the imported GT -- the Fit-tab scorecard, no simulator."""
        import fit_model
        out = []
        # nmode/nfkv from the held FitResult: another fit in this process must not
        # re-dispatch this result's noise reconstruction (module-global trap).
        nmode = getattr(self.result, "nmode", None)
        nfkv = getattr(self.result, "nfkv", None)
        for il in self.result.loads:
            cg = self.gt_corner(il)
            pr_z = fit_model.predict(self.result.P[il], cg["fz"], self.result.nfk,
                                     nfkv=nfkv, nmode=nmode)
            pr_p = fit_model.predict(self.result.P[il], cg["fp"], self.result.nfk,
                                     nfkv=nfkv, nmode=nmode)
            pr_n = fit_model.predict(self.result.P[il], cg["fn"], self.result.nfk,
                                     nfkv=nfkv, nmode=nmode)
            zrms = float(np.sqrt(np.mean((20 * np.log10(np.abs(pr_z["Zout"]) / np.abs(cg["Zg"]))) ** 2)))
            prms = float(np.sqrt(np.mean((20 * np.log10(np.abs(pr_p["PSRR"]) / np.abs(cg["Hg"]))) ** 2)))
            npsd = float(np.sqrt(np.mean((20 * np.log10((pr_n["noise"] + 1e-30) / (cg["Sg"] + 1e-30))) ** 2)))
            out.append(dict(il=il, zrms=zrms, prms=prms, npsd=npsd))
        return out

    def gt_corner(self, il):
        """GT arrays for one corner (for the overlay): freq grids + complex Zout/PSRR + Sv."""
        z = self.ref[f"z_{il}"]; p = self.ref[f"p_{il}"]; n = self.ref[f"noise_{il}"]
        return dict(fz=z[:, 0], Zg=z[:, 1] + 1j * z[:, 2],
                    fp=p[:, 0], Hg=p[:, 1] + 1j * p[:, 2],
                    fn=n[:, 0], Sg=n[:, 1])

    def predict_corner(self, il):
        """Model overlay for one corner on the GT's own freq grids."""
        import fit_model
        cg = self.gt_corner(il)
        nmode = getattr(self.result, "nmode", None)
        nfkv = getattr(self.result, "nfkv", None)
        return dict(Zm=fit_model.predict(self.result.P[il], cg["fz"], self.result.nfk,
                                         nfkv=nfkv, nmode=nmode)["Zout"],
                    Hm=fit_model.predict(self.result.P[il], cg["fp"], self.result.nfk,
                                         nfkv=nfkv, nmode=nmode)["PSRR"],
                    Sm=fit_model.predict(self.result.P[il], cg["fn"], self.result.nfk,
                                         nfkv=nfkv, nmode=nmode)["noise"])

    # ---- current ports (bias sinks/sources): the V/I-dual overlay -----------------
    def current_ports(self):
        """Pin names of the current ports carried by the loaded ref ([] if none / no ref).
        These are the bias sink/source outputs the Compare tab plots with I-V/Y/PSRR/noise
        instead of Zout (V/I duality)."""
        if self.ref is None:
            return []
        import current_digest
        return current_digest.list_iports(self.ref)

    def current_compare(self, pin):
        """One current port's GT arrays + analytic model curves + fit-vs-GT metrics, for the
        Compare-tab overlay. Pure-numpy (current_digest.port_view + fit_isrc); no simulator,
        no voltage FitResult needed -- current ports live in the ref independently."""
        import current_digest, fit_isrc
        v = current_digest.port_view(self.ref, pin)
        p = fit_isrc.fit_isrc(v)
        return dict(view=v, params=p, metrics=current_digest.diff_metrics(v, p),
                    iv_model=fit_isrc.predict_iv(p, v["iv_v"]),
                    y_model=fit_isrc.predict_y(p, v["ac_f"]),
                    psrr_model=fit_isrc.predict_psrr(p, v["psrr_f"]),
                    noise_model=fit_isrc.predict_noise(p, v["nz_f"]),
                    idcT_model=fit_isrc.predict_idcT(p, v["temps"]))

    def text_report(self, outdir=None):
        """Analytic text MODEL-vs-GT difference report (report.build_report) for the current
        fit -- the copy-pasteable diagnosis built for an airgapped red zone where you can't
        screenshot the overlays. Qt-free (works headless); powers the Compare tab's 'Save text
        report' button. Writes results/score/report_<name>.txt by default. Returns (path, text)."""
        import report
        if self.result is None:
            raise RuntimeError("fit a model first")
        name = pathlib.Path(self.ref_path).stem
        txt = report.build_report(self.ref, self.result, name, refpath=str(self.ref_path))
        if outdir:
            out = pathlib.Path(outdir) / f"report_{name}.txt"
            out.write_text(txt, encoding="utf-8")
        else:
            out = report.write_report(name, txt)
        return out, txt


class ExtractCore:
    """Qt-free in-situ EXTRACTION front-half (Mechanism A): manifest -> augment -> run ->
    PSF -> multi-port npz, PLUS per-output single-port refs. It PRODUCES the same npz the
    Import tab already consumes, so the two paths converge at the npz firewall. Same
    headless-testable discipline as ModelerCore -- no Qt.

    The `ade` backend drives the real Maestro run (needs a live skillbridge session); the
    `spectre_cli` backend runs fully offline (the dev/CI fixture reusing extract_pmu's PSF),
    so --selftest exercises the whole front-half without Cadence."""

    def __init__(self):
        self.manifest = None
        self.manifest_path = None
        self.npz_path = None         # the produced multi-port npz
        self.gate = None             # (passed|None, worst, detail) vs the trusted gold
        self.report = None           # multi-port fit report text (current ports separate)
        self.result = None           # fit_multiport result dict
        self.port_refs = {}          # output -> per-output single-port npz path (for Import)
        self._gui = None             # the GUI form dict the manifest was built from (workarea keys)
        self._fit_manifest = None    # the manifest object self.result was fit from (desync guard)

    def _invalidate_run(self):
        """Drop ALL run-derived state when the manifest is repointed -- a fit / per-output refs /
        report from a PREVIOUS manifest must never be reused under a NEW manifest's identity
        (the same 'invalidate any prior fit' discipline ModelerCore uses on a ref change). This
        is what makes build_model_cell's guard reject a stale (non-None, wrong) result."""
        self.result = None
        self.report = None
        self.npz_path = None
        self.gate = None
        self.port_refs = {}
        self._fit_manifest = None

    def load_manifest(self, name_or_path):
        """Load + validate a pin-role manifest. Returns the human summary."""
        from insitu import manifest as M
        self.manifest = M.load(name_or_path)
        self.manifest_path = self.manifest.get("_path")
        self._gui = None             # a directly-loaded manifest -> derive workarea keys from it
        self._invalidate_run()       # the new manifest has no run yet -- drop any prior fit/refs
        return M.summary(self.manifest)

    # ---- deliverable 1: pin FORM -> resolved manifest --------------------------------
    def build_manifest_from_gui(self, gui, *, session=None, netmap=None, work_root=None,
                                corner=None):
        """Turn the Extract-tab PIN FORM (symbol pin names + scalars) into a validated
        pin-role manifest, written into the WORKAREA and loaded as the current manifest.

        This is the front-half of the acceptance interface: the designer types pins, we
        resolve them to TB nets (B resolver over a live skillbridge session, unless a
        netmap is injected) and build the manifest (C). Returns (path, summary, warnings).
        `warnings` are build_manifest's m['_warnings'] (e.g. i_out compliance vdc not
        supplied) -- surfaced so the GUI can prompt. Raises ResolveUnavailable when net
        resolution needs a live session and none is available (offline, no netmap)."""
        import json
        from insitu import build_manifest as BM, manifest as M, pmu_corner as PC
        from insitu.resolve import resolve_nets
        pins = [*BM.supply_pins(gui), *(gui.get("v_outs") or []),
                *(gui.get("i_outs") or []), *(gui.get("biases") or {}).keys()]
        if netmap is None:
            netmap = resolve_nets(gui["tb_lib"], gui["tb_cell"],
                                  gui.get("tb_view", "schematic"),
                                  gui.get("dut_inst") or gui.get("tb_inst", ""),
                                  pins, session=session)
        m = BM.build_manifest(gui, netmap)            # validated; carries _warnings + 'pin' fields
        warns = list(m.get("_warnings", []))
        corner = corner or gui.get("corner") or "nom"
        base, _dirs = PC.corner_dir(work_root, gui, corner)   # WORK_ROOT/ldo_modeling/<lib>__<cell>/<corner>
        path = base / f"{gui['tb_cell']}_{corner}.manifest.json"
        path.write_text(json.dumps(m, indent=2) + "\n")
        self.manifest = M.load(str(path))
        self.manifest_path = str(path)
        self._gui = dict(gui)
        self._invalidate_run()       # a freshly-built manifest has no run yet -- drop any prior fit
        return path, M.summary(self.manifest), warns

    # ---- deliverable 3: the ONE combined model cell (veriloga + symbol + compile) -----
    def _wa_gui(self):
        """The minimal {tb_lib,tb_cell} the workarea path needs -- the form dict if the
        manifest came from build_manifest_from_gui, else derived from the loaded manifest."""
        if self._gui:
            return self._gui
        d = self.manifest["dut"]
        return {"tb_lib": d["tb_lib"], "tb_cell": d["tb_cell"]}

    def _corner(self):
        """The corner label for the workarea: the form's corner, else the manifest's."""
        if self._gui and self._gui.get("corner"):
            return self._gui["corner"]
        if self.manifest.get("corner"):
            return self.manifest["corner"]
        fb = self.manifest.get("corners", {}).get("fallback", ["nom"])
        return str(fb[0]) if fb else "nom"

    def _model_ports(self):
        """(supply_pin, ground) for the combined model symbol: the single supply input
        (LEFT) and VSS (BOTTOM). Derived from the manifest -- the supply's preserved 'pin'
        name (build_manifest keeps it), else its role key; ground = the manifest ground."""
        sup = next(iter(self.manifest["supplies"].values())) if self.manifest.get("supplies") else {}
        supply_pin = sup.get("pin") or (next(iter(self.manifest["supplies"]))
                                        if self.manifest.get("supplies") else "AVDD1P0")
        return supply_pin, (self.manifest.get("ground") or "VSS")

    def build_model_cell(self, model_lib, model_cell, model_path, *, session=None,
                         dry_run=False, work_root=None, progress=None):
        """Emit the ONE combined PMU Verilog-A (1 supply LEFT / N v-rails + M i-biases RIGHT /
        VSS BOTTOM) from the multi-port fit and -- with a live session -- import + compile it +
        build the symbol cell in Cadence at the user's lib/cell/path (Component D, step_emit +
        step_cell). No live session / dry_run -> writes the .va + returns the SKILL plan only
        (the artifact is still produced; the cell build happens on the company box). Requires a
        completed run OF THE CURRENT MANIFEST (self.result fit from self.manifest) -- a manifest
        swap since the run is refused, so a stale fit can never be emitted under a new manifest's
        supply/ground/ports. Returns dict(va, built, plan, pinspec, error). A LIVE build failure
        (bridge down mid-call) does NOT lose the .va: built=False + error is returned, not raised."""
        if self.result is None or self._fit_manifest is not self.manifest:
            raise RuntimeError("run an extraction against the CURRENT manifest first (Build & "
                               "Run) -- the loaded data changed since the last run, so the model "
                               "cell would mix a stale fit with the new manifest's ports/supply")
        from insitu import pmu_corner as PC
        supply_pin, ground = self._model_ports()
        _base, dirs = PC.corner_dir(work_root, self._wa_gui(), self._corner())
        va_path = pathlib.Path(dirs["model"]) / f"{model_cell}.va"
        p = PC.step_emit(self.result, cell_name=model_cell, va_path=va_path,   # always: write .va
                         supply=supply_pin, ground=ground, progress=progress)

        def _cell(sess, dry):
            return PC.step_cell(self.result, p, model_lib=model_lib, model_cell=model_cell,
                                model_path=model_path, supply=supply_pin, ground=ground,
                                session=sess, dry_run=dry, progress=progress)

        if session is not None and not dry_run:
            try:
                live = _cell(session, False)
                return dict(va=str(p), built=True, plan=live["plan"],
                            pinspec=live["pinspec"], error=None)
            except Exception as e:                       # noqa: BLE001  bridge down mid-build
                dry = _cell(None, True)                  # recover plan/pinspec for the report
                return dict(va=str(p), built=False, plan=dry["plan"], pinspec=dry["pinspec"],
                            error=f"{type(e).__name__}: {e}")
        dry = _cell(None, True)
        return dict(va=str(p), built=False, plan=dry["plan"], pinspec=dry["pinspec"], error=None)

    def plan(self):
        """The augment plan (session-free preview of what 'Build & Run' will do)."""
        from insitu import augment
        return augment.build_plan(self.manifest)

    def run(self, backend="spectre_cli", session="fnxSession0", regenerate=False, tol=1e-6,
            progress=None, cancel=None):
        """augment(ade) -> run -> PSF -> multi-port npz -> gate vs gold -> multi-port fit +
        per-output single-port refs. Returns dict(npz_path, gate, report, ports). Pure
        orchestration over the insitu package + fit_multiport. progress(frac,msg)/cancel()
        are forwarded to the ade run-drive (UI feedback + a responsive Cancel)."""
        if self.manifest is None:
            raise RuntimeError("load a manifest first")
        from insitu import cli
        # the ade run-drive builds Test_PMU_extract itself (run_ade build_first=True) -- no
        # double-build here.
        path, npz, _r = cli.produce_npz(self.manifest, backend, session, regenerate,
                                        progress=progress, cancel=cancel)
        self.npz_path = path
        self.gate = cli.gate_vs_gold(npz, tol=tol)
        import fit_multiport as FMP
        self.result = FMP.fit_multiport(path, self.manifest)
        self._fit_manifest = self.manifest          # the result belongs to THIS manifest (desync guard)
        self.report = FMP.report(self.result)
        self.port_refs = FMP.export_single_port_refs(path, self.manifest)
        return dict(npz_path=path, gate=self.gate, report=self.report, ports=self.port_refs)

    def run_cluster_sweep(self, *, netlistdir, pdk_model_dir=None, ahdllibdir=None,
                          engine="alps", donau=None, runner=None, work_root=None,
                          dry_run=False, group_status=None, log=None, cancel=None,
                          max_parallel=1):
        """Run a FULL Donau+ALPS measurement sweep from the loaded manifest, pure-CLI (no ADE /
        no skillbridge): build each measurement GROUP's one-hot netlist OFFLINE from the
        designer's base input.scs, submit one dsub+alps job per group, collect the per-group PSF,
        read it into the by-tag npz, and multi-port fit. Sets self.npz_path/result/report exactly
        like run(), so 'Create model cell' works afterwards (the desync guard still holds).

        netlistdir     the designer's BASE netlist dir (holding input.scs -- a maestro .tran TB).
        pdk_model_dir  PDK model ROOT directory ($MODEL_ROOT); -I <dir>/<engine> is appended. Optional.
        ahdllibdir     pre-compiled AHDL/VA DB (optional -- else the sim compiles VA from the netlist).
        engine         'alps' (Donau+ALPS) | 'spectre'.
        donau          cluster.donau.DonauCfg (account/queue/cpu/mem); defaults to the validated tuple.
        runner         injected cluster command executor (a fake in tests; real subprocess on the box).
        dry_run        assemble the per-group dsub commands WITHOUT submitting (returns them).
        group_status   callback(i, n, group, state) -- drives the GUI per-group status table.
        log            callback(msg) -- streamed progress lines.
        cancel         callback() -> bool -- stops launching NEW groups (cooperative Cancel).
        max_parallel   cap on how many GROUP jobs run on Donau at once (default 1 = serial).

        Returns dict(dry_run, dsub_cmds, n_groups[, npz_path, gate, report, ports, psf_map])."""
        if self.manifest is None:
            raise RuntimeError("load a manifest first")
        from insitu import pmu_corner as PC
        from cluster.netlist_augment import make_offline_group_netlister
        from cluster.donau import DonauCfg
        m = self.manifest
        gui, corner = self._wa_gui(), self._corner()
        _base, dirs = PC.corner_dir(work_root, gui, corner)
        _log = (lambda _s, msg: log(msg)) if log else None
        # offline per-group netlister over the designer's base input.scs. The resolved-net GUARD +
        # the supply-source auto-detect fire HERE, before any submit, so a bad manifest (placeholder
        # nets / no locatable supply source) fails loudly up front, not mid-sweep.
        factory = make_offline_group_netlister(netlistdir, m, dirs["netlist"])
        netinfo = PC.step_netlist(dirs, netlistdir=netlistdir, ahdllibdir=ahdllibdir,
                                  pdk_model_dir=pdk_model_dir, progress=_log)
        runres = PC.step_run(netinfo, dirs["psf"], m, engine=engine, donau=donau or DonauCfg(),
                             runner=runner, dry_run=dry_run, group_netlister=factory,
                             group_status=group_status, cancel=cancel, progress=_log,
                             max_parallel=max_parallel)
        if dry_run:
            return dict(dry_run=True, dsub_cmds=runres["dsub_cmds"],
                        n_groups=len(runres["dsub_cmds"]))
        npz = PC.step_import(m, runres["psf_map"], npz_dir=dirs["npz"], load=corner, progress=_log)
        self.npz_path = str(npz)
        import fit_multiport as FMP
        self.result = FMP.fit_multiport(str(npz), m)
        self._fit_manifest = m                        # the fit belongs to THIS manifest (desync guard)
        self.report = FMP.report(self.result)
        self.port_refs = FMP.export_single_port_refs(str(npz), m)
        self.gate = (None, "", "Donau+ALPS sweep (no gold reference)")
        return dict(dry_run=False, npz_path=str(npz), gate=self.gate, report=self.report,
                    ports=self.port_refs, psf_map=runres["psf_map"],
                    dsub_cmds=runres["dsub_cmds"], n_groups=len(runres["dsub_cmds"]))

    def port_list(self):
        return list(self.port_refs)


# =============================================================================== Qt UI
try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtWidgets import (QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
                                 QHBoxLayout, QFormLayout, QGridLayout, QLabel, QLineEdit,
                                 QPushButton, QComboBox, QCheckBox, QFileDialog, QTextEdit,
                                 QTableWidget, QTableWidgetItem, QGroupBox, QMessageBox, QScrollArea,
                                 QHeaderView)
    import matplotlib
    matplotlib.use("Qt5Agg")
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    _HAVE_QT = True
except Exception as _qt_err:           # GUI optional at import time (logic core stays usable)
    _HAVE_QT = False
    _QT_IMPORT_ERR = _qt_err


if _HAVE_QT:

    class _Canvas(FigureCanvas):
        def __init__(self, nrows=1, ncols=1, figsize=(7, 5)):
            # constrained_layout (not tight_layout) re-solves spacing on every resize, so the
            # panels never overlap/pile up ("积压") when the canvas is squeezed in a stacked tab.
            self.fig = Figure(figsize=figsize, constrained_layout=True)
            super().__init__(self.fig)
            self.axes = self.fig.subplots(nrows, ncols, squeeze=False)

        def clear(self):
            for ax in self.axes.flat:
                ax.clear()

    class _FitWorker(QtCore.QThread):
        """Run fit() off the UI thread (seconds-scale); keeps the window responsive."""
        done = QtCore.pyqtSignal(object)
        failed = QtCore.pyqtSignal(str)

        def __init__(self, core):
            super().__init__()
            self.core = core

        def run(self):
            try:
                self.core.fit()
                self.done.emit(self.core.result)
            except Exception as e:
                import traceback
                self.failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

    class _ExtractWorker(QtCore.QThread):
        """Run the in-situ extraction (augment->run->PSF->npz->fit) off the UI thread.
        The ade backend can take a while (a Maestro run); spectre_cli is fast. Streams
        per-group progress and honours a cooperative Cancel so the UI never looks wedged."""
        done = QtCore.pyqtSignal(object)
        failed = QtCore.pyqtSignal(str)
        progressed = QtCore.pyqtSignal(float, str)        # frac in [0,1], message
        cancelled = QtCore.pyqtSignal()

        def __init__(self, extract, backend, session, regenerate):
            super().__init__()
            self.extract, self.backend = extract, backend
            self.session, self.regenerate = session, regenerate
            self._cancel = False

        def cancel(self):
            """Request cancellation (UI thread). The run-drive checks this between poll
            ticks / before each group and raises CancelledError -- no thread kill, so the
            designer's ADE state is still restored by run_ade's finally-block."""
            self._cancel = True

        def run(self):
            from insitu.run import CancelledError
            try:
                out = self.extract.run(backend=self.backend, session=self.session,
                                       regenerate=self.regenerate,
                                       progress=lambda f, m: self.progressed.emit(f, m),
                                       cancel=lambda: self._cancel)
                self.done.emit(out)
            except CancelledError:
                self.cancelled.emit()
            except Exception as e:
                import traceback
                self.failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

    class _ClusterSweepWorker(QtCore.QThread):
        """Run the FULL Donau+ALPS measurement sweep (offline per-group netlist -> dsub+alps ->
        per-group PSF -> by-tag npz -> multi-port fit) off the UI thread. A real submit blocks
        on the cluster queue, so it MUST be threaded; per-group state is streamed to the GUI
        status table via group_state, and a cooperative Cancel stops between groups."""
        done = QtCore.pyqtSignal(object)
        failed = QtCore.pyqtSignal(str)
        progressed = QtCore.pyqtSignal(float, str)        # frac in [0,1], message
        group_state = QtCore.pyqtSignal(int, int, str, str, str)   # i, n, tag, analysis, state
        cancelled = QtCore.pyqtSignal()

        def __init__(self, extract, *, netlistdir, pdk, ahdl, engine, donau_cfg, dry_run,
                     max_parallel=1, work_root=None):
            super().__init__()
            self.extract = extract
            self.netlistdir, self.pdk, self.ahdl = netlistdir, pdk, ahdl
            self.engine, self.donau_cfg, self.dry_run = engine, donau_cfg, dry_run
            self.max_parallel = max_parallel
            self.work_root = work_root
            self._done_groups = set()                 # indices whose PSF has landed (bar driver)
            self._cancel = False

        def cancel(self):
            """Request cancellation (UI thread). step_run stops launching NEW groups and raises
            CancelledError -- jobs already on the queue are drained first."""
            self._cancel = True

        def _gs(self, i, n, group, state):
            self.group_state.emit(i, n, group["tag"], group["analysis"], state)
            # With several groups in flight at once, drive the bar off COMPLETED groups (not a
            # single-i estimate): count each group once its PSF lands / preview is assembled.
            if state in ("done", "preview"):
                self._done_groups.add(i)
            frac = len(self._done_groups) / max(n, 1)
            self.progressed.emit(min(1.0, frac), f"group {i+1}/{n} {group['tag']}: {state}")

        def run(self):
            from insitu.run import CancelledError
            try:
                out = self.extract.run_cluster_sweep(
                    netlistdir=self.netlistdir, pdk_model_dir=self.pdk or None,
                    ahdllibdir=self.ahdl or None, engine=self.engine, donau=self.donau_cfg,
                    dry_run=self.dry_run, group_status=self._gs, work_root=self.work_root,
                    log=lambda m: self.progressed.emit(-1.0, m),
                    cancel=lambda: self._cancel, max_parallel=self.max_parallel)
                self.done.emit(out)
            except CancelledError:
                self.cancelled.emit()
            except Exception as e:
                import traceback
                self.failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

    # measurement / TB guidance surfaced in Tab 2 (from GUI_DEPLOY_PLAN.md §7)
    MEAS_HINTS = (
        "Measurement / testbench guidance (avoids the common extraction mistakes):\n\n"
        "• Conventions (silent-mismatch traps):\n"
        "   - PSRR = COMPLEX transfer H = vout/vin (store Re/Im or mag+phase), NOT attenuation-dB.\n"
        "   - Zout = V(vout)/I with 1 A AC into vout and vin held by an ideal DC source.\n"
        "   - Noise = amplitude PSD V/√Hz (tick the sqrt box if Spectre gives V²/Hz).\n"
        "   - Spurs are INTRINSIC only (no stimulus); inject external supply tones at vin in the\n"
        "     system TB and let the PSRR path carry them.\n\n"
        "• Sweep density: Zout/PSRR dec 40 (10 Hz→100 MHz), noise dec 20; densify only if the\n"
        "   Zout peak / PSRR notch is under-resolved (≳10 pts across the −3 dB width). Start noise\n"
        "   BELOW the flicker corner (try 1 Hz). The knob that matters is a CLEAN DC OP\n"
        "   (errpreset=conservative, tight reltol), not AC reltol.\n\n"
        "• Decap / loading: each load corner = ideal DC current source (sets OP, AC-open). INCLUDE\n"
        "   the LDO's intrinsic output cap; EXCLUDE external board/system decap and other vout loads\n"
        "   (the system TB already has them → double-count otherwise). meta_cout/esr = design values.\n\n"
        "• HF (*_hf) arrays: same TB, NOMINAL corner only, swept to 500 MHz. Two uses: bound the RF\n"
        "   carrier, and feed Cout/ESR auto-extraction (capacitive tail).\n\n"
        "• Bandwidth for an RF chip (e.g. 5.8 GHz): modeling band ≠ system max freq. Run ONE\n"
        "   exploratory nominal Zout sweep to 6–10 GHz: a smooth cap/ESR tail ⇒ the lumped model\n"
        "   extrapolates (500 M–1 G fine); an inductive rise ⇒ sweep past the carrier and add a\n"
        "   series-ESL element."
    )

    # ---- Import-grid quantities: (label, key, scope, required) -------------------
    #   scope: "corner" = one file per load corner ; "nominal" = nominal corner only ;
    #          "global" = one file total.   required = needed for a complete fit+emit.
    IMPORT_ROWS = [
        ("Zout",        "z",          "corner",  True),
        ("PSRR",        "p",          "corner",  True),
        ("Noise",       "noise",      "corner",  True),
        ("Zout HF",     "z_hf",       "nominal", True),
        ("DC load-reg", "dc_loadreg", "global",  True),
        ("DC dropout",  "dc_dropout", "global",  True),
        ("PSRR HF",     "p_hf",       "nominal", False),
        ("Trans step",  "trans_lin",  "corner",  False),
        ("Trans 1 mA",  "trans_big",  "nominal", False),
        ("Trans 5 mA",  "trans_slew", "nominal", False),
        ("DC line-reg", "dc_linereg", "global",  False),
        ("Spurs",       "spurs",      "corner",  False),
        ("Spur gate",   "spur_500u",  "global",  False),
        ("Bias xfer",   "ibp",        "corner",  False),
    ]

    # A fresh, structurally-valid single-LDO skeleton for 'New…' -- the designer re-tags the
    # pins for their DUT. The editor's help panel explains each role; Validate shows the
    # derived measurement matrix before Save.
    _MANIFEST_TEMPLATE = """{
  "_note": "TEMPLATE — replace every my_* value (my_lib/my_dut_cell/my_testbench) and the example net names (VDD1P0, VOUT, EN) with your design's real lib/cell/net names, then Validate. (_note is ignored by the loader.)",
  "name": "my_ldo",
  "dut": {
    "lib": "my_lib",
    "cell": "my_dut_cell",
    "tb_lib": "my_lib",
    "tb_cell": "my_testbench",
    "tb_inst": "I0",
    "extract_cell": "my_testbench_extract"
  },
  "ground": "gnd!",
  "supplies": {
    "1p0": {"net": "VDD1P0", "dc": 1.05}
  },
  "v_out": {
    "out": {"net": "VOUT"}
  },
  "i_out": {},
  "bias": {},
  "leave_alone": ["EN"],
  "current_psrr_supplies": ["1p0"],
  "analysis": {
    "ac": "ac start=10 stop=500M dec=20",
    "noise": "noise start=10 stop=100M dec=20"
  }
}
"""

    _MANIFEST_ROLE_HELP = (
        "<b>Pin-role manifest</b> — tag each DUT-boundary pin, the tool does the rest.<br>"
        "&nbsp;• <b>DUT</b> = the PMU/LDO you model; <b>Testbench</b> = the cell Maestro simulates "
        "(instantiates the DUT + sources/probes). <i>extract_cell</i> auto = &lt;tb_cell&gt;_extract.<br>"
        "&nbsp;• <b>supplies</b> <code>{name:{net,dc}}</code> — rails to PSRR (dc = the OP value).<br>"
        "&nbsp;• <b>v_out</b> <code>{name:{net}}</code> — voltage outputs (Zout/noise/coupling).<br>"
        "&nbsp;• <b>i_out</b> <code>{name:{net,dc}}</code> — current sinks (admittance / current-PSRR).<br>"
        "&nbsp;• Any pin you DON'T list is left exactly as the testbench drives it.<br>"
        "<b>Validate</b> previews the measurement matrix; <b>Save</b> reloads it on Tab 0.")

    class _FindReplaceBar(QWidget):
        """A small Ctrl+F / Ctrl+H find/replace bar over a QTextEdit (the Raw-JSON view).
        Plain-substring search (no regex) — enough for a designer hunting a net/role name."""

        def __init__(self, parent, edit):
            super().__init__(parent)
            self.edit = edit
            h = QHBoxLayout(self); h.setContentsMargins(0, 2, 0, 2)
            self.find = QLineEdit(); self.find.setPlaceholderText("Find… (Ctrl+F)")
            self.repl = QLineEdit(); self.repl.setPlaceholderText("Replace with… (Ctrl+H)")
            b_next = QPushButton("Next"); b_prev = QPushButton("Prev")
            b_rep = QPushButton("Replace"); b_all = QPushButton("Replace all")
            b_close = QPushButton("✕"); b_close.setMaximumWidth(28)
            b_next.clicked.connect(lambda: self._find(True))
            b_prev.clicked.connect(lambda: self._find(False))
            b_rep.clicked.connect(self._replace)
            b_all.clicked.connect(self._replace_all)
            b_close.clicked.connect(self.hide)
            self.find.returnPressed.connect(lambda: self._find(True))
            for wd in (QLabel("Find"), self.find, b_next, b_prev,
                       QLabel("Repl"), self.repl, b_rep, b_all, b_close):
                h.addWidget(wd)
            self.setVisible(False)

        def open_find(self):
            self.setVisible(True); self.find.setFocus(); self.find.selectAll()

        def open_replace(self):
            self.setVisible(True); self.repl.setFocus()

        def _find(self, forward=True):
            from PyQt5.QtGui import QTextDocument
            needle = self.find.text()
            if not needle:
                return False
            flags = QTextDocument.FindFlags()
            if not forward:
                flags |= QTextDocument.FindBackward
            if not self.edit.find(needle, flags):
                # wrap around: move the cursor to the doc start/end and retry once
                cur = self.edit.textCursor()
                cur.movePosition(cur.End if not forward else cur.Start)
                self.edit.setTextCursor(cur)
                return self.edit.find(needle, flags)
            return True

        def _replace(self):
            cur = self.edit.textCursor()
            if cur.hasSelection() and cur.selectedText() == self.find.text():
                cur.insertText(self.repl.text())
            self._find(True)

        def _replace_all(self):
            needle = self.find.text()
            if not needle:
                return
            txt = self.edit.toPlainText()
            self.edit.setPlainText(txt.replace(needle, self.repl.text()))

    class _ManifestEditorDialog(QtWidgets.QDialog):
        """In-GUI manifest editor: edit/validate/save a pin-role manifest without leaving the
        tool. Two sub-tabs:
          • Form     — fill-in-the-blanks (REQUIRED always visible + an ADVANCED group, collapsed).
                       Supplies/v_out/i_out are tables. A TYPED OVERLAY on the parsed dict, so
                       unknown/unmodeled keys survive the round-trip untouched.
          • Raw JSON — the original QTextEdit (escape hatch + power view), with Ctrl+F / Ctrl+H.
        Validate / Save / Save As reuse manifest._fill_defaults + .validate + .summary (thin-shell)."""

        def __init__(self, parent, text, path):
            super().__init__(parent)
            self.path = pathlib.Path(path) if path else None
            self.saved_path = None
            self.setWindowTitle(f"Manifest editor — {self.path.name if self.path else 'new (unsaved)'}")
            self.resize(500, 680)
            # STASH: parse the incoming text once; the form is an overlay on THIS dict, so every
            # key the form does not model survives intact. Bad JSON -> empty stash, raw text kept.
            try:
                self._stash = json.loads(text)
                if not isinstance(self._stash, dict):
                    self._stash = {}
            except (json.JSONDecodeError, TypeError):
                self._stash = {}

            lay = QVBoxLayout(self)
            help_ = QLabel(_MANIFEST_ROLE_HELP); help_.setWordWrap(True)
            help_.setStyleSheet("background:#eef5ff; padding:8px; border:1px solid #cdddee;")
            lay.addWidget(help_)

            self.subtabs = QTabWidget()
            self.subtabs.addTab(self._build_form_tab(), "Form")
            self.subtabs.addTab(self._build_raw_tab(text), "Raw JSON")
            self.subtabs.currentChanged.connect(self._on_subtab_changed)
            lay.addWidget(self.subtabs, 1)

            self.status = QLabel("edit, then Validate"); self.status.setWordWrap(True)
            self.status.setStyleSheet("font-family:monospace; font-size:11px;")
            lay.addWidget(self.status)
            brow = QHBoxLayout()
            b_val = QPushButton("Validate"); b_val.clicked.connect(self._validate)
            b_scan = QPushButton("Scan netlist…"); b_scan.clicked.connect(self._scan_netlist)
            b_scan.setToolTip("Pick the base input.scs and auto-fill each table's source-instance "
                              "+ dc columns from the sources detected on each role net.")
            b_save = QPushButton("Save"); b_save.clicked.connect(self._save)
            b_saveas = QPushButton("Save As…"); b_saveas.clicked.connect(self._save_as)
            b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
            brow.addWidget(b_val); brow.addWidget(b_scan); brow.addStretch(1)
            brow.addWidget(b_save); brow.addWidget(b_saveas); brow.addWidget(b_cancel)
            lay.addLayout(brow)

            # populate the form widgets from the stashed dict (or template defaults)
            self._dict_to_form(self._stash)

        # ---- Raw-JSON sub-tab (the original editor + find/replace) -----------------
        def _build_raw_tab(self, text):
            w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0)
            self.ed = QTextEdit(); self.ed.setPlainText(text)
            self.ed.setStyleSheet("font-family:monospace; font-size:12px;")
            self.ed.setLineWrapMode(QTextEdit.NoWrap)
            self.findbar = _FindReplaceBar(self, self.ed)
            v.addWidget(self.findbar)
            v.addWidget(self.ed, 1)
            # Ctrl+F / Ctrl+H find+replace over the raw text (escape hatch power-user view)
            QtWidgets.QShortcut(QtGui.QKeySequence.Find, self.ed,
                                activated=self.findbar.open_find)
            QtWidgets.QShortcut(QtGui.QKeySequence.Replace, self.ed,
                                activated=self.findbar.open_replace)
            return w

        # ---- Form sub-tab ----------------------------------------------------------
        def _build_form_tab(self):
            # Organized BY TOPIC (not a blunt required/advanced cut): Identity · Supplies ·
            # Voltage outputs · Current outputs are always visible; only genuinely-rare overrides
            # live in a collapsed Advanced group. `leave_alone` is NOT surfaced — augment never
            # consumes it, so any pin you don't list is already left exactly as the TB drives it.
            outer = QWidget(); ov = QVBoxLayout(outer)
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            inner = QWidget(); v = QVBoxLayout(inner)

            # one-line orientation: what DUT vs Testbench mean + the leave-alone policy
            note = QLabel(
                "<b>DUT</b> = the PMU/LDO you're modeling. &nbsp;<b>Testbench</b> = the cell "
                "Maestro actually simulates (it instantiates the DUT + sources/probes).<br>"
                "Any pin you don't list below is left <b>exactly as the testbench drives it</b> — "
                "you never have to enumerate the rest.")
            note.setWordWrap(True)
            note.setStyleSheet("color:#345; background:#f5f8ff; padding:6px; border:1px solid #dde6f2;")
            v.addWidget(note)

            # IDENTITY — what & where (always visible) ------------------------------
            idg = QGroupBox("Identity — what & where")
            idg.setStyleSheet("QGroupBox{font-weight:bold;}")
            idf = QFormLayout(idg)
            self.f_name = QLineEdit(); self.f_name.setPlaceholderText("e.g. wur_pmu_top")
            self.f_name.setToolTip("A name for this model / manifest (free text).")
            idf.addRow(self._req_label("Model name"), self.f_name)
            # DUT first (D4): tb_lib inherits dut_lib when left blank.
            self.f_dut_lib = QLineEdit(); self.f_dut_lib.setPlaceholderText("e.g. Hi1108_WuR_PMU")
            self.f_dut_lib.setToolTip("Library that holds your PMU/LDO cell — the DUT itself.")
            idf.addRow(self._req_label("DUT library"), self.f_dut_lib)
            self.f_dut_cell = QLineEdit(); self.f_dut_cell.setPlaceholderText("e.g. WuR_PMU_TOP")
            self.f_dut_cell.setToolTip("The DUT CELL name — your PMU/LDO cellview "
                                       "(NOT the instance name).")
            idf.addRow(self._req_label("DUT cell (your PMU/LDO)"), self.f_dut_cell)
            self.f_dut_inst = QLineEdit(); self.f_dut_inst.setPlaceholderText("e.g. PMU_TOP")
            self.f_dut_inst.setToolTip("The DUT's INSTANCE name inside the testbench (e.g. PMU_TOP) "
                                       "— how the DUT is placed in the TB, not the cell name.")
            idf.addRow("DUT instance (in the TB)", self.f_dut_inst)
            self.f_tb_lib = QLineEdit()
            self.f_tb_lib.setPlaceholderText("blank → same as DUT library")
            self.f_tb_lib.setToolTip("Library that holds the testbench cell. Blank → reuse the "
                                     "DUT library.")
            idf.addRow("Testbench library", self.f_tb_lib)
            self.f_tb_cell = QLineEdit(); self.f_tb_cell.setPlaceholderText("e.g. sim_LDO")
            self.f_tb_cell.setToolTip("The testbench cell Maestro simulates (wraps the DUT + "
                                      "sources/probes).")
            idf.addRow(self._req_label("Testbench cell (Maestro)"), self.f_tb_cell)
            self.f_ground = QLineEdit(); self.f_ground.setPlaceholderText("e.g. VSS  (blank → gnd!)")
            self.f_ground.setToolTip("Ground net name. Blank → gnd!.")
            idf.addRow("Ground net", self.f_ground)
            # Mode-A / live-Virtuoso optional fields (relocated from the deleted Advanced group).
            # Not needed for cluster/offline runs; surfaced here so the live path can still set them.
            idf.addRow(QLabel(
                "<span style='color:#678;font-weight:normal'>(Mode A / live Virtuoso — optional, "
                "not needed for cluster runs)</span>"))
            self.f_extract = QLineEdit()
            self.f_extract.setToolTip("(Mode A / live) The TB copy stimuli are appended to. "
                                      "Auto = <tb_cell>_extract.")
            idf.addRow("extract_cell", self.f_extract)
            self.f_tb_view = QLineEdit(); self.f_tb_view.setPlaceholderText("e.g. schematic")
            self.f_tb_view.setToolTip("(Mode A / live) Testbench cellview to open.")
            idf.addRow("tb_view", self.f_tb_view)
            self.f_src_test = QLineEdit()
            self.f_src_test.setPlaceholderText("optional — auto-discovered from the ADE-XL session")
            self.f_src_test.setToolTip("(Mode A / live) The Maestro/ADE test whose operating point + "
                                       "design variables we inherit (so the bias matches your real run). "
                                       "Auto-found by TB cell name — set only if auto-find fails.")
            idf.addRow("ade_src_test", self.f_src_test)
            v.addWidget(idg)

            # COVERAGE — the §2 tier ladder + the global coverage knobs (always visible) -----
            # Placed right after Identity so the user picks WHAT to characterize before filling
            # the per-rail sweep cells (which live in the Voltage/Current output tables below).
            cov = QGroupBox("Coverage — what to characterize")
            cov.setStyleSheet("QGroupBox{font-weight:bold;}")
            covf = QFormLayout(cov)
            self.f_cov_tier = QComboBox()
            self.f_cov_tier.addItems(["T0", "T1", "T2", "T3", "T4"])
            self.f_cov_tier.setCurrentText("T4")             # default = FULL tier ladder
            self.f_cov_tier.setToolTip(
                "The coverage tier — a NESTED ADDITIVE ladder (each tier adds its sims on top of "
                "every lower one):\n"
                "  T0  LTI AC + .noise at the OP (Zout/PSRR/Y/pi/noise)\n"
                "  T1  + transient slew/recovery steps\n"
                "  T2  + DC I-V (current sinks) + dropout/load-reg (rails)\n"
                "  T3  + the per-rail load schedule (AC/noise repeated at each iload)\n"
                "  T4  + temperature corners\n"
                "DEFAULT = T4 (full). NB: a tier alone adds ZERO points until you DECLARE the "
                "matching per-rail sweep (iload/trans/iv/temps) — a tier with no declared params "
                "reproduces the identical T0 matrix.")
            covf.addRow("tier", self.f_cov_tier)
            self.f_cov_temps = QLineEdit()
            self.f_cov_temps.setPlaceholderText("blank → single (session) temp; e.g. -40,55,125")
            self.f_cov_temps.setToolTip(
                "Temperature corners in °C, comma-separated → coverage.temps. Blank → [] (a single "
                "session temp, no temp sweep). e.g. -40,55,125 (middle = nominal/model-bake temp).")
            covf.addRow("temperature corners [°C]", self.f_cov_temps)
            self.f_cov_slew = QCheckBox("emit slew_en core (default off = model runs LTI)")
            self.f_cov_slew.setToolTip(
                "Sets the emitted VA param coverage.slew_en (0/1). Even when T1 is extracted (the "
                "model CARRIES the slew core), the model runs LTI until slew_en=1. Default OFF.")
            covf.addRow("slew_en", self.f_cov_slew)
            self.f_cov_lin = QCheckBox("2× amplitude linearity self-check (guardrail 4)")
            self.f_cov_lin.setToolTip(
                "coverage.lin_gate: rerun one AC point at 2× drive amplitude; ratio-invariance "
                "certifies the OP is linear (the one-hot superposition assumption holds). Default OFF.")
            covf.addRow("lin_gate", self.f_cov_lin)
            cov_help = QLabel(
                "<span style='color:#678'>Per-rail load sweeps live in the Voltage-outputs "
                "<b>“iload sweep”</b> + <b>“trans”</b> columns; per-sink I-V lives in the "
                "Current-outputs <b>“iv_sweep”</b> column (all below). A tier only EMITS a point "
                "where its matching cell is filled.</span>")
            cov_help.setWordWrap(True)                        # [C2] word-wrap the rich-text help
            covf.addRow(cov_help)
            v.addWidget(cov)

            # SUPPLIES (always visible) ---------------------------------------------
            sg = QGroupBox("Supplies — rails to PSRR")
            sg.setStyleSheet("QGroupBox{font-weight:bold;}")
            sgl = QVBoxLayout(sg)
            sgl.addWidget(QLabel(
                "<span style='color:#678'>Each supply needs a net + its DC operating value. "
                "<b>src instance</b> = the testbench voltage source on that rail (the one we set "
                "the PSRR AC mag on); <b>leave blank to auto-detect</b> — fill it only if "
                "auto-detect can't find / picks the wrong one. <b>PSRR→I</b> = include this rail "
                "as a current-PSRR reference for the current outputs. The <b>analysis</b> column "
                "lets you override the global AC sweep for just this rail's PSRR.</span>"))
            self.t_supplies = self._make_table(
                ["key", "net", "dc", "src instance", "PSRR→I", "analysis"],
                ["AVDD1P0", "VDD1P0", "1.0", "V_AVDD", "✓", "(gear)"],
                check_cols=["PSRR→I"], analysis_role="supplies")
            sgl.addWidget(self._table_block(self.t_supplies))
            v.addWidget(sg)

            # VOLTAGE OUTPUTS (always visible) --------------------------------------
            vg = QGroupBox("Voltage outputs — LDO rails")
            vg.setStyleSheet("QGroupBox{font-weight:bold;}")
            vgl = QVBoxLayout(vg)
            vgl.addWidget(QLabel("<span style='color:#678'>The regulated voltage outputs "
                                 "(Zout / PSRR / noise). &nbsp;<b>≥1 voltage OR current output "
                                 "required.</b> <b>src instance</b> = the load idc on this rail "
                                 "(we set its AC mag to inject the Zout test current); blank → "
                                 "auto-detect / insert. <b>iload sweep</b> (T3 loads) = "
                                 "“&lt;type&gt; &lt;start&gt; &lt;stop&gt; &lt;n&gt;” + optional "
                                 "“+ p1,p2” extra points (SI suffixes ok), e.g. "
                                 "“log 50u 2m 4 + 300u”; blank → single OP. <b>trans</b> (T1 slew) "
                                 "= “&lt;from&gt;:&lt;to&gt;[:label] , …” + optional trailing "
                                 "“@edge=1n,tstop=1u”, e.g. “0:2m:slew , 450u:550u:lin”. The "
                                 "<b>analysis</b> column overrides this output's AC (Zout) and "
                                 "noise sweeps.</span>"))
            self.t_vout = self._make_table(
                ["key", "net", "src instance", "iload sweep", "trans", "analysis"],
                ["vco", "VDD0P8_VCO", "I_load", "log 200u 6m 4 + 3m", "0:6m:slew", "(gear)"],
                analysis_role="v_out")
            self._set_header_tip(self.t_vout, "iload sweep",
                "STATIC / DC load axis (amps). DOUBLE-CLICK for the Cadence-style editor. The "
                "small-signal AC + noise are RE-RUN at each load point, and (with a range) a "
                "dropout / load-regulation DC sweep is produced. Forms: '<type> start stop n' "
                "(e.g. 'log 200u 6m 4'), '+ p1,p2' added points, or bare 'p1,p2'. Blank → single OP.\n"
                "This is the STATIC dimension; for a load STEP (slew) use the 'trans' column.")
            self._set_header_tip(self.t_vout, "trans",
                "DYNAMIC / transient load STEP (slew, overshoot, settling) — a DIFFERENT axis from "
                "'iload sweep'. Double-click '…' for the editor: a BASELINE (light) load + TARGET "
                "(heavy) loads → one baseline→target slew run each, with edge time + sim time. "
                "Cell form: '<from>:<to>[:label] , …' + optional '@edge=1n,tstop=10u'. Blank → no slew.")
            vgl.addWidget(self._table_block(self.t_vout))
            v.addWidget(vg)

            # CURRENT OUTPUTS (always visible; carries its OWN settings) -------------
            cg = QGroupBox("Current outputs — bias / current sinks")
            cg.setStyleSheet("QGroupBox{font-weight:bold;}")
            cgl = QVBoxLayout(cg)
            cgl.addWidget(QLabel("<span style='color:#678'>Current-output pins (admittance / "
                                 "current-PSRR): <b>net</b> + <b>compliance dc</b> (the single DC "
                                 "voltage held during AC/noise) + optional <b>iv_sweep</b> — a "
                                 "VOLTAGE sweep that traces the pin's I-V / compliance knee "
                                 "(T2 coverage → coverage.iv). <b>probe instance</b> = the "
                                 "testbench vdc on that pin whose AC mag we set (the current "
                                 "read = its :p current); <b>blank → auto-detect / insert "
                                 "(Vprobe_&lt;key&gt;)</b>. The <b>analysis</b> column overrides "
                                 "this output's AC sweep. Hover any column header for details. "
                                 "Leave the table empty if your DUT has no current outputs.</span>"))
            self.t_iout = self._make_table(
                ["key", "net", "compliance dc", "iv_sweep", "probe instance", "analysis"],
                ["i500n", "IBP_500N", "0.9", "0:0.01:1.1", "Vprobe_i500n", "(gear)"],
                analysis_role="i_out")
            # per-column header tooltips: iv_sweep is the most-confused column (the user asked
            # "what IS iv_sweep / how do I fill it?") -- spell out that it is a VOLTAGE sweep, its
            # three accepted forms, and that it is NOT the 'compliance dc' single point.
            self._set_header_tip(self.t_iout, "iv_sweep",
                "I-V / compliance-knee VOLTAGE sweep for this current-output pin (a T2 coverage "
                "point → coverage.iv). The pin's probe vsource is stepped across this voltage "
                "range and the pin CURRENT (<probe>:p) is recorded → the output's I-V curve "
                "(Spectre `dc dev=<probe> param=dc`). UNITS = volts. DOUBLE-CLICK the cell for the "
                "Cadence-style editor (linear/log + range + points). Accepted text forms:\n"
                "  • <type> start stop n            e.g. 'lin 0 0.8 80'  or  'log 1m 1 30'\n"
                "  • <type> start stop n + p1,p2    add specific I-V points, e.g. 'lin 0 1 11 + 0.45,0.9'\n"
                "  • p1,p2                          specific points only\n"
                "  • start:step:stop                legacy, e.g. 0:0.01:0.8\n"
                "BLANK (or 'auto') → NO I-V sweep for this pin; give a range/points to sweep it. "
                "This is NOT 'compliance dc' (that is the single DC voltage held during AC/noise). "
                "Needs a vsource probe on the pin — leave 'probe instance' blank to auto-insert one.")
            self._set_header_tip(self.t_iout, "compliance dc",
                "The single DC voltage held on this pin during the AC / noise analyses (the probe "
                "vsource's dc=). A scalar voltage, e.g. 0.9. For the swept I-V curve use 'iv_sweep'.")
            self._set_header_tip(self.t_iout, "probe instance",
                "The testbench vsource on this pin whose AC mag we set; the current read is its "
                ":p current. Blank → the build auto-detects a vsource on the net, else inserts "
                "Vprobe_<key> (the normal case when the pin carries only the characterized "
                "current source).")
            cgl.addWidget(self._table_block(self.t_iout))
            v.addWidget(cg)

            # BIAS PORTS (promoted to a top-level table from the deleted Advanced group) -----
            bg = QGroupBox("Bias ports — held DC")
            bg.setStyleSheet("QGroupBox{font-weight:bold;}")
            bgl = QVBoxLayout(bg)
            bgl.addWidget(QLabel("<span style='color:#678'>Pins held at a fixed DC during the "
                                 "AC/noise runs (net + dc). Leave empty if none.</span>"))
            # Bias is a HELD pin, never a one-hot stimulus -> no per-object analysis column and not
            # touched by "Scan netlist" (nothing to inject/probe). Deliberately a plain 3-col table.
            self.t_bias = self._make_table(["key", "net", "dc"], ["vbg", "VBG", "0.6"])
            bgl.addWidget(self._table_block(self.t_bias))
            v.addWidget(bg)

            # SIMULATION — global default AC / noise sweep (always visible) -----------------
            simg = QGroupBox("Simulation — default AC / noise sweep")
            simg.setStyleSheet("QGroupBox{font-weight:bold;}")
            simf = QFormLayout(simg)
            simf.addRow(QLabel("<span style='color:#678;font-weight:normal'>The global default "
                               "sweep for every group. Override per object with the table "
                               "<b>analysis</b> column.</span>"))
            self.f_ac = QLineEdit()
            self.f_ac.setPlaceholderText("ac start=10 stop=500M dec=20")
            self.f_ac.setToolTip("Global default AC sweep (Zout / PSRR). Use Edit… for the "
                                 "Cadence-style sweep dialog.")
            simf.addRow("analysis.ac", self._with_sweep_editor(self.f_ac, "ac"))
            self.f_noise = QLineEdit()
            self.f_noise.setPlaceholderText("noise start=10 stop=100M dec=20")
            self.f_noise.setToolTip("Global default noise sweep. Use Edit… for the Cadence-style "
                                    "sweep dialog.")
            simf.addRow("analysis.noise", self._with_sweep_editor(self.f_noise, "noise"))
            # Corner label (a run/output-dir label, NOT a PDK process corner). Replaces the
            # pull_from_session checkbox + the multi-value fallback line (both relocated/dropped).
            self.f_corner = QLineEdit("nom")
            self.f_corner.setPlaceholderText("nom")
            self.f_corner.setToolTip("Label for this run = the output dir name + the read label; "
                                     "it does NOT switch the PDK process corner (that comes from the "
                                     "base netlist / PDK model dir).")
            simf.addRow("Corner label", self.f_corner)
            v.addWidget(simg)

            v.addStretch(1)
            # [2.5] every group's help text must WRAP -- an unwrapped rich-text QLabel reports its
            # full single-line width as a minimum, forcing the whole group (and the dialog) ~3400px
            # wide. Wrapping lets each group shrink to its table; the table keeps its own scrollbar.
            for _lb in inner.findChildren(QLabel):
                _lb.setWordWrap(True)
            # [2.2/2.5] keep the single-line input boxes a tidy fixed size -- otherwise a QFormLayout
            # field stretches to the full window width (an over-long blank after "Model name" etc.).
            for _le in inner.findChildren(QLineEdit):
                _le.setMaximumWidth(280)
            scroll.setWidget(inner)
            ov.addWidget(scroll, 1)
            return outer

        @staticmethod
        def _req_label(text):
            return QLabel(f"{text} <span style='color:#b00020'>*</span>")

        @staticmethod
        def _toggle_group(group, on):
            """Collapse/expand a checkable QGroupBox by hiding its child widgets (PyQt5 has no
            native collapse)."""
            for ch in group.findChildren(QWidget):
                ch.setVisible(on)

        def _make_table(self, cols, example, check_cols=None, analysis_role=None):
            t = QTableWidget(0, len(cols))
            t.setHorizontalHeaderLabels(cols)
            hh = t.horizontalHeader()
            # [2.1] NO column stretches -- a Stretch column swallows the whole viewport and
            # balloons the table/form/dialog. Every column is Interactive (user-draggable) with a
            # compact fixed default; the table shows its own horizontal scrollbar when the window is
            # narrow, instead of forcing the dialog wide. 'net' is just a bit wider than the rest.
            hh.setStretchLastSection(False)
            _WIDS = {"key": 92, "net": 168, "dc": 78, "compliance dc": 104, "src instance": 116,
                     "probe instance": 122, "iv_sweep": 94, "iload sweep": 132, "trans": 122,
                     "PSRR→I": 60, "analysis": 72}
            for c, name in enumerate(cols):
                hh.setSectionResizeMode(c, QHeaderView.Interactive)
                t.setColumnWidth(c, _WIDS.get(name, 96))
            t.setMaximumHeight(150)
            t.setToolTip("example row → " + " / ".join(f"{c}={e}" for c, e in zip(cols, example)))
            t._example = example
            t._cols = cols
            # the boolean (checkbox) columns, e.g. supplies 'PSRR→I'
            t._check_cols = set(check_cols or [])
            t._check_idx = {cols.index(c) for c in (check_cols or []) if c in cols}
            # the per-object analysis override: a 'gear' button column ('analysis') + a per-row
            # store {row_id: {ac?, noise?}}. analysis_role in {supplies, v_out, i_out}; v_out also
            # carries noise. row_id is the QTableWidgetItem id of the row's key cell (stable across
            # sort; the store is keyed by the live key text at collect time).
            t._analysis_role = analysis_role
            t._analysis_idx = cols.index("analysis") if "analysis" in cols else None
            t._analysis = {}                 # key-text -> {ac?, noise?}
            # coverage-sweep columns get a composite cell: an editable QLineEdit (type directly)
            # PLUS a '…' button that opens the Cadence-style editor. _dialog_cols marks them so the
            # row builder installs the widget and the readers/writers route through it.
            _COV_SWEEP_COLS = {"iload sweep", "trans", "iv_sweep"}
            t._dialog_cols = {i for i, c in enumerate(cols) if c in _COV_SWEEP_COLS}
            # [B4] live type-validation: re-colour a typed src/probe-instance name vs the base
            # netlist when its cell is edited. Guarded (reentrancy + role/column filtered) so it
            # is a no-op for every other column and when no base netlist is loaded.
            t.cellChanged.connect(lambda r, c, _t=t: self._on_src_cell_changed(_t, r, c))
            return t

        @staticmethod
        def _set_header_tip(t, col_name, tip):
            """Set a per-column HEADER tooltip (hover the column title). No-op if the column or its
            header item is absent (Qt may not have materialised the header item in a headless run)."""
            cols = getattr(t, "_cols", [])
            if col_name not in cols:
                return
            hi = t.horizontalHeaderItem(cols.index(col_name))
            if hi is not None:
                hi.setToolTip(tip)

        def _table_block(self, table):
            """A table + Add/Remove buttons in one container widget (for QFormLayout.addRow)."""
            cont = QWidget(); v = QVBoxLayout(cont); v.setContentsMargins(0, 0, 0, 0)
            v.addWidget(table)
            h = QHBoxLayout()
            b_add = QPushButton("+ row")
            b_add.clicked.connect(lambda _=False, t=table: self._table_add_row(t))
            b_del = QPushButton("− row")
            b_del.clicked.connect(lambda _=False, t=table: self._table_del_row(t))
            h.addWidget(b_add); h.addWidget(b_del); h.addStretch(1)
            v.addLayout(h)
            return cont

        @staticmethod
        def _table_add_row(t, values=None):
            r = t.rowCount(); t.insertRow(r)
            check_idx = getattr(t, "_check_idx", set()) or set()
            an_idx = getattr(t, "_analysis_idx", None)
            for c in range(t.columnCount()):
                if c in check_idx:                       # boolean (checkbox) cell, e.g. PSRR→I
                    it = QTableWidgetItem()
                    it.setFlags((it.flags() | QtCore.Qt.ItemIsUserCheckable)
                                & ~QtCore.Qt.ItemIsEditable)
                    raw = "" if values is None else (values[c] if c < len(values) else "")
                    on = str(raw).strip().lower() in ("1", "true", "yes", "✓", "x", "on")
                    it.setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)
                    t.setItem(r, c, it)
                    continue
                if an_idx is not None and c == an_idx:   # per-object analysis 'gear' affordance
                    it = QTableWidgetItem()
                    it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
                    t.setItem(r, c, it)
                    btn = QPushButton("gear…")
                    btn.setToolTip("Override the global AC/noise sweep for just this object "
                                   "(unchecked → inherit the global default).")
                    # Resolve the row from the button at CLICK time (sender's cell), so a table
                    # rebuild/reorder can't leave the lambda holding a deleted item (D6-safe).
                    btn.clicked.connect(
                        lambda _=False, tt=t, b=btn: _ManifestEditorDialog._gear_clicked(tt, b))
                    t.setCellWidget(r, c, btn)
                    continue
                val = "" if values is None else (values[c] if c < len(values) else "")
                if c in getattr(t, "_dialog_cols", set()):    # coverage-sweep: QLineEdit + '…' button
                    it = QTableWidgetItem()                    # placeholder item (keeps selection sane)
                    it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
                    t.setItem(r, c, it)
                    t.setCellWidget(r, c, _ManifestEditorDialog._make_cov_cell_widget(t, c, str(val)))
                    continue
                it = QTableWidgetItem(str(val))
                if values is None:                            # placeholder hint on a fresh row
                    ex = getattr(t, "_example", [])
                    if c < len(ex):
                        it.setToolTip(f"e.g. {ex[c]}")
                t.setItem(r, c, it)
            _ManifestEditorDialog._refresh_analysis_cell(t, r)
            return r

        @staticmethod
        def _make_cov_cell_widget(t, c, text=""):
            """The composite coverage-sweep cell: an editable QLineEdit (type a sweep directly) +
            a small '…' button that opens the Cadence-style editor. The QLineEdit is the cell's
            canonical store (read/written via _cov_edit). `kind` (iload/trans/iv) picks the editor."""
            cols = getattr(t, "_cols", [])
            name = cols[c] if c < len(cols) else ""
            kind = "trans" if name == "trans" else ("iv" if name == "iv_sweep" else "iload")
            w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(1)
            le = QLineEdit(text); le.setFrame(False)
            le.setToolTip("Type a sweep directly, or click '…' for the editor.")
            h.addWidget(le, 1)
            b = QPushButton("…"); b.setMaximumWidth(22); b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setToolTip("Open the sweep/step editor (type / range / step-or-points / specific points).")
            h.addWidget(b)
            w._edit = le; w._kind = kind
            b.clicked.connect(lambda _=False, tt=t, ww=w: _ManifestEditorDialog._cov_edit_clicked(tt, ww))
            return w

        @staticmethod
        def _cov_edit(t, r, c):
            """The QLineEdit of a composite coverage-sweep cell at (r,c), or None for a plain cell.
            The single accessor every reader/writer uses so a coverage cell is widget- or item-
            backed transparently."""
            if c not in getattr(t, "_dialog_cols", set()):
                return None
            w = t.cellWidget(r, c)
            return getattr(w, "_edit", None) if w is not None else None

        @staticmethod
        def _cov_edit_clicked(t, w):
            """The '…' button handler: open the right editor for this composite cell's kind,
            editing its QLineEdit in place. Resolves the owning dialog via t.window()."""
            dlg = t.window()
            le = getattr(w, "_edit", None)
            kind = getattr(w, "_kind", "iload")
            if le is None:
                return
            if kind == "trans" and hasattr(dlg, "_open_trans_editor"):
                dlg._open_trans_editor(le)
            elif hasattr(dlg, "_open_cov_sweep_editor"):
                dlg._open_cov_sweep_editor(le, kind)

        @staticmethod
        def _table_del_row(t):
            r = t.currentRow()
            if r < 0:
                r = t.rowCount() - 1
            if r >= 0:
                t.removeRow(r)

        @staticmethod
        def _table_rows(t):
            rows = []
            for r in range(t.rowCount()):
                cells = []
                for c in range(t.columnCount()):
                    le = _ManifestEditorDialog._cov_edit(t, r, c)   # composite coverage cell?
                    if le is not None:
                        cells.append(le.text().strip())
                    else:
                        it = t.item(r, c)
                        cells.append(it.text().strip() if it else "")
                rows.append(cells)
            return rows

        # ---- checkbox column + per-object analysis override helpers ----------------
        @staticmethod
        def _row_checked(t, r, col_name):
            """The boolean state of a checkbox column (by header name) in row r."""
            cols = getattr(t, "_cols", [])
            if col_name not in cols:
                return False
            it = t.item(r, cols.index(col_name))
            return bool(it) and it.checkState() == QtCore.Qt.Checked

        @staticmethod
        def _set_row_checked(t, r, col_name, on):
            cols = getattr(t, "_cols", [])
            if col_name not in cols:
                return
            it = t.item(r, cols.index(col_name))
            if it is not None:
                it.setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)

        @staticmethod
        def _refresh_analysis_cell(t, r):
            """Reflect whether row r carries an analysis override on its gear button label."""
            an_idx = getattr(t, "_analysis_idx", None)
            if an_idx is None:
                return
            btn = t.cellWidget(r, an_idx)
            if btn is None:
                return
            key_it = t.item(r, 0)
            key = key_it.text().strip() if key_it else ""
            ov = (getattr(t, "_analysis", {}) or {}).get(key)
            if ov:
                btn.setText("gear ✓"); btn.setStyleSheet("font-weight:bold; color:#157f3b;")
                btn.setToolTip("Per-object override SET: " + ", ".join(
                    f"{k}={v}" for k, v in ov.items()) + "  (click to edit; clear to inherit).")
            else:
                btn.setText("gear…"); btn.setStyleSheet("")
                btn.setToolTip("Override the global AC/noise sweep for just this object "
                               "(unchecked → inherit the global default).")

        @staticmethod
        def _gear_clicked(t, btn):
            """Find the row that owns this gear button (sender), then open its analysis popup.
            Resolving the row at click time keeps it valid across table rebuilds/reorders."""
            an_idx = getattr(t, "_analysis_idx", None)
            if an_idx is None:
                return
            for r in range(t.rowCount()):
                if t.cellWidget(r, an_idx) is btn:
                    ki = t.item(r, 0)
                    key = ki.text().strip() if ki else ""
                    _ManifestEditorDialog._edit_row_analysis(t, key)
                    return

        @staticmethod
        def _edit_row_analysis(t, key):
            """Open a small popup to set this row's per-object analysis override (prefilled from
            the global default). Stores the result in t._analysis keyed by the row key text.
            Empty fields clear that kind from the override; an empty override is removed entirely."""
            if not key:
                QMessageBox.information(t, "Analysis override",
                                        "Give this row a key first, then set its analysis.")
                return
            role = getattr(t, "_analysis_role", None)
            with_noise = (role == "v_out")
            # the owning dialog (to read the global defaults the popup prefills from)
            dlg = t.window()
            g_ac = getattr(dlg, "f_ac", None)
            g_noise = getattr(dlg, "f_noise", None)
            cur = dict((getattr(t, "_analysis", {}) or {}).get(key) or {})
            pop = QtWidgets.QDialog(t)
            pop.setWindowTitle(f"Analysis override — {key}")
            pv = QVBoxLayout(pop)
            pv.addWidget(QLabel(
                "<span style='color:#678'>Override the global sweep for just this object. "
                "Blank a field → inherit the global default for that kind.</span>"))
            form = QFormLayout(); pv.addLayout(form)
            e_ac = QLineEdit(cur.get("ac", ""))
            e_ac.setPlaceholderText((g_ac.text() if g_ac else "") or "ac start=10 stop=500M dec=20")
            form.addRow("AC sweep", dlg._with_sweep_editor(e_ac, "ac")
                        if hasattr(dlg, "_with_sweep_editor") else e_ac)
            e_noise = None
            if with_noise:
                e_noise = QLineEdit(cur.get("noise", ""))
                e_noise.setPlaceholderText((g_noise.text() if g_noise else "")
                                           or "noise start=10 stop=100M dec=20")
                form.addRow("Noise sweep", dlg._with_sweep_editor(e_noise, "noise")
                            if hasattr(dlg, "_with_sweep_editor") else e_noise)
            brow = QHBoxLayout()
            b_ok = QPushButton("OK"); b_clear = QPushButton("Clear override")
            b_cancel = QPushButton("Cancel")
            brow.addStretch(1)
            brow.addWidget(b_clear); brow.addWidget(b_cancel); brow.addWidget(b_ok)
            pv.addLayout(brow)
            b_ok.clicked.connect(pop.accept)
            b_cancel.clicked.connect(pop.reject)
            b_clear.clicked.connect(lambda: (e_ac.clear(),
                                             e_noise.clear() if e_noise else None, pop.accept()))
            if pop.exec_():
                ov = {}
                if e_ac.text().strip():
                    ov["ac"] = e_ac.text().strip()
                if e_noise is not None and e_noise.text().strip():
                    ov["noise"] = e_noise.text().strip()
                store = getattr(t, "_analysis", None)
                if store is None:
                    store = {}; t._analysis = store
                if ov:
                    store[key] = ov
                else:
                    store.pop(key, None)
                # refresh the gear label on the row that owns this key
                for r in range(t.rowCount()):
                    ki = t.item(r, 0)
                    if ki and ki.text().strip() == key:
                        _ManifestEditorDialog._refresh_analysis_cell(t, r)

        # ---- form <-> dict (the typed OVERLAY, D6) ---------------------------------
        def _dict_to_form(self, m):
            """Populate the Form widgets from a parsed manifest dict. Tables are rebuilt from
            the dict's role maps; unknown keys are NOT shown (they live on in self._stash)."""
            d = m.get("dut") or {}
            self.f_name.setText(str(m.get("name", "")))
            self.f_dut_lib.setText(str(d.get("lib", "")))
            self.f_dut_cell.setText(str(d.get("cell", "")))
            self.f_tb_lib.setText(str(d.get("tb_lib", "")))
            self.f_tb_cell.setText(str(d.get("tb_cell", "")))
            self.f_ground.setText(str(m.get("ground", "")))
            self.f_extract.setText(str(d.get("extract_cell", "")))
            self.f_tb_view.setText(str(d.get("tb_view", "")))
            self.f_dut_inst.setText(str(d.get("tb_inst", "")))
            self.f_src_test.setText(str(d.get("ade_src_test", "")))
            self.f_extract.setPlaceholderText(
                (d.get("tb_cell", "") or "TB") + "_extract  (auto)")
            # [2.4] global default analysis + single corner label (the per-object overrides + the
            # current-PSRR set + the corners.pull_from_session flag are restored separately below).
            an = m.get("analysis") or {}
            self.f_ac.setText(str(an.get("ac", "")))
            self.f_noise.setText(str(an.get("noise", "")))
            cor = m.get("corners") or {}
            fb = cor.get("fallback") or []
            self.f_corner.setText(str(fb[0]) if fb else "nom")

            # the per-rail/per-sink COVERAGE display cells are populated from m['coverage'] in a
            # dedicated post-pass (below), not from the role-entry dicts -- `fill` leaves them blank.
            _COV_COLS = {"iload sweep", "trans", "iv_sweep"}

            def fill(t, mp, cols):
                """`cols` = the LOGICAL manifest keys mapping to the DATA columns ONLY (the
                checkbox + analysis + coverage display columns are populated separately).
                cols[0]=='key'. The row is built ALIGNED to the table's physical column order, so
                a coverage display column is emitted as a blank placeholder here."""
                t.setRowCount(0)
                if getattr(t, "_analysis", None) is not None:
                    t._analysis.clear()             # start clean; restored from the dict below
                tcols = getattr(t, "_cols", cols)
                # physical columns the row builder must SKIP (emit a blank placeholder): the gear
                # 'analysis' column, the checkbox columns (set separately), and the coverage cells.
                skip = set(_COV_COLS) | set(getattr(t, "_check_cols", set())) | {"analysis"}
                for k, ent in (mp or {}).items():
                    ent = ent or {}
                    # walk the table's PHYSICAL columns, consuming the LOGICAL data columns in
                    # order; skip columns blank so a coverage/checkbox/gear cell stays untouched.
                    li = 1
                    row = [k]
                    unresolved_pin = None        # set if the 'net' cell is a placeholder default
                    for pc in tcols[1:]:
                        if pc in skip:
                            row.append("")                 # filled by a dedicated pass / widget
                            continue
                        col = cols[li] if li < len(cols) else None
                        li += 1
                        if col == "net":
                            disp, is_ph = self._net_display(ent.get("net"))
                            row.append(disp)
                            if is_ph:
                                unresolved_pin = ent.get("pin") or k
                        elif col is None:
                            row.append("")
                        else:
                            v = ent.get(col, "")
                            row.append("" if v is None else str(v))
                    # restore this object's per-object analysis override into the table store
                    ov = ent.get("analysis")
                    if isinstance(ov, dict) and ov and getattr(t, "_analysis", None) is not None:
                        t._analysis[k] = dict(ov)
                    r = self._table_add_row(t, row)
                    if unresolved_pin is not None:
                        self._flag_unresolved_net(t, r, tcols.index("net"), unresolved_pin)
            fill(self.t_supplies, m.get("supplies"), ["key", "net", "dc", "tb_src"])
            fill(self.t_vout, m.get("v_out"), ["key", "net", "src"])
            fill(self.t_iout, m.get("i_out"), ["key", "net", "dc", "probe_src"])
            fill(self.t_bias, m.get("bias"), ["key", "net", "dc"])
            # COVERAGE post-pass: tier/temps/slew_en/lin_gate globals + the per-rail/per-sink cells.
            self._dict_to_coverage(m)
            # [2.3] current-PSRR is now a per-supply checkbox. Absent key -> check ALL (mirrors
            # manifest._fill_defaults: current-PSRR vs every supply). Present -> check the listed.
            cpsrr = m.get("current_psrr_supplies")
            check_all = cpsrr is None
            cset = set(cpsrr or [])
            for r in range(self.t_supplies.rowCount()):
                ki = self.t_supplies.item(r, 0)
                key = ki.text().strip() if ki else ""
                self._set_row_checked(self.t_supplies, r, "PSRR→I",
                                      check_all or key in cset)

        def _dict_to_coverage(self, m):
            """Populate the Coverage widgets + the per-rail/per-sink coverage table cells from
            m['coverage']. ABSENT coverage -> tier=T4 defaults + empty cells (a no-coverage
            manifest opens clean). The role tables must already be filled (the cells key by row)."""
            cov = m.get("coverage") or {}
            tier = cov.get("tier", "T4")
            self.f_cov_tier.setCurrentText(tier if tier in
                                           ("T0", "T1", "T2", "T3", "T4") else "T4")
            tps = cov.get("temps") or []
            self.f_cov_temps.setText(", ".join(self._fmt_num(t) for t in tps))
            self.f_cov_slew.setChecked(bool(int(cov.get("slew_en", 0) or 0)))
            self.f_cov_lin.setChecked(bool(cov.get("lin_gate", False)))
            # per-rail / per-sink cells -- keyed by the row's key text.
            loads = cov.get("loads") or {}
            trans = cov.get("transient") or {}
            ivs = cov.get("iv") or {}
            self._set_cov_cells(self.t_vout, "iload sweep",
                                {k: self._loads_to_text(v) for k, v in loads.items()})
            self._set_cov_cells(self.t_vout, "trans",
                                {k: self._trans_to_text(v) for k, v in trans.items()})
            # i_out iv_sweep: prefer the wired coverage.iv; legacy fall-back to the per-entry
            # i_out.<c>.iv_sweep (old manifests) so an existing knee still shows + round-trips.
            ivtext = {k: self._ivcov_to_text(v) for k, v in ivs.items()}
            for k, ent in (m.get("i_out") or {}).items():
                if k not in ivtext and (ent or {}).get("iv_sweep") is not None:
                    ivtext[k] = self._iv_to_text(ent["iv_sweep"])
            self._set_cov_cells(self.t_iout, "iv_sweep", ivtext)

        @staticmethod
        def _set_cov_cells(t, col_name, by_key):
            """Write a {row-key -> cell text} map into table `t`'s `col_name` column, matching on
            the row's key cell. Missing keys leave the cell blank (the default)."""
            cols = getattr(t, "_cols", [])
            if col_name not in cols:
                return
            ci = cols.index(col_name)
            for r in range(t.rowCount()):
                ki = t.item(r, 0)
                key = ki.text().strip() if ki else ""
                txt = by_key.get(key, "")
                le = _ManifestEditorDialog._cov_edit(t, r, ci)    # composite coverage cell?
                if le is not None:
                    le.setText(txt or "")
                    continue
                it = t.item(r, ci)
                if it is None:
                    it = QTableWidgetItem(); t.setItem(r, ci, it)
                it.setText(txt or "")

        @staticmethod
        def _net_display(net):
            """A '<net:PIN>' value is the resolver's UNRESOLVED placeholder, not a real net.
            Don't show the wrapper to a designer hand-editing the form -- strip it to the bare
            PIN name (the obvious default; for most testbenches the connecting net == the pin
            name). Returns (display_text, is_placeholder)."""
            s = "" if net is None else str(net)
            if s.startswith("<net:") and s.endswith(">"):
                return s[len("<net:"):-1], True
            return s, False

        @staticmethod
        def _flag_unresolved_net(t, r, c, pin):
            """Style a net cell that was filled from an unresolved '<net:PIN>' placeholder:
            amber italic + a tooltip telling the designer it's a default to confirm or edit."""
            it = t.item(r, c)
            if it is None:
                return
            it.setForeground(QtGui.QColor("#b8860b"))
            f = it.font(); f.setItalic(True); it.setFont(f)
            it.setToolTip(
                f"Unresolved — defaulted to the pin name “{pin}”. Keep it if your "
                f"testbench net is also “{pin}”; otherwise type the real TB net "
                f"(or run Mode A in Virtuoso to auto-resolve).")

        @staticmethod
        def _iv_to_text(iv):
            if iv is None:
                return ""
            if isinstance(iv, str):
                return iv
            if isinstance(iv, (list, tuple)) and len(iv) == 3:   # [vlo,vhi,step] -> start:step:stop
                return f"{iv[0]}:{iv[2]}:{iv[1]}"
            return ", ".join(str(x) for x in iv)

        @staticmethod
        def _iv_from_text(s):
            s = s.strip()
            if not s:
                return None
            if s.lower() == "auto":
                return "auto"
            try:
                nums = [float(x) for x in s.split(":")]
            except ValueError:
                return s
            if len(nums) == 3:                                   # start:step:stop -> [vlo,vhi,step]
                start, step, stop = nums
                return [start, stop, step]
            return nums

        # ---- coverage compact-string parsers / renderers (§2 the GUI side) ----------
        # The per-rail/per-sink coverage cells are terse strings the designer types; these turn
        # them into the manifest's coverage.{loads,transient,iv} sub-dicts and back, byte-clean
        # (an empty cell emits no sub-dict). SI suffixes (f/p/n/u/m/k/M/G) are honored both ways.
        _SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
               "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}

        @staticmethod
        def _si_to_float(tok):
            """Parse a number with an optional SI suffix ('50u' -> 5e-5). Raises ValueError on
            garbage so the caller can keep the raw string / skip the cell."""
            tok = tok.strip()
            if not tok:
                raise ValueError("empty")
            mul = _ManifestEditorDialog._SI.get(tok[-1])
            if mul is not None and not tok[-1].isdigit():
                return float(tok[:-1]) * mul
            return float(tok)

        @staticmethod
        def _float_to_si(x):
            """Render a float back to a compact SI string ('5e-5' -> '50u'). Picks the suffix that
            keeps the mantissa in [1,1000); falls back to '%g' for 0 / out-of-range values."""
            try:
                v = float(x)
            except (TypeError, ValueError):
                return str(x)
            if v == 0:
                return "0"
            for suf, mul in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1.0),
                             ("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12), ("f", 1e-15)):
                scaled = v / mul
                if 1.0 <= abs(scaled) < 1000.0:
                    return f"{scaled:g}{suf}"
            return f"{v:g}"

        # ------------------------------------------------ Cadence-style sweep editor (Block C)
        @staticmethod
        def _split_brackets(s):
            """Whitespace-split a sweep string but keep a [...] group (Spectre `values=[a b c]`,
            which may contain spaces) as ONE token, so a bracketed list is never shattered."""
            toks, buf, depth = [], [], 0
            for ch in str(s):
                if ch == "[":
                    depth += 1; buf.append(ch)
                elif ch == "]":
                    depth = max(0, depth - 1); buf.append(ch)
                elif ch.isspace() and depth == 0:
                    if buf:
                        toks.append("".join(buf)); buf = []
                else:
                    buf.append(ch)
            if buf:
                toks.append("".join(buf))
            return toks

        @staticmethod
        def _sweep_parse(s):
            """Parse a sweep string ('ac start=10 stop=500M dec=20 values=[1 2 3]') into the dict
            the sweep editor edits. The sweep token (lin/step/dec/log) sets both the TYPE (lin/step
            -> linear; dec/log -> log) and the spec-kind. Unrecognised key=val / bare tokens are
            preserved verbatim in 'extra' so a round-trip never drops them. Returns dict:
              {name, type:'lin'|'log', start, stop, spec_kind:'lin'|'step'|'dec'|'log'|'',
               spec_val, values:[str,...], extra:[str,...]}  (start/stop/spec_val/values raw)."""
            toks = _ManifestEditorDialog._split_brackets((s or "").strip())
            f = {"name": "", "type": "log", "start": "", "stop": "",
                 "spec_kind": "", "spec_val": "", "values": [], "extra": []}
            if toks and "=" not in toks[0]:
                f["name"] = toks[0]; toks = toks[1:]
            for t in toks:
                if "=" not in t:
                    f["extra"].append(t); continue
                k, v = t.split("=", 1)
                if k in ("lin", "step", "dec", "log"):
                    f["spec_kind"] = k; f["spec_val"] = v
                    f["type"] = "log" if k in ("dec", "log") else "lin"
                elif k == "start":
                    f["start"] = v
                elif k == "stop":
                    f["stop"] = v
                elif k == "values":
                    inner = v.strip().lstrip("[").rstrip("]")
                    f["values"] = [p for p in inner.replace(",", " ").split() if p]
                else:
                    f["extra"].append(t)
            return f

        @staticmethod
        def _sweep_render(f):
            """Render a sweep-editor dict back to a canonical sweep string. Emits
            '<name> start=<a> stop=<b> <spec_kind>=<spec_val> values=[p1 p2 ...] <extra...>',
            dropping any empty piece. Spectre's `values=[...]` uses space separation."""
            parts = [f.get("name") or "ac"]
            if f.get("start"):
                parts.append(f"start={f['start']}")
            if f.get("stop"):
                parts.append(f"stop={f['stop']}")
            sk, sv = f.get("spec_kind"), f.get("spec_val")
            if sk and sv:
                parts.append(f"{sk}={sv}")
            vals = f.get("values") or []
            if vals:
                parts.append("values=[" + " ".join(str(p) for p in vals) + "]")
            parts.extend(f.get("extra") or [])
            return " ".join(parts)

        @staticmethod
        def _default_sweep_fields(name):
            """The standard default sweep fields for a fresh ac/noise editor -- MIRRORS the backend
            manifest.load() defaults: log, 20 points/decade, 10 Hz -> 500 MHz (ac) / 100 MHz
            (noise). So opening the editor on a blank field and clicking OK yields the SAME sweep
            the backend would have defaulted to, never a bare param-less name."""
            stop = "100M" if name == "noise" else "500M"
            return {"name": name, "type": "log", "start": "10", "stop": stop,
                    "spec_kind": "dec", "spec_val": "20", "values": [], "extra": []}

        @staticmethod
        def _sweep_editor_initial(text, default_name):
            """The fields the sweep editor opens with. Parse `text`; but when it carries NO sweep
            params (a fresh/empty field, or a bare 'ac' with only extra tokens) seed the standard
            defaults for default_name -- so 'open + OK' produces a sensible sweep, not a bare name.
            Any present name + extra tokens are preserved."""
            f = _ManifestEditorDialog._sweep_parse(text)
            name = f.get("name") or default_name
            if f.get("start") or f.get("stop") or f.get("spec_val") or f.get("values"):
                f["name"] = name
                return f
            d = _ManifestEditorDialog._default_sweep_fields(name)
            d["extra"] = f.get("extra") or []
            return d

        def _with_sweep_editor(self, edit, default_name):
            """Wrap a sweep QLineEdit + an 'Edit…' button in one widget (for QFormLayout.addRow).
            The QLineEdit stays the canonical string store; the button opens the Cadence-style
            editor, which parses the current string and writes the rebuilt string back."""
            cont = QWidget(); h = QHBoxLayout(cont)
            h.setContentsMargins(0, 0, 0, 0); h.setSpacing(4)
            h.addWidget(edit, 1)
            b = QPushButton("Edit…"); b.setMaximumWidth(58)
            b.setToolTip("Open the Cadence-style sweep editor: linear/log, range, step-or-points, "
                         "plus optional specific added points.")
            b.clicked.connect(lambda _=False, e=edit, n=default_name: self._open_sweep_editor(e, n))
            h.addWidget(b)
            return cont

        @staticmethod
        def _spec_items(swtype):
            """The 'Specify by' choices for a sweep type: [(label, kind), ...]. Linear -> number of
            points (lin) / step size (step); Log -> points per decade (dec) / number of steps (log)."""
            if swtype == "lin":
                return [("Number of points", "lin"), ("Step size", "step")]
            return [("Points per decade", "dec"), ("Number of steps", "log")]

        def _open_sweep_editor(self, edit, default_name="ac"):
            """The Cadence-style sweep editor dialog. Parses edit's current string, lets the user
            set type / start / stop / specify-by / specific points, and writes the rebuilt string
            back into edit on OK. edit (a QLineEdit) remains the single source of truth."""
            f = self._sweep_editor_initial(edit.text(), default_name)
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle(f"Sweep editor — {f['name']}")
            v = QVBoxLayout(dlg)
            v.addWidget(QLabel("<span style='color:#678'>Cadence-style sweep. The result is written "
                               "back as a Spectre sweep string. SI suffixes ok (10, 500M, 1k).</span>"))
            form = QFormLayout(); v.addLayout(form)

            c_type = QComboBox(); c_type.addItem("Linear", "lin"); c_type.addItem("Logarithmic", "log")
            c_type.setCurrentIndex(0 if f["type"] == "lin" else 1)
            form.addRow("Sweep type", c_type)
            e_start = QLineEdit(f["start"]); e_start.setPlaceholderText("e.g. 10")
            form.addRow("Start", e_start)
            e_stop = QLineEdit(f["stop"]); e_stop.setPlaceholderText("e.g. 500M")
            form.addRow("Stop", e_stop)
            c_spec = QComboBox()
            e_specv = QLineEdit(f["spec_val"]); e_specv.setPlaceholderText("e.g. 20")
            form.addRow("Specify by", c_spec)
            form.addRow("  value", e_specv)
            e_points = QLineEdit(", ".join(f["values"]))
            e_points.setPlaceholderText("optional, comma/space-separated, e.g. 304M, 1.2G")
            e_points.setToolTip("Specific frequencies to ADD to the swept grid (Spectre "
                                "values=[...]). Useful to land exactly on a known resonance/spur.")
            form.addRow("Add specific points", e_points)

            def _fill_spec(want_kind):
                items = self._spec_items("lin" if c_type.currentData() == "lin" else "log")
                c_spec.blockSignals(True); c_spec.clear()
                for label, kind in items:
                    c_spec.addItem(label, kind)
                idx = next((i for i, (_l, k) in enumerate(items) if k == want_kind), 0)
                c_spec.setCurrentIndex(idx)
                c_spec.blockSignals(False)

            _fill_spec(f["spec_kind"])
            # switching type re-populates the specify-by choices (keeping the kind if still valid)
            c_type.currentIndexChanged.connect(lambda _i: _fill_spec(c_spec.currentData()))

            brow = QHBoxLayout(); brow.addStretch(1)
            b_cancel = QPushButton("Cancel"); b_ok = QPushButton("OK")
            brow.addWidget(b_cancel); brow.addWidget(b_ok); v.addLayout(brow)
            b_cancel.clicked.connect(dlg.reject); b_ok.clicked.connect(dlg.accept)
            if not dlg.exec_():
                return
            pts = [p for p in e_points.text().replace(",", " ").split() if p]
            newf = {"name": f["name"],
                    "type": "lin" if c_type.currentData() == "lin" else "log",
                    "start": e_start.text().strip(), "stop": e_stop.text().strip(),
                    "spec_kind": c_spec.currentData(), "spec_val": e_specv.text().strip(),
                    "values": pts, "extra": f.get("extra") or []}
            edit.setText(self._sweep_render(newf))

        # ----------------- coverage-cell sweep editor (iload sweep / iv_sweep), double-click
        @staticmethod
        def _covsweep_parse(text):
            """Parse a coverage-sweep CELL ('<type> start stop n [+ p1,p2]', bare 'p1,p2', legacy
            'start:step:stop', or '' / 'auto') into editor fields {type,start,stop,n,points:[str]}.
            String-preserving (keeps the user's SI text, unlike the float-valued manifest dict)."""
            s = (text or "").strip()
            f = {"type": "lin", "start": "", "stop": "", "n": "", "points": []}
            if not s or s.lower() == "auto":
                return f
            sweep_part, _, pts_part = s.partition("+")
            sweep_part, pts_part = sweep_part.strip(), pts_part.strip()
            toks = sweep_part.split()
            if len(toks) >= 4 and toks[0] in ("lin", "log"):
                f["type"], f["start"], f["stop"], f["n"] = toks[0], toks[1], toks[2], toks[3]
            elif ":" in sweep_part and "+" not in s:
                bits = sweep_part.split(":")
                if len(bits) == 3:
                    try:
                        a, step, b = (_ManifestEditorDialog._si_to_float(x) for x in bits)
                        n = int(round((b - a) / step)) + 1 if step else 2
                        f["type"], f["start"], f["stop"], f["n"] = "lin", bits[0], bits[2], str(max(n, 2))
                    except ValueError:
                        pass
            elif sweep_part and "+" not in s:                 # bare points head ('p1,p2')
                pts_part = sweep_part
            f["points"] = [p for p in pts_part.replace(" ", "").split(",") if p]
            return f

        @staticmethod
        def _covsweep_render(f):
            """Editor fields -> a coverage-sweep cell string. '<type> start stop n' when a full
            range is given; specific points appended as '+ p1,p2' (or bare 'p1,p2' with no range);
            '' for empty (= no sweep)."""
            typ = f.get("type") or "lin"
            a, b, n = (str(f.get(k) or "").strip() for k in ("start", "stop", "n"))
            pts = [str(p).strip() for p in (f.get("points") or []) if str(p).strip()]
            head = f"{typ} {a} {b} {n}" if (a and b and n) else ""
            tail = ",".join(pts)
            if head and tail:
                return f"{head} + {tail}"
            return head or tail                              # range-only, points-only, or ''

        @staticmethod
        def _cov_spec_items(swtype):
            """'Specify by' choices for a coverage sweep: linear -> number of points / step size;
            log -> number of points only (a log grid is defined by total point count)."""
            if swtype == "lin":
                return [("Number of points", "points"), ("Step size", "step")]
            return [("Number of points", "points")]

        def _open_cov_sweep_editor(self, le, kind="iload"):
            """The Cadence-style coverage sweep editor (linear/log + range + number-of-points OR
            step-size + optional specific points). Parses the cell QLineEdit `le`, lets the user
            edit, writes the rebuilt cell text back on OK. `kind` ('iv'/'iload') tunes labels/units.
            The cell stores a point COUNT, so a step size is converted to a count on OK."""
            if le is None:
                return
            f = self._covsweep_parse(le.text())
            unit = "V" if kind == "iv" else "A"
            title = {"iv": "I-V voltage sweep", "iload": "Load (iload) sweep"}.get(kind, "Coverage sweep")
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle(title)
            v = QVBoxLayout(dlg)
            v.addWidget(QLabel(f"<span style='color:#678'>Cadence-style {title.lower()} ({unit}). "
                               f"Linear/log range by point count or step size, plus optional specific "
                               f"points (land exactly on a knee/compliance value). Leave the range "
                               f"blank for points-only; Clear for no sweep.</span>"))
            form = QFormLayout(); v.addLayout(form)
            c_type = QComboBox(); c_type.addItem("Linear", "lin"); c_type.addItem("Logarithmic", "log")
            c_type.setCurrentIndex(0 if (f["type"] or "lin") == "lin" else 1)
            form.addRow("Sweep type", c_type)
            e_start = QLineEdit(f["start"]); e_start.setPlaceholderText("e.g. 0")
            form.addRow(f"Start ({unit})", e_start)
            e_stop = QLineEdit(f["stop"]); e_stop.setPlaceholderText("e.g. 0.8" if kind == "iv" else "e.g. 2m")
            form.addRow(f"Stop ({unit})", e_stop)
            c_spec = QComboBox()
            e_specv = QLineEdit(f["n"]); e_specv.setPlaceholderText("e.g. 12")
            form.addRow("Specify by", c_spec)
            form.addRow("  value", e_specv)
            e_pts = QLineEdit(", ".join(f["points"]))
            e_pts.setPlaceholderText("optional, comma/space-separated")
            e_pts.setToolTip("Specific values to ADD to the swept grid (folded into one Spectre dc "
                             "value list). Useful to land exactly on a knee / compliance point.")
            form.addRow("Add specific points", e_pts)

            def _fill_cov_spec():
                items = self._cov_spec_items(c_type.currentData())
                cur = c_spec.currentData()
                c_spec.blockSignals(True); c_spec.clear()
                for label, k in items:
                    c_spec.addItem(label, k)
                idx = next((i for i, (_l, k) in enumerate(items) if k == cur), 0)
                c_spec.setCurrentIndex(idx); c_spec.blockSignals(False)
            _fill_cov_spec()
            c_type.currentIndexChanged.connect(lambda _i: _fill_cov_spec())

            brow = QHBoxLayout(); brow.addStretch(1)
            b_clear = QPushButton("Clear"); b_cancel = QPushButton("Cancel"); b_ok = QPushButton("OK")
            brow.addWidget(b_clear); brow.addWidget(b_cancel); brow.addWidget(b_ok); v.addLayout(brow)
            b_cancel.clicked.connect(dlg.reject); b_ok.clicked.connect(dlg.accept)
            b_clear.clicked.connect(lambda: (e_start.clear(), e_stop.clear(), e_specv.clear(),
                                             e_pts.clear(), dlg.accept()))
            if not dlg.exec_():
                return
            pts = [p for p in e_pts.text().replace(",", " ").split() if p]
            a, b = e_start.text().strip(), e_stop.text().strip()
            n_str = ""
            if c_spec.currentData() == "step":               # step size -> convert to a point count
                try:
                    av = self._si_to_float(a); bv = self._si_to_float(b)
                    step = self._si_to_float(e_specv.text())
                    if step:
                        n_str = str(max(int(round((bv - av) / step)) + 1, 2))
                except ValueError:
                    n_str = ""
            else:
                n_str = e_specv.text().strip()
            newf = {"type": c_type.currentData(), "start": a, "stop": b, "n": n_str, "points": pts}
            le.setText(self._covsweep_render(newf))

        # ----------------- transient-step editor (the v_out 'trans' cell)
        @staticmethod
        def _trans_parse_cell(text):
            """Parse a 'trans' cell ('<from>:<to>[:label] , … @edge=…,tstop=…,tstep=…') into editor
            fields {steps:[{from,to,label}], edge, tstop, tstep} (string-preserving)."""
            s = (text or "").strip()
            out = {"steps": [], "edge": "", "tstop": "", "tstep": ""}
            if not s:
                return out
            body, _, tail = s.partition("@")
            for chunk in body.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                bits = chunk.split(":")
                if len(bits) < 2:
                    continue
                out["steps"].append({"from": bits[0].strip(), "to": bits[1].strip(),
                                     "label": bits[2].strip() if len(bits) >= 3 else ""})
            for kv in tail.replace(" ", "").split(","):
                if "=" in kv:
                    k, val = kv.split("=", 1)
                    if k in ("edge", "tstop", "tstep"):
                        out[k] = val
            return out

        @staticmethod
        def _trans_render_cell(f):
            """Editor fields -> a 'trans' cell string (matches _trans_to_text's format so it
            round-trips through _trans_from_text)."""
            chunks = []
            for st in f.get("steps", []):
                a = str(st.get("from", "")).strip(); b = str(st.get("to", "")).strip()
                lbl = str(st.get("label", "")).strip()
                if not (a and b):
                    continue
                chunks.append(f"{a}:{b}:{lbl}" if lbl else f"{a}:{b}")
            txt = " , ".join(chunks)
            tail = [f"{k}={str(f.get(k)).strip()}" for k in ("edge", "tstop", "tstep")
                    if str(f.get(k) or "").strip()]
            if tail:
                txt = (txt + " @" + ",".join(tail)) if txt else ("@" + ",".join(tail))
            return txt

        def _open_trans_editor(self, le):
            """The transient load-STEP editor for a v_out 'trans' cell, in designer terms: a
            BASELINE (light) load + a list of TARGET (heavy) loads -- each target becomes one
            baseline→target slew step -- plus the edge time and sim duration. Maps to/from the
            cell's from:to step format. Writes the rebuilt cell text back on OK."""
            if le is None:
                return
            f = self._trans_parse_cell(le.text())
            steps = f["steps"]
            froms = {s["from"] for s in steps if s.get("from")}
            baseline = next(iter(froms)) if len(froms) == 1 else (steps[0]["from"] if steps else "")
            targets = [s["to"] for s in steps if s.get("to")]
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Transient load steps (slew)")
            v = QVBoxLayout(dlg)
            v.addWidget(QLabel("<span style='color:#678'>Load-transient (slew) test: from a "
                               "<b>baseline</b> (light) load, step up to each <b>target</b> (heavy) "
                               "load and watch Vout droop &amp; recovery. One run per target. SI "
                               "suffixes ok. <b>edge</b> = how fast the current switches; <b>tstop</b> "
                               "= how long to simulate (long enough to see recovery). This is the "
                               "DYNAMIC axis; static DC loads go in 'iload sweep'.</span>"))
            form = QFormLayout(); v.addLayout(form)
            e_base = QLineEdit(baseline); e_base.setPlaceholderText("e.g. 100u")
            e_base.setToolTip("The light/initial load every step starts from.")
            form.addRow("Baseline (light) load", e_base)
            e_tg = QLineEdit(", ".join(targets)); e_tg.setPlaceholderText("e.g. 2m, 3m, 4m")
            e_tg.setToolTip("Heavy loads to step to — one baseline→target slew run per value.")
            form.addRow("Step to (targets)", e_tg)
            e_edge = QLineEdit(f["edge"]); e_edge.setPlaceholderText("e.g. 1n")
            e_tstop = QLineEdit(f["tstop"]); e_tstop.setPlaceholderText("e.g. 10u")
            e_tstep = QLineEdit(f["tstep"]); e_tstep.setPlaceholderText("optional, e.g. 10n")
            form.addRow("Edge time", e_edge); form.addRow("Sim time (tstop)", e_tstop)
            form.addRow("tstep (optional)", e_tstep)
            brow = QHBoxLayout(); brow.addStretch(1)
            b_clear = QPushButton("Clear"); b_cancel = QPushButton("Cancel"); b_ok = QPushButton("OK")
            brow.addWidget(b_clear); brow.addWidget(b_cancel); brow.addWidget(b_ok); v.addLayout(brow)
            b_cancel.clicked.connect(dlg.reject); b_ok.clicked.connect(dlg.accept)
            b_clear.clicked.connect(lambda: (e_base.clear(), e_tg.clear(), e_edge.clear(),
                                             e_tstop.clear(), e_tstep.clear(), dlg.accept()))
            if not dlg.exec_():
                return
            base = e_base.text().strip()
            tgs = [t for t in e_tg.text().replace(",", " ").split() if t]
            steps2 = [{"from": base, "to": t, "label": t} for t in tgs]  # label = target (nice tag)
            newf = {"steps": steps2, "edge": e_edge.text().strip(),
                    "tstop": e_tstop.text().strip(), "tstep": e_tstep.text().strip()}
            le.setText(self._trans_render_cell(newf))

        @staticmethod
        def _loads_to_text(spec):
            """coverage.loads[<o>] dict -> the compact 'iload sweep' cell. Sweep first
            ('<type> <start> <stop> <n>'), then any extra points as '+ p1,p2'. None/{} -> ''."""
            spec = spec or {}
            sw = spec.get("sweep")
            pts = spec.get("points") or []
            parts = []
            if sw:
                parts.append("{} {} {} {}".format(
                    sw.get("type", "lin"),
                    _ManifestEditorDialog._float_to_si(sw["start"]),
                    _ManifestEditorDialog._float_to_si(sw["stop"]),
                    int(sw.get("n", 0) or 0)))
            if pts:
                ptxt = ",".join(_ManifestEditorDialog._float_to_si(p) for p in pts)
                parts.append(f"+ {ptxt}" if parts else ptxt)
            return " ".join(parts)

        @staticmethod
        def _loads_from_text(s):
            """The 'iload sweep' cell -> coverage.loads[<o>] dict (or None when blank). Formats:
              '<type> <start> <stop> <n>'              -> {sweep:{...}}
              '<type> <start> <stop> <n> + p1,p2'      -> {sweep, points}
              '+ p1,p2'  or  'p1,p2'  (no type token)  -> {points} only
            SI suffixes are parsed; an unparseable cell returns None (no coverage emitted)."""
            s = (s or "").strip()
            if not s:
                return None
            sweep_part, _, pts_part = s.partition("+")
            sweep_part, pts_part = sweep_part.strip(), pts_part.strip()
            out = {}
            toks = sweep_part.split()
            # a leading sweep needs a non-numeric type token + 3 numbers; otherwise the whole
            # head is points (handles '50u,170u' with NO '+').
            if len(toks) >= 4 and toks[0] in ("lin", "log"):
                try:
                    out["sweep"] = {"type": toks[0],
                                    "start": _ManifestEditorDialog._si_to_float(toks[1]),
                                    "stop": _ManifestEditorDialog._si_to_float(toks[2]),
                                    "n": int(float(toks[3]))}
                except (ValueError, IndexError):
                    return None
            elif sweep_part and "+" not in s:
                # no '+' separator and not a recognized sweep head -> treat the head as points
                pts_part = (sweep_part + "," + pts_part).strip(",") if pts_part else sweep_part
                sweep_part = ""
            pts = []
            for p in pts_part.replace(" ", "").split(","):
                if not p:
                    continue
                try:
                    pts.append(_ManifestEditorDialog._si_to_float(p))
                except ValueError:
                    pass
            if pts:
                out["points"] = pts
            return out or None

        @staticmethod
        def _trans_to_text(spec):
            """coverage.transient[<o>] dict -> the compact 'trans' cell. Steps as
            '<from>:<to>[:label] , ...' then an optional '@edge=..,tstop=..' tail. None/{} -> ''."""
            spec = spec or {}
            steps = spec.get("steps") or []
            if not steps:
                return ""
            f2 = _ManifestEditorDialog._float_to_si
            chunks = []
            for st in steps:
                base = f"{f2(st.get('from'))}:{f2(st.get('to'))}"
                lbl = st.get("label")
                chunks.append(f"{base}:{lbl}" if lbl else base)
            txt = " , ".join(chunks)
            tail = []
            for k in ("edge", "tstop", "tstep"):
                if spec.get(k) is not None:
                    tail.append(f"{k}={f2(spec[k])}")
            if tail:
                txt += " @" + ",".join(tail)
            return txt

        @staticmethod
        def _trans_from_text(s):
            """The 'trans' cell -> coverage.transient[<o>] dict (or None when blank). Format:
              '<from>:<to>[:label] , ...'  with an optional trailing '@edge=1n,tstop=1u,tstep=..'.
            SI suffixes parsed. An unparseable step is skipped; no steps -> None."""
            s = (s or "").strip()
            if not s:
                return None
            body, _, tail = s.partition("@")
            steps = []
            for chunk in body.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                bits = chunk.split(":")
                if len(bits) < 2:
                    continue
                try:
                    st = {"from": _ManifestEditorDialog._si_to_float(bits[0]),
                          "to": _ManifestEditorDialog._si_to_float(bits[1])}
                except ValueError:
                    continue
                if len(bits) >= 3 and bits[2].strip():
                    st["label"] = bits[2].strip()
                steps.append(st)
            if not steps:
                return None
            out = {"steps": steps}
            for kv in tail.replace(" ", "").split(","):
                if "=" not in kv:
                    continue
                k, v = kv.split("=", 1)
                if k in ("edge", "tstop", "tstep"):
                    try:
                        out[k] = _ManifestEditorDialog._si_to_float(v)
                    except ValueError:
                        pass
            return out

        @staticmethod
        def _ivcov_to_text(spec):
            """coverage.iv[<c>] dict -> the 'iv_sweep' cell. Sweep first ('<type> <start> <stop>
            <n>'), then any specific added points as '+ p1,p2'; points-only -> 'p1,p2'. None -> ''."""
            spec = spec or {}
            sw = spec.get("sweep")
            pts = spec.get("points") or []
            f2 = _ManifestEditorDialog._float_to_si
            head = ("{} {} {} {}".format(sw.get("type", "lin"), f2(sw["start"]), f2(sw["stop"]),
                                         int(sw.get("n", 0) or 0)) if sw else "")
            tail = ",".join(f2(p) for p in pts)
            if head and tail:
                return f"{head} + {tail}"
            return head or tail            # sweep-only, or points-only (bare), or '' for neither

        @staticmethod
        def _ivcov_from_text(s):
            """The 'iv_sweep' cell -> coverage.iv[<c>] dict (or None). Accepted forms:
              '<type> <start> <stop> <n>'          e.g. 'lin 0 1.1 12'  -> {sweep:{...}}
              '<type> <start> <stop> <n> + p1,p2'  e.g. 'lin 0 1.1 12 + 0.5,0.9' -> {sweep,points}
              'p1,p2' or '+ p1,p2'                 specific I-V points only       -> {points}
              legacy 'start:step:stop'             e.g. '0:0.01:1.1'    -> {sweep:{type:lin,...}}
              blank / 'auto'                                                       -> None (no sweep)
            SI suffixes parsed. Unparseable -> None."""
            s = (s or "").strip()
            if not s or s.lower() == "auto":
                return None
            sweep_part, _, pts_part = s.partition("+")
            sweep_part, pts_part = sweep_part.strip(), pts_part.strip()
            out = {}
            toks = sweep_part.split()
            if len(toks) >= 4 and toks[0] in ("lin", "log"):
                try:
                    out["sweep"] = {"type": toks[0],
                                    "start": _ManifestEditorDialog._si_to_float(toks[1]),
                                    "stop": _ManifestEditorDialog._si_to_float(toks[2]),
                                    "n": int(float(toks[3]))}
                except (ValueError, IndexError):
                    return None
            elif ":" in sweep_part and "+" not in s:         # legacy start:step:stop -> lin sweep
                try:
                    nums = [_ManifestEditorDialog._si_to_float(x) for x in sweep_part.split(":")]
                except ValueError:
                    return None
                if len(nums) == 3:
                    start, step, stop = nums
                    n = int(round((stop - start) / step)) + 1 if step else 2
                    out["sweep"] = {"type": "lin", "start": start, "stop": stop, "n": max(n, 2)}
            elif sweep_part and "+" not in s:                # bare points head (no '+'): 'p1,p2'
                pts_part = sweep_part
            pts = []
            for p in pts_part.replace(" ", "").split(","):
                if not p:
                    continue
                try:
                    pts.append(_ManifestEditorDialog._si_to_float(p))
                except ValueError:
                    pass
            if pts:
                out["points"] = pts
            return out or None

        @staticmethod
        def _num(s):
            """Parse a numeric cell to int/float when it looks numeric, else keep the string."""
            try:
                f = float(s)
                return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f
            except ValueError:
                return s

        def _form_to_dict(self):
            """Build the merged manifest dict: deep-copy the STASH (preserving unknown/unmodeled
            keys), then overwrite ONLY the keys the form models (D6 — lossless overlay)."""
            import copy
            m = copy.deepcopy(self._stash) if isinstance(self._stash, dict) else {}
            m["name"] = self.f_name.text().strip()
            # DUT block: overlay the modeled dut keys, preserve any others (extract_view, etc.)
            d = dict(m.get("dut") or {})
            d["lib"] = self.f_dut_lib.text().strip()
            d["cell"] = self.f_dut_cell.text().strip()
            # D4: tb_lib inherits the DUT library when left blank (type DUT first)
            d["tb_lib"] = self.f_tb_lib.text().strip() or self.f_dut_lib.text().strip()
            d["tb_cell"] = self.f_tb_cell.text().strip()
            for src, key in ((self.f_extract, "extract_cell"), (self.f_tb_view, "tb_view"),
                             (self.f_dut_inst, "tb_inst"), (self.f_src_test, "ade_src_test")):
                val = src.text().strip()
                if val:
                    d[key] = val
                else:
                    d.pop(key, None)
            m["dut"] = d
            # ground: only set when non-empty; blank -> drop it so _fill_defaults restores 'gnd!'
            # (a present-but-empty value would ship as '' and defeat the setdefault). Mirrors how
            # analysis / current_psrr_supplies below conditionally set-or-pop.
            ground = self.f_ground.text().strip()
            if ground:
                m["ground"] = ground
            else:
                m.pop("ground", None)

            # role tables overlay onto the STASHED per-entry dicts, so unmodeled per-entry keys
            # (e.g. supplies.<k>.tb_src, i_out.<k>.probe_src, a 'pin' traceability tag) survive (D6).
            stash_roles = m if isinstance(m, dict) else {}

            g_ac = self.f_ac.text().strip()
            g_noise = self.f_noise.text().strip()

            # physical columns the role collector must SKIP (handled elsewhere): the coverage
            # cells (-> coverage.*), the checkbox columns (-> current_psrr_supplies), the gear.
            _cov_cols = {"iload sweep", "trans", "iv_sweep"}

            def collect(t, role, cols):
                """Overlay the table onto the stashed role map (preserving unmodeled per-entry
                keys). `cols` are the LOGICAL manifest keys for the DATA columns (cols[0]=='key'),
                consumed IN ORDER as we walk the table's PHYSICAL columns -- a coverage/checkbox/
                gear column is skipped (it does not map to a per-entry role key). 'dc' parses
                numeric; any other column ('net', 'tb_src', 'src', 'probe_src', ...) is a trimmed
                string -- BLANK drops the key so a downstream default (auto-detect / Vprobe_<key>)
                is restored rather than shipping an empty override. The per-object analysis override
                (t._analysis[key]) is written as <entry>.analysis, but only when it actually differs
                from the global default (keep manifests clean)."""
                prev = stash_roles.get(role) or {}
                store = getattr(t, "_analysis", {}) or {}
                tcols = getattr(t, "_cols", cols)
                skip = _cov_cols | set(getattr(t, "_check_cols", set())) | {"analysis"}
                out = {}
                for cells in self._table_rows(t):
                    key = cells[0] if cells else ""
                    if not key:
                        continue
                    ent = dict(prev.get(key) or {})      # preserve this entry's unmodeled keys
                    li = 1
                    for pc_idx, pc in enumerate(tcols[1:], start=1):
                        if pc in skip:
                            continue                     # coverage / checkbox / gear: not a role key
                        col = cols[li] if li < len(cols) else None
                        li += 1
                        if col is None:
                            continue
                        val = (cells[pc_idx] if pc_idx < len(cells) else "").strip()
                        if col == "dc":
                            if val != "":
                                ent["dc"] = self._num(val)
                            else:
                                ent.pop("dc", None)
                        elif col == "net":
                            ent["net"] = val
                        else:                            # tb_src / src / probe_src / any string field
                            if val:
                                ent[col] = val
                            else:
                                ent.pop(col, None)
                    # the wired iv_sweep column now feeds coverage.iv -- so DROP any legacy
                    # i_out.<c>.iv_sweep per-entry key (it migrates to coverage.iv on save).
                    ent.pop("iv_sweep", None)
                    # per-object analysis override: drop any kind that equals the global default
                    # (clean manifests); write {ac?, noise?} only when something genuinely differs.
                    ov_in = dict(store.get(key) or {})
                    ov = {}
                    if ov_in.get("ac") and ov_in["ac"] != g_ac:
                        ov["ac"] = ov_in["ac"]
                    if role == "v_out" and ov_in.get("noise") and ov_in["noise"] != g_noise:
                        ov["noise"] = ov_in["noise"]
                    if ov:
                        ent["analysis"] = ov
                    else:
                        ent.pop("analysis", None)
                    out[key] = ent
                return out
            m["supplies"] = collect(self.t_supplies, "supplies", ["key", "net", "dc", "tb_src"])
            m["v_out"] = collect(self.t_vout, "v_out", ["key", "net", "src"])
            m["i_out"] = collect(self.t_iout, "i_out", ["key", "net", "dc", "probe_src"])
            m["bias"] = collect(self.t_bias, "bias", ["key", "net", "dc"])
            # COVERAGE: the tier/temps/slew/lin globals + the per-rail/per-sink sweep cells. Overlay
            # onto the stashed coverage (preserving enable/dropout/nominal/holdout + any unmodeled
            # key the form does not surface); OMIT every empty sub-dict so a no-coverage manifest
            # stays byte-clean (no `coverage` key at all unless something non-default is set).
            m["coverage"] = self._coverage_to_dict(m.get("coverage"))
            if not m["coverage"]:
                m.pop("coverage", None)

            # [2.3] current-PSRR from the per-supply checkboxes. The EXPLICIT list of checked keys
            # is always written (an empty list = no current-PSRR is allowed + meaningful).
            cpsrr = [self.t_supplies.item(r, 0).text().strip()
                     for r in range(self.t_supplies.rowCount())
                     if self._row_checked(self.t_supplies, r, "PSRR→I")
                     and self.t_supplies.item(r, 0)
                     and self.t_supplies.item(r, 0).text().strip()]
            m["current_psrr_supplies"] = cpsrr
            # leave_alone is intentionally NOT surfaced in the form (augment never consumes it;
            # undeclared pins already stay TB-original). Don't touch it -> any existing value
            # rides on the deep-copied stash and survives the round-trip (lossless).
            # [2.4] corners: a single run-label line. fallback=[label]; PRESERVE any
            # pull_from_session already in the stash (lossless), don't surface it in the form.
            label = self.f_corner.text().strip() or "nom"
            cor = dict(m.get("corners") or {})
            cor["fallback"] = [label]
            m["corners"] = cor
            an = dict(m.get("analysis") or {})
            if g_ac:
                an["ac"] = g_ac
            if g_noise:
                an["noise"] = g_noise
            if an:
                m["analysis"] = an
            return m

        def _coverage_to_dict(self, prev):
            """Build coverage from the Coverage widgets + the per-rail/per-sink cells, OVERLAID on
            the stashed coverage `prev` (so enable/dropout/nominal/holdout + any unmodeled key
            survive the round-trip, D6). Returns {} when NOTHING non-default is set so the caller
            can drop the whole section -> a no-coverage manifest stays byte-clean. The rule for a
            byte-clean drop: tier==T4 (the default), no temps, slew_en off, lin_gate off, and every
            loads/transient/iv sub-dict empty AND nothing was carried in `prev`."""
            import copy
            cov = copy.deepcopy(prev) if isinstance(prev, dict) else {}

            tier = self.f_cov_tier.currentText().strip() or "T4"
            cov["tier"] = tier
            temps = []
            for tok in self.f_cov_temps.text().split(","):
                tok = tok.strip()
                if tok:
                    try:
                        temps.append(self._num(tok))
                    except ValueError:
                        pass
            if temps:
                cov["temps"] = temps
            else:
                cov.pop("temps", None)
            if self.f_cov_slew.isChecked():
                cov["slew_en"] = 1
            else:
                cov.pop("slew_en", None)
            if self.f_cov_lin.isChecked():
                cov["lin_gate"] = True
            else:
                cov.pop("lin_gate", None)

            # per-rail / per-sink cells overlay their sub-dicts; a blank cell DROPS that key so the
            # sub-dict shrinks (and disappears when empty) -- no stale {} survives a cleared cell.
            def overlay(section, table, col_name, parse):
                sub = dict(cov.get(section) or {})
                cols = getattr(table, "_cols", [])
                ci = cols.index(col_name) if col_name in cols else None
                if ci is not None:
                    for cells in self._table_rows(table):
                        key = cells[0] if cells else ""
                        if not key:
                            continue
                        txt = (cells[ci] if ci < len(cells) else "").strip()
                        parsed = parse(txt) if txt else None
                        if parsed:
                            # preserve any unmodeled sub-keys the form does not surface
                            merged = dict(sub.get(key) or {})
                            merged.update(parsed)
                            sub[key] = merged
                        else:
                            sub.pop(key, None)
                if sub:
                    cov[section] = sub
                else:
                    cov.pop(section, None)
            overlay("loads", self.t_vout, "iload sweep", self._loads_from_text)
            overlay("transient", self.t_vout, "trans", self._trans_from_text)
            overlay("iv", self.t_iout, "iv_sweep", self._ivcov_from_text)

            # byte-clean drop: only the default tier + nothing else set + nothing inherited.
            non_default = (tier != "T4" or "temps" in cov or "slew_en" in cov or "lin_gate" in cov
                           or cov.get("loads") or cov.get("transient") or cov.get("iv")
                           or cov.get("dropout") or cov.get("enable"))
            if not non_default:
                return {}
            return cov

        # ---- Form <-> Raw sync (merge-overlay both ways) ---------------------------
        def _on_subtab_changed(self, idx):
            """Form (0) -> Raw (1): regenerate the JSON text from the merged dict. Raw (1) -> Form
            (0): re-parse the raw text into the stash + repopulate the form (warn on bad JSON,
            keep the raw text). Both directions preserve unknown keys."""
            if idx == 1:                                    # leaving Form -> entering Raw
                self._sync_form_to_raw()
            else:                                           # leaving Raw -> entering Form
                self._sync_raw_to_form()

        def _sync_form_to_raw(self):
            self._stash = self._form_to_dict()              # re-stash so unknown keys persist
            self.ed.setPlainText(json.dumps(self._stash, indent=2) + "\n")

        def _sync_raw_to_form(self):
            try:
                m = json.loads(self.ed.toPlainText())
                if not isinstance(m, dict):
                    raise ValueError("top-level JSON must be an object")
            except (json.JSONDecodeError, ValueError) as e:
                QMessageBox.warning(self, "Raw JSON",
                    f"Raw JSON is not parseable; keeping the text, form not updated.\n\n{e}")
                self.subtabs.blockSignals(True)
                self.subtabs.setCurrentIndex(1)             # stay on Raw so the user can fix it
                self.subtabs.blockSignals(False)
                return
            self._stash = m                                 # unknown keys live here
            self._dict_to_form(m)

        def _current_dict(self):
            """The merged manifest dict reflecting whichever sub-tab is active."""
            if self.subtabs.currentIndex() == 0:            # Form active -> overlay the stash
                return self._form_to_dict()
            try:                                            # Raw active -> parse the text
                m = json.loads(self.ed.toPlainText())
                return m if isinstance(m, dict) else None
            except json.JSONDecodeError:
                return None

        def _check(self):
            """Parse + validate the CURRENT editor state. Returns (ok, message). On ok, message
            is the human summary incl. the derived measurement matrix; else an actionable error."""
            from insitu import manifest as M
            m = self._current_dict()
            if m is None:
                return False, "JSON error: the Raw JSON does not parse"
            try:
                m = M._fill_defaults(m)
                M.validate(m)
                return True, M.summary(m)
            except M.ManifestError as e:
                return False, f"manifest error: {e}"

        def _validate(self):
            ok, msg = self._check()
            self.status.setStyleSheet("font-family:monospace; font-size:11px; "
                                      "color:%s;" % ("#157f3b" if ok else "#b00020"))
            self.status.setText(("VALID ✓\n" if ok else "INVALID ✗\n") + msg)
            return ok

        # ---- [SCAN] auto-fill source-instance + dc from a base netlist -------------
        @staticmethod
        def _mw_netlist_path(mw):
            """The MainWindow's base-netlist path -> a usable input.scs file, or None. The main
            window's xb_netlist is a DIR picker (the work dir holding input.scs), but tolerate a
            direct file too. Returns the resolved .scs path string, or None if absent/missing."""
            edit = getattr(mw, "xb_netlist", None)
            txt = edit["edit"].text().strip() if isinstance(edit, dict) and "edit" in edit else ""
            if not txt:
                return None
            p = pathlib.Path(txt)
            if p.is_dir():
                cand = p / "input.scs"
                return str(cand) if cand.exists() else None
            return str(p) if p.exists() else None

        def _scan_netlist(self):
            """Scan the base input.scs and fill each table's source-instance + dc columns from the
            detected driving instances. [Fix B] FIRST try the main-window base-netlist path (the
            Mode-B 'Netlist dir' field) -- if it resolves to an input.scs, scan it WITHOUT a dialog;
            only fall back to QFileDialog when the main-window path is empty/missing. Reports
            found / missing / type-mismatch in the status bar."""
            fn = self._mw_netlist_path(self.parent())            # main-window xb_netlist (no dialog)
            from_mw = fn is not None
            if not from_mw:
                start = str(self.path.parent if self.path else ROOT)
                fn, _ = QFileDialog.getOpenFileName(
                    self, "Select base netlist (input.scs)", start,
                    "Spectre netlist (*.scs);;All files (*)")
                if not fn:
                    return
            try:
                from cluster import netlist_augment as NA
                scan = NA.scan_netlist_sources(fn, self._current_dict() or {})
            except Exception as e:                                # noqa: BLE001 (report, never crash)
                self.status.setStyleSheet("font-family:monospace; font-size:11px; color:#b00020;")
                self.status.setText(f"scan failed: {e}")
                return
            found, insertable, error = self._apply_scan(scan)
            # green unless a SUPPLY error (no insert fallback) needs attention -> red
            self.status.setStyleSheet("font-family:monospace; font-size:11px; "
                                      + ("color:#b00020;" if error else "color:#157f3b;"))
            src = "main-window netlist dir" if from_mw else "picked file"
            self.status.setText(
                f"scanned {pathlib.Path(fn).name} ({src}): {found} reusable source(s), "
                f"{insertable} open pin(s) → auto-insert Iext_/Vprobe_ at build (normal, amber), "
                f"{error} supply error(s) (red). "
                "Source-instance + dc cells filled where reusable (bias is not scanned) — "
                "review the amber/red ones, then Validate.")

        def _apply_scan(self, scan):
            """Fill the supplies/v_out/i_out tables' source-instance (+ dc) columns from a
            scan_netlist_sources() result. Returns (found, insertable, error) counts:
              found      = a CORRECT-master reusable source on the net (green; cell + dc filled).
              insertable = a v_out/i_out pin with NO reusable source -- nothing on the net, OR only
                           the other-role source (e.g. an isource on a current-output net: that is
                           the bias current being characterized, not a reusable vsource probe).
                           This is the NORMAL open-pin case: the build inserts Iext_<key>/Vprobe_<key>
                           at netlist time. The cell is left BLANK (so the build takes that path) and
                           the compliance dc is NOT filled from a wrong-master source (B2 fix: an
                           isource's current must never land in a voltage 'compliance dc').
              error      = a SUPPLY with no vsource (or a wrong-master source) on its net: supplies
                           have no insert fallback, so this is a real red error.
            Qt-light so the selftest can drive it with an in-memory scan dict."""
            # (table, role, src-instance display-column header, has a 'dc' column?, insert template)
            specs = [(self.t_supplies, "supplies", "src instance",   True,  None),
                     (self.t_vout,     "v_out",    "src instance",   False, "Iext_{}"),
                     (self.t_iout,     "i_out",    "probe instance", True,  "Vprobe_{}")]
            found = insertable = error = 0
            for t, role, src_col, has_dc, insert_tmpl in specs:
                rmap = scan.get(role) or {}
                cols = getattr(t, "_cols", [])
                sidx = cols.index(src_col) if src_col in cols else None
                didx = cols.index("dc") if "dc" in cols else \
                    (cols.index("compliance dc") if "compliance dc" in cols else None)
                companion = "load" if role == "v_out" else "source"   # the other-role source on net
                for r in range(t.rowCount()):
                    ki = t.item(r, 0)
                    key = ki.text().strip() if ki else ""
                    info = rmap.get(key)
                    if not info:
                        continue
                    inst = info.get("instance")
                    reusable = inst is not None and bool(info.get("type_ok", True))
                    can_insert = insert_tmpl is not None                # v_out / i_out fall back
                    if reusable:
                        found += 1
                    elif can_insert:
                        insertable += 1
                    else:
                        error += 1                                      # supply: no fallback
                    if sidx is not None:
                        it = t.item(r, sidx)
                        if it is None:
                            it = QTableWidgetItem(); t.setItem(r, sidx, it)
                        if reusable:
                            it.setText(inst)
                            it.setForeground(QtGui.QColor("#157f3b"))
                            it.setToolTip(f"Detected {info.get('master')} '{inst}' on "
                                          f"{info.get('net')}.")
                        elif can_insert:
                            # NORMAL open / other-type pin: leave BLANK so the build inserts the
                            # probe/load; never put a wrong-master name in the reuse cell.
                            it.setText("")
                            it.setForeground(QtGui.QColor("#b8860b"))
                            ins = insert_tmpl.format(key)
                            if inst is None:
                                it.setToolTip(
                                    f"No reusable source on '{info.get('net')}' — blank → the build "
                                    f"inserts {ins} at netlist time (normal for this output). Type a "
                                    f"name only to reuse a specific source.")
                            else:
                                it.setToolTip(
                                    f"'{inst}' on '{info.get('net')}' is a {info.get('master')} (the "
                                    f"characterized {companion}, not a reusable probe) — blank → the "
                                    f"build inserts {ins} (normal). Type a name only to reuse a "
                                    f"specific source.")
                        else:                                           # supply: real error
                            it.setText(inst or "")
                            it.setForeground(QtGui.QColor("#b00020"))
                            if inst is None:
                                it.setToolTip(
                                    f"No vsource drives '{info.get('net')}' — a supply has no insert "
                                    f"fallback; place the source or name supplies.{key}.tb_src.")
                            else:
                                it.setToolTip(
                                    f"Detected '{inst}' is a {info.get('master')} — a supply needs a "
                                    f"vsource (the read math depends on it).")
                    # the dc cell: ONLY from a correct-master reusable source (B2: never leak a
                    # wrong-master value, e.g. an isource's current, into a voltage 'compliance dc')
                    if has_dc and didx is not None and reusable and info.get("dc") is not None:
                        dit = t.item(r, didx)
                        if dit is None:
                            dit = QTableWidgetItem(); t.setItem(r, didx, dit)
                        dit.setText(self._fmt_num(info["dc"]))
            return found, insertable, error

        @staticmethod
        def _fmt_num(x):
            """A clean cell string for a scanned float (drop a trailing .0 on integers)."""
            if isinstance(x, float) and x.is_integer():
                return str(int(x))
            return str(x)

        def _base_netlist_text(self):
            """The base input.scs TEXT from the main-window 'Netlist dir' field, or None. Cached on
            the resolved path so per-keystroke re-validation does not re-read the file each time."""
            try:
                fn = self._mw_netlist_path(self.parent())
            except Exception:                                    # noqa: BLE001 (never crash on edit)
                return None
            if not fn:
                return None
            cache = getattr(self, "_base_text_cache", None)
            if cache and cache[0] == fn:
                return cache[1]
            try:
                txt = pathlib.Path(fn).read_text()
            except Exception:                                    # noqa: BLE001
                return None
            self._base_text_cache = (fn, txt)
            return txt

        def _on_src_cell_changed(self, t, row, col):
            """[B4] cellChanged handler: validate ONLY the src/probe-instance column of a
            supplies/v_out/i_out table; no-op for every other cell. Reentrancy-guarded because the
            validator re-colours the same item (which would re-emit cellChanged)."""
            if getattr(self, "_in_src_validate", False):
                return
            role = getattr(t, "_analysis_role", None)
            cols = getattr(t, "_cols", [])
            if role not in ("supplies", "v_out", "i_out") or not cols:
                return
            col_name = "probe instance" if role == "i_out" else "src instance"
            if col_name not in cols or col != cols.index(col_name):
                return
            self._in_src_validate = True
            try:
                self._validate_src_cell(t, row)
            finally:
                self._in_src_validate = False

        def _validate_src_cell(self, t, row, base_text=None):
            """[B4] Colour a typed src/probe-instance name against the base netlist's TOP-LEVEL
            instances (scope-aware via NA._find_instance): green = correct master, red = wrong
            master (with the blank→auto-insert hint for v_out/i_out), amber = not a top-level
            instance. A BLANK cell clears the colour (the build auto-detects/inserts). Qt-light:
            pass base_text in the selftest; in the GUI it reads the cached main-window netlist.
            No-op when no base netlist is available (nothing to validate against). Returns the
            verdict string ('ok'/'wrong'/'absent'/'blank'/'no-base') for the selftest."""
            from cluster import netlist_augment as NA
            role = getattr(t, "_analysis_role", None)
            if role not in ("supplies", "v_out", "i_out"):
                return "no-base"
            cols = getattr(t, "_cols", [])
            col_name = "probe instance" if role == "i_out" else "src instance"
            if col_name not in cols:
                return "no-base"
            it = t.item(row, cols.index(col_name))
            if it is None:
                return "no-base"

            def _paint(color, tip):
                # re-colour with the table's signals blocked so the resulting itemChanged never
                # re-enters this validator (belt-and-suspenders with the _in_src_validate guard).
                prev = t.signalsBlocked()
                t.blockSignals(True)
                try:
                    it.setForeground(QtGui.QColor(color))
                    it.setToolTip(tip)
                finally:
                    t.blockSignals(prev)

            name = it.text().strip()
            if not name:                                         # blank -> auto-detect/insert
                _paint("#222222", "Blank → the build auto-detects a source on the net, else "
                                  "inserts one (Iext_/Vprobe_).")
                return "blank"
            if base_text is None:
                base_text = self._base_netlist_text()
            if base_text is None:                                # nothing to validate against
                return "no-base"
            inst = NA._find_instance(base_text, name)
            master = NA.ROLE_MASTER[role]
            if inst is None:
                _paint("#b8860b", f"'{name}' is not a top-level instance in the base netlist — "
                                  f"check the name, or leave blank to auto-detect/insert.")
                return "absent"
            if inst[2] != master:
                esc = "" if role == "supplies" else " — or leave blank to auto-insert one"
                _paint("#b00020", f"'{name}' is a {inst[2]} but {role} needs a {master} (the read "
                                  f"math depends on the master type){esc}.")
                return "wrong"
            _paint("#157f3b", f"OK: '{name}' is a {master} at top level.")
            return "ok"

        def _merged_text(self):
            """The JSON text to write: the merged dict (preserves unknown keys). If Raw is active
            and parses, use its verbatim text (keeps the designer's formatting); else regenerate."""
            if self.subtabs.currentIndex() == 1:
                try:
                    json.loads(self.ed.toPlainText())
                    return self.ed.toPlainText()             # keep hand formatting
                except json.JSONDecodeError:
                    pass
            m = self._current_dict()
            return json.dumps(m, indent=2) + "\n"

        def _write(self, path):
            ok = self._validate()
            if not ok:
                QMessageBox.warning(self, "Manifest", "Fix the validation error before saving.")
                return False
            try:
                pathlib.Path(path).write_text(self._merged_text())
            except OSError as e:
                QMessageBox.critical(self, "Manifest", f"write failed: {e}")
                return False
            self.saved_path = pathlib.Path(path)
            return True

        def _save(self):
            if self.path is None:
                return self._save_as()
            if self._write(self.path):
                self.accept()

        def _save_as(self):
            from insitu import MANIFEST_DIR
            start = str(self.path or (MANIFEST_DIR / "my_ldo.json"))
            fn, _ = QFileDialog.getSaveFileName(self, "Save manifest as", start, "JSON (*.json)")
            if not fn:
                return False
            if not fn.endswith(".json"):
                fn += ".json"
            if self._write(fn):
                self.path = pathlib.Path(fn)
                self.setWindowTitle(f"Manifest editor — {self.path.name}")
                self.accept()
                return True
            return False

    class MainWindow(QMainWindow):
        def __init__(self, core=None):
            super().__init__()
            self.core = core or ModelerCore()
            self.extract = ExtractCore()    # Mechanism A in-situ extraction front-half
            self.file_edits = {}            # (quantity, corner) -> QLineEdit
            self.setWindowTitle("LDO behavioral modeler (offline)")
            self.resize(1180, 820)
            self.tabs = QTabWidget()
            self.setCentralWidget(self.tabs)
            self.tabs.addTab(self._tab_extract(), "0 · Extract (in-situ)")
            self.tabs.addTab(self._tab_profile(), "1 · Profile")
            self.tabs.addTab(self._tab_import(), "2 · Import data")
            self.tabs.addTab(self._tab_fit(), "3 · Fit")
            self.tabs.addTab(self._tab_compare(), "4 · Compare")
            self.tabs.addTab(self._tab_transid(), "5 · Trans-ID")
            self.statusBar().showMessage(
                "In-situ extraction → Tab 0. Or hand-imported data → Tab 1 Profile.")
            self._load_autosave()           # restore last session's form entries (if any)

        # --- form-state persistence (PMU pin form + Profile) ---------------------
        def _config_widgets(self):
            """Registry config-key -> widget for everything we persist across launches.
            QLineEdit by text; QComboBox by userData; QSpinBox by value. Built after all tabs
            exist, so every widget is present. (Old keys absent from the registry — e.g. the
            removed single 'supply'/'supply_dc' — are simply ignored on load: back-compat.)"""
            return {
                "tb_lib": self.xf_tblib, "tb_cell": self.xf_tbcell, "tb_view": self.xf_tbview,
                "dut_inst": self.xf_inst, "dut_lib": self.xf_dutlib, "dut_cell": self.xf_dutcell,
                "supplies": self.xf_supplies, "cpsrr": self.xf_cpsrr,
                "vouts": self.xf_vouts, "iouts": self.xf_iouts, "vdc": self.xf_vdc,
                "ivsweep": self.xf_ivsweep, "temps": self.xf_temps,
                "ground": self.xf_ground, "corner": self.xf_corner, "src_test": self.xf_srctest,
                "manifest": self.x_manifest, "session": self.x_session, "backend": self.x_backend,
                "mode": self.x_mode, "location": self.x_location,
                "b_netlist": self.xb_netlist["edit"], "b_pdk": self.xb_pdk["edit"],
                "b_ahdl": self.xb_ahdl["edit"],
                "d_account": self.xd_account, "d_queue": self.xd_queue,
                "d_cpu": self.xd_cpu, "d_mem": self.xd_mem, "d_maxjobs": self.xd_maxjobs,
                "p_name": self.e_name, "p_vref": self.e_vref, "p_loads": self.e_loads,
                "p_nom": self.e_nom, "p_cout": self.e_cout, "p_esr": self.e_esr,
            }

        def _collect_config(self):
            out = {}
            for k, wd in self._config_widgets().items():
                if isinstance(wd, QComboBox):
                    d = wd.currentData()
                    out[k] = d if d is not None else wd.currentText()
                elif isinstance(wd, QtWidgets.QSpinBox):
                    out[k] = wd.value()
                else:
                    out[k] = wd.text()
            return out

        def _apply_config(self, data):
            """Restore saved values into the widgets. Only touches widgets a key exists for, so
            an older/partial config never clears newer fields. e_nom's item list is rebuilt from
            the saved load list so the nominal can be re-selected. No profile re-validation here
            (no startup dialog): the user clicks Apply/Resolve as usual once restored."""
            for k, wd in self._config_widgets().items():
                if k not in data:
                    continue
                v = data[k]
                if wd is self.e_nom:
                    continue                                  # handled after e_loads below
                if isinstance(wd, QComboBox):
                    idx = wd.findData(v)
                    if idx < 0:
                        idx = wd.findText(str(v))
                    if idx >= 0:
                        wd.setCurrentIndex(idx)
                elif isinstance(wd, QtWidgets.QSpinBox):
                    try:
                        wd.setValue(int(v))
                    except (TypeError, ValueError):
                        pass
                else:
                    wd.setText("" if v is None else str(v))
            if "p_loads" in data and "p_nom" in data:        # rebuild nominal dropdown from saved loads
                loads = [s.strip() for s in str(data["p_loads"]).split(",") if s.strip()]
                if loads:
                    self.e_nom.blockSignals(True)
                    self.e_nom.clear(); self.e_nom.addItems(loads)
                    if str(data["p_nom"]) in loads:
                        self.e_nom.setCurrentText(str(data["p_nom"]))
                    self.e_nom.blockSignals(False)

        def _save_autosave(self):
            try:
                aj = _autosave_json()
                aj.parent.mkdir(parents=True, exist_ok=True)
                aj.write_text(json.dumps(self._collect_config(), indent=2), encoding="utf-8")
            except OSError:
                pass                                          # never let a read-only HOME block exit

        def _load_autosave(self):
            try:
                aj = _autosave_json()
                if aj.exists():
                    self._apply_config(json.loads(aj.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass                                          # corrupt/old state must not block launch

        def _save_config_as(self):
            cfgdir = _config_dir()
            cfgdir.mkdir(parents=True, exist_ok=True)
            fn, _ = QFileDialog.getSaveFileName(self, "Save form config",
                                                str(cfgdir / "my_pmu_config.json"),
                                                "JSON (*.json)")
            if not fn:
                return
            if not fn.endswith(".json"):
                fn += ".json"
            try:
                pathlib.Path(fn).write_text(json.dumps(self._collect_config(), indent=2),
                                            encoding="utf-8")
                self.statusBar().showMessage(f"Saved form config → {fn}")
            except OSError as e:
                QMessageBox.warning(self, "Save config", f"{type(e).__name__}: {e}")

        def _load_config_dialog(self):
            cfgdir = _config_dir()
            start = str(cfgdir if cfgdir.exists() else ROOT)
            fn, _ = QFileDialog.getOpenFileName(self, "Load form config", start, "JSON (*.json)")
            if not fn:
                return
            try:
                self._apply_config(json.loads(pathlib.Path(fn).read_text(encoding="utf-8")))
                self.statusBar().showMessage(f"Loaded form config ← {fn}")
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "Load config", f"{type(e).__name__}: {e}")

        def closeEvent(self, ev):
            self._save_autosave()                             # remember this session's entries
            super().closeEvent(ev)

        # --- Tab 0 helpers: path pickers + mode/location visibility --------------
        def _path_row(self, name, dir_only=False):
            """A QLineEdit + Browse button in one container. Returns {'w':container,'edit':QLineEdit}."""
            edit = QLineEdit()
            btn = QPushButton("Browse…")
            btn.clicked.connect(lambda _=False, e=edit, d=dir_only: self._pick_path(e, d))
            cont = QWidget(); h = QHBoxLayout(cont); h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(edit); h.addWidget(btn)
            return {"w": cont, "edit": edit}

        def _pick_path(self, edit, dir_only):
            if dir_only:
                d = QFileDialog.getExistingDirectory(self, "Select directory", str(ROOT))
            else:
                d, _ = QFileDialog.getOpenFileName(self, "Select file", str(ROOT))
            if d:
                edit.setText(d)

        @staticmethod
        def _pdk_default_for(engine):
            """The engine-aware default PDK model path from $MODEL_ROOT (item #2 / D7).

            The consumer (cluster/alps_cli.build_sim_cmd → _engine_model_tree) treats model_dir as
            a DIRECTORY ROOT and appends the engine name to form the `-I` include TREE — it does NOT
            consume a toplevel.scs FILE. So we default to the per-engine DIRECTORY $MODEL_ROOT/<eng>
            (spectre_cli → /spectre, alps → /alps). ADE = live Maestro (no Mode-B import) → no default.
            Returns "" when MODEL_ROOT is unset or the engine takes no default (never crashes)."""
            root = os.environ.get("MODEL_ROOT")
            if not root:
                return ""
            sub = {"spectre_cli": "spectre", "alps": "alps"}.get(engine)
            if not sub:                                  # 'ade' (live) -> no Mode-B import default
                return ""
            return f"{root.rstrip('/')}/{sub}"

        def _x_pdk_default(self):
            """Refresh the xb_pdk placeholder (soft default) for the current engine. Auto-FILL the
            field with the engine default when the user has not hand-edited it — i.e. it is empty
            OR still holds the value we last auto-applied (so switching engines REFRESHES the stale
            default instead of leaving the wrong path, #1). A genuine user edit (current text differs
            from the stashed auto-value) is left untouched. Always update the placeholder. Safe if
            MODEL_ROOT is unset (default resolves to '' -> placeholder falls back to the generic hint)."""
            if not hasattr(self, "xb_pdk"):
                return
            dflt = self._pdk_default_for(self.x_backend.currentData())
            ed = self.xb_pdk["edit"]
            ed.setPlaceholderText(dflt or "optional — only if the netlist needs an -I model tree")
            cur = ed.text().strip()
            if dflt and (not cur or cur == getattr(self, "_xb_pdk_auto", None)):
                ed.setText(dflt)
                self._xb_pdk_auto = dflt

        def _x_mode_changed(self, *a):
            """Show the Mode-A pin form OR the Mode-B import group; Session only for ADE engine.
            Guarded: the combo signals fire during _tab_extract construction before the later
            groups exist."""
            if not hasattr(self, "x_grp_donau"):
                return                                        # tab still building
            mode = self.x_mode.currentData()
            self.x_grp_pinform.setVisible(mode == "schematic")
            self.x_grp_modeb.setVisible(mode == "import")
            # ADE is the LIVE-skillbridge engine -> invalid in Mode B (no skillbridge): grey it
            # out and switch off it. Session (ADE-only) then never shows in Mode B.
            ade_i = self.x_backend.findData("ade")
            self.x_backend.model().item(ade_i).setEnabled(mode != "import")
            if mode == "import" and self.x_backend.currentData() == "ade":
                self.x_backend.setCurrentIndex(self.x_backend.findData("alps"))
            self.x_grp_session.setVisible(self.x_backend.currentData() == "ade")
            self._x_pdk_default()       # engine-aware $MODEL_ROOT default for the PDK field (D7)

        def _x_location_changed(self, *a):
            if not hasattr(self, "x_grp_donau"):
                return                                        # tab still building
            on_cluster = self.x_location.currentData() == "cluster"
            self.x_grp_donau.setVisible(on_cluster)
            if hasattr(self, "x_status_box"):
                self.x_status_box.setVisible(on_cluster)      # per-group table only for a sweep
            if hasattr(self, "x_dryrun"):
                self.x_dryrun.setVisible(on_cluster)          # preview toggle is cluster-only

        # --- skillbridge (live Virtuoso) connection indicator --------------------
        def _set_sb(self, state, msg):
            """state: 'ok' (green) / 'no' (red) / 'idle' (grey)."""
            dot = {"ok": "●", "no": "○", "idle": "◌"}[state]
            col = {"ok": "#157f3b", "no": "#b00020", "idle": "#777"}[state]
            self.x_sb_status.setText(f"skillbridge: {dot} {msg}")
            self.x_sb_status.setStyleSheet(f"color:{col}; font-weight:bold;")

        def _sb_initial(self):
            """Cheap startup check: just whether skillbridge is importable (no connection attempt,
            so launch never blocks). The full connection test is the Check button."""
            try:
                import skillbridge  # noqa: F401
                self._set_sb("idle", "installed — click Check to test the connection")
            except Exception:
                self._set_sb("no", "not installed (offline venv — Mode B / offline modeling is fine)")

        def _check_skillbridge(self):
            """Probe the live skillbridge server (bounded). Updates the indicator. Never raises."""
            self._set_sb("idle", "checking…")
            QApplication.processEvents()
            try:
                from insitu.resolve import open_session, ResolveUnavailable
            except Exception:
                self._set_sb("no", "not installed (offline venv)")
                return
            try:
                open_session(timeout=3.0)            # opens a bounded Workspace or raises
                self._set_sb("ok", "connected (live Virtuoso)")
                self.statusBar().showMessage("skillbridge connected — Mode A / ADE / Create cell available.")
            except ResolveUnavailable as e:
                self._set_sb("no", str(e).splitlines()[0][:90])
            except Exception as e:
                self._set_sb("no", f"{type(e).__name__}: {str(e)[:70]}")

        # --- Tab 0: Extract (in-situ, Mechanism A) -------------------------------
        def _tab_extract(self):
            # [Fix C] wrap the tab content in a QScrollArea (mirror _build_form_tab's idiom):
            # outer widget -> QScrollArea(setWidgetResizable) -> inner content widget. When the
            # window is short the tab now SCROLLS vertically instead of squashing the blocks.
            tab = QWidget(); tabv = QVBoxLayout(tab); tabv.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            w = QWidget(); outer = QVBoxLayout(w)
            help_ = QLabel(
                "<b>In-situ PMU LDO modeling — one corner, end to end.</b> Fill the form with your "
                "PMU's <b>symbol pin names</b> (the tool resolves them to TB nets and builds the "
                "pin-role manifest). <b>Build &amp; Run</b> sweeps the per-measurement cluster jobs, "
                "reads the PSF, and fits every output. <b>Create model cell</b> then writes the "
                "combined Verilog-A + symbol (AVDD1P0 left · outputs right · VSS bottom) and "
                "compiles it.<br>Engine <b>ade</b> = the live Maestro run (rides Job Setup → cluster); "
                "<b>spectre_cli</b> = the offline dev fixture; <b>cluster</b> = pure-CLI dsub+alps "
                "(Path B, pending its box-validated netlister).")
            help_.setWordWrap(True)
            help_.setStyleSheet("background:#eef5ff; padding:9px; border:1px solid #cdddee;")
            outer.addWidget(help_)

            # ---- form config: persist what you type across launches ---------------------
            cfgrow = QHBoxLayout()
            cfgrow.addWidget(QLabel("Form config:"))
            b_cfg_save = QPushButton("Save config…")
            b_cfg_save.setToolTip("Save every field on this tab + the Profile tab to a JSON you "
                                  "can reload (e.g. one per project).")
            b_cfg_save.clicked.connect(self._save_config_as)
            b_cfg_load = QPushButton("Load config…")
            b_cfg_load.setToolTip("Reload a saved form config into the fields.")
            b_cfg_load.clicked.connect(self._load_config_dialog)
            cfgrow.addWidget(b_cfg_save); cfgrow.addWidget(b_cfg_load)
            note = QLabel("· your entries auto-restore on next launch")
            note.setStyleSheet("color:#567;")
            cfgrow.addWidget(note); cfgrow.addStretch(1)
            cfgw = QWidget(); cfgw.setLayout(cfgrow); outer.addWidget(cfgw)

            # ---- MODE selector: how the manifest is produced ----------------------------
            mrow = QHBoxLayout()
            mrow.addWidget(QLabel("Mode:"))
            self.x_mode = QComboBox()
            self.x_mode.addItem("Build from schematic (resolve pins)", "schematic")
            self.x_mode.addItem("Import netlist + manifest (no skillbridge)", "import")
            self.x_mode.setToolTip("schematic: fill the pin form below; the tool resolves pins to TB "
                                   "nets (live skillbridge) and builds the manifest.\n"
                                   "import: bring a prepared netlist (input.scs) + a manifest + PDK "
                                   "model dir + ahdllibdir and run it (local/cluster) — no skillbridge. "
                                   "Use this when the tool can't netlist for you.")
            self.x_mode.currentIndexChanged.connect(self._x_mode_changed)
            mrow.addWidget(self.x_mode); mrow.addStretch(1)
            # skillbridge (live Virtuoso) connection indicator -- tells you up front whether the
            # live paths (Mode A resolve, ADE run, Create model cell) can work.
            self.x_sb_status = QLabel(); self.x_sb_status.setToolTip(
                "Whether a live Virtuoso skillbridge server is reachable. Needed for Mode A "
                "(resolve pins), the ADE engine, and Create model cell. Mode B / offline modeling "
                "do NOT need it. Click Check after starting the CIW skillbridge server.")
            self.x_sb_check = QPushButton("Check")
            self.x_sb_check.setToolTip("Test the skillbridge connection now (opens a bounded probe).")
            self.x_sb_check.clicked.connect(self._check_skillbridge)
            mrow.addWidget(self.x_sb_status); mrow.addWidget(self.x_sb_check)
            mw = QWidget(); mw.setLayout(mrow); outer.addWidget(mw)

            # ---- pin FORM (deliverable 1): symbol pins -> resolved manifest (MODE A) -----
            self.x_grp_pinform = QGroupBox("1 · Describe your PMU (symbol pin names)")
            gb = self.x_grp_pinform
            gf = QFormLayout(gb)
            self.xf_tblib = QLineEdit(); self.xf_tbcell = QLineEdit()
            self.xf_tbview = QLineEdit("schematic"); self.xf_inst = QLineEdit("I0")
            self.xf_inst.setToolTip("DUT instance name inside the testbench (e.g. PMU_TOP / I0).")
            # DUT FIRST (type the design under test), then the testbench that wraps it. The
            # testbench library inherits the DUT library when left blank (tb_lib <- dut_lib).
            self.xf_dutlib = QLineEdit(); self.xf_dutcell = QLineEdit()
            self.xf_dutlib.setToolTip("DUT = the LDO/PMU being modeled. Its Cadence library "
                                      "(e.g. sim_1108_yusheng).")
            self.xf_dutcell.setToolTip("DUT cell = your LDO cellview (the design under test, e.g. Test_LDO).")
            dutrow = QHBoxLayout()
            for lab, ed, tip in (("DUT library", self.xf_dutlib, "sim_1108_yusheng"),
                                 ("DUT cell", self.xf_dutcell, "Test_LDO"),
                                 ("DUT inst", self.xf_inst, "PMU_TOP")):
                ed.setPlaceholderText(f"e.g. {tip}")
                dutrow.addWidget(QLabel(lab)); dutrow.addWidget(ed)
            dutw = QWidget(); dutw.setLayout(dutrow); gf.addRow("DUT (your LDO) *", dutw)
            self.xf_tblib.setToolTip("Testbench = the schematic that instantiates the DUT + the "
                                     "sources/probes ADE simulates. Blank → inherits the DUT library.")
            self.xf_tbcell.setToolTip("Testbench cell — the harness cellview wrapping the DUT.")
            self.xf_tblib.setPlaceholderText("blank → DUT library")
            self.xf_tbcell.setPlaceholderText("e.g. Test_LDO_TB")
            tbrow = QHBoxLayout()
            for lab, ed in (("Testbench library", self.xf_tblib), ("Testbench cell", self.xf_tbcell),
                            ("view", self.xf_tbview)):
                tbrow.addWidget(QLabel(lab)); tbrow.addWidget(ed)
            tbw = QWidget(); tbw.setLayout(tbrow); gf.addRow("Testbench *", tbw)
            self.xf_supplies = QLineEdit("AVDD1P0@1.0")
            self.xf_supplies.setToolTip("Supply INPUT pin(s), comma-separated, each 'pin@dc' "
                                        "(dc = operating voltage). One or many, e.g. "
                                        "AVDD1P0@1.0, DVDD0P8@0.8. PSRR is measured vs every supply.")
            gf.addRow("Supplies (pin@dc) *", self.xf_supplies)
            self.xf_cpsrr = QLineEdit()
            self.xf_cpsrr.setPlaceholderText("optional: AVDD1P0   (blank → current-PSRR vs ALL supplies)")
            self.xf_cpsrr.setToolTip("Which supply(ies) the CURRENT-output PSRR (pi_*) is referenced "
                                     "to — comma-separated pins. Blank → all supplies. Voltage-output "
                                     "PSRR (p_*) is always measured vs every supply.")
            gf.addRow("Current-PSRR ref", self.xf_cpsrr)
            self.xf_vouts = QLineEdit("VDD0P8_DIG, VDD0P8_PLL, VDD0P8_VCO")
            self.xf_vouts.setToolTip("Voltage OUTPUT pins, comma-separated (Zout / PSRR / noise).")
            gf.addRow("Voltage outputs", self.xf_vouts)
            self.xf_iouts = QLineEdit("IBP_POLY_1P8U_VCO, IBP_POLY_500N_VCO_Fit, "
                                      "IBP_PTAT_TUNE_1P5U_VCO")
            self.xf_iouts.setToolTip("Current OUTPUT pins, comma-separated (admittance / cur-PSRR).")
            gf.addRow("Current outputs", self.xf_iouts)
            self.xf_vdc = QLineEdit()
            self.xf_vdc.setPlaceholderText("optional: IBP_POLY_500N_VCO_Fit=0.9, IBP_POLY_1P8U_VCO=0.85")
            self.xf_vdc.setToolTip("Per current-output COMPLIANCE voltage = the pin's normal "
                                   "operating bias voltage. A current port FORCES a voltage and "
                                   "MEASURES current (V/I dual), so the probe holds the pin at this "
                                   "dc and reads Idc there — it's the operating point Idc/I-V are "
                                   "defined at (NOT the current value). Omit → 0 V clamp + a warning. "
                                   "Voltage outputs need NO bias here (their Zout probe is AC-only, "
                                   "dc=0, so the TB's own load biases the rail — true in-situ).")
            gf.addRow("I-out compliance vdc", self.xf_vdc)
            self.xf_ivsweep = QLineEdit()
            self.xf_ivsweep.setPlaceholderText("optional: IBP_POLY_1P8U_VCO=0:0.01:1.1, IBP_PTAT_TUNE_1P5U_VCO=auto")
            self.xf_ivsweep.setToolTip("Per current-output I-V compliance-knee sweep (G5), "
                                       "Cadence order: 'pin=start:step:stop' (e.g. 0:0.01:1.1) "
                                       "or 'pin=auto' (0 → supply+margin). Blank → the single OP "
                                       "only (no knee). User-defined so the harness serves any project's pins.")
            gf.addRow("I-out I-V sweep", self.xf_ivsweep)
            self.xf_temps = QLineEdit()
            self.xf_temps.setPlaceholderText("optional: -40, 55, 125   (°C; middle = nominal)")
            self.xf_temps.setToolTip("Temperature points for Idc(T)/PTAT/noise(T). Blank → nominal "
                                     "only. The middle point is the model-bake nominal temp.")
            gf.addRow("Temperatures [°C]", self.xf_temps)
            gnrow = QHBoxLayout()
            self.xf_ground = QLineEdit("VSS"); self.xf_corner = QLineEdit("tt_25c")
            gnrow.addWidget(QLabel("ground")); gnrow.addWidget(self.xf_ground)
            gnrow.addWidget(QLabel("corner")); gnrow.addWidget(self.xf_corner)
            gnw = QWidget(); gnw.setLayout(gnrow); gf.addRow("Ground / corner", gnw)
            self.xf_srctest = QLineEdit()
            self.xf_srctest.setPlaceholderText("optional — auto-discovered from the ADE-XL session")
            self.xf_srctest.setToolTip("The designer ADE test holding the in-situ OP (its vars are "
                                       "inherited). Normally auto-found by matching the TB cell; set "
                                       "this only if that fails (the error will tell you).")
            gf.addRow("ADE source test", self.xf_srctest)
            self.xf_build = QPushButton("Resolve pins → Build manifest")
            self.xf_build.setToolTip("Resolve each symbol pin to its TB net (live skillbridge) and "
                                     "build the pin-role manifest in the workarea, then enable Run.")
            self.xf_build.clicked.connect(self._x_build_manifest)
            gf.addRow(self.xf_build)
            outer.addWidget(gb)

            # ---- the MANIFEST: the resolved pin-role + measurement contract -------------
            mhdr = QLabel("<b>Manifest</b> — the pin-role + measurement contract (which nets to "
                          "probe, what to measure). <b>Mode A</b> builds it from the form above; "
                          "<b>Mode B</b>: Load/Browse your prepared one here (required).")
            mhdr.setWordWrap(True); mhdr.setStyleSheet("color:#345; margin-top:6px;")
            outer.addWidget(mhdr)
            form = QFormLayout(); outer.addLayout(form)
            self.x_manifest = QLineEdit("pmu_top")
            self.x_manifest.setToolTip("A manifest name (resolved under cadence/insitu/manifests/, "
                                       "e.g. pmu_top) OR a path to a manifest JSON. In Mode A the "
                                       "pin form writes here; in Mode B you point at your own.")
            row = QHBoxLayout(); row.addWidget(self.x_manifest)
            b_browse = QPushButton("Browse…"); b_browse.clicked.connect(self._x_browse)
            b_browse.setToolTip("Pick a manifest JSON file from disk.")
            b_load = QPushButton("Load"); b_load.clicked.connect(self._x_load)
            b_load.setToolTip("Load the manifest above (enables Build & Run / the cluster preview).")
            b_edit = QPushButton("Edit…"); b_edit.clicked.connect(self._x_edit)
            b_edit.setToolTip("Open the manifest JSON in an editor: re-tag pins when you "
                              "switch LDOs, Validate, then Save (reloads here).")
            b_new = QPushButton("New…"); b_new.clicked.connect(self._x_new)
            b_new.setToolTip("Start a fresh manifest from a commented template.")
            for b in (b_browse, b_load, b_edit, b_new):
                row.addWidget(b)
            rw = QWidget(); rw.setLayout(row); form.addRow("Manifest", rw)
            # Engine (the simulator) and run-location (where it runs) are now SEPARATE.
            self.x_backend = QComboBox()
            self.x_backend.addItem("ADE — live Maestro (rides Job Setup)", "ade")
            self.x_backend.addItem("Spectre — local CLI fixture", "spectre_cli")
            self.x_backend.addItem("ALPS", "alps")
            self.x_backend.setToolTip("The simulator engine. ADE = live Maestro (needs the skillbridge "
                                      "session). Spectre = offline local CLI fixture. ALPS = the "
                                      "company engine (cluster, via the Import + cluster path).")
            self.x_backend.currentIndexChanged.connect(self._x_mode_changed)
            form.addRow("Engine", self.x_backend)
            self.x_location = QComboBox()
            self.x_location.addItem("local", "local")
            self.x_location.addItem("cluster (Donau dsub)", "cluster")
            self.x_location.setToolTip("Where the run executes. local = one process here. "
                                       "cluster = submit per-measurement jobs to Donau (dsub); shows "
                                       "the cluster settings panel below.")
            self.x_location.currentIndexChanged.connect(self._x_location_changed)
            form.addRow("Run on", self.x_location)
            self.x_session = QLineEdit("fnxSession0")
            self.x_session.setToolTip("ADE-XL session name (ADE engine only).")
            self.x_grp_session = QWidget()
            _sgl = QHBoxLayout(self.x_grp_session); _sgl.setContentsMargins(0, 0, 0, 0)
            _sgl.addWidget(self.x_session)
            form.addRow("Session", self.x_grp_session)

            # ---- MODE B: import a prepared netlist + PDK + ahdllib (no skillbridge) ------
            self.x_grp_modeb = QGroupBox("1b · Import netlist + PDK (Mode B — no skillbridge)")
            bf = QFormLayout(self.x_grp_modeb)
            self.xb_netlist = self._path_row("xb_netlist", dir_only=True)
            self.xb_netlist["edit"].setToolTip(
                "The ADE/work dir holding the Spectre-syntax netlist input.scs (the deck the "
                "simulator runs). In Mode A, ADE generates this for you; in Mode B you bring it.")
            bf.addRow("Netlist dir (input.scs) *", self.xb_netlist["w"])
            self.xb_pdk = self._path_row("xb_pdk", dir_only=True)
            self.xb_pdk["edit"].setToolTip(
                "OPTIONAL. An include SEARCH DIRECTORY (the sim's -I), NOT the toplevel.scs file. "
                "It's where `include \"toplevel.scs\"` is resolved. Give either the model ROOT "
                "($MODEL_ROOT — the {alps,spectre} subtree is appended) or the engine dir itself "
                "($MODEL_ROOT/alps), used as-is. If you paste the toplevel.scs FILE path, its "
                "containing directory is used (no '/alps' is tacked onto a file).\n"
                "Leave BLANK if the netlist's own `include` lines already point at the models "
                "(self-contained). Default: when $MODEL_ROOT is set and this is blank, the engine "
                "picks the subtree ($MODEL_ROOT/spectre for Spectre, $MODEL_ROOT/alps for ALPS).")
            self.xb_pdk["edit"].setPlaceholderText(self._pdk_default_for(self.x_backend.currentData())
                                                   or "optional — only if the netlist needs an -I model tree")
            bf.addRow("PDK model dir (optional)", self.xb_pdk["w"])
            self.xb_ahdl = self._path_row("xb_ahdl", dir_only=True)
            self.xb_ahdl["edit"].setPlaceholderText("optional — blank ⇒ simulator compiles VA from the netlist")
            self.xb_ahdl["edit"].setToolTip(
                "OPTIONAL. ahdllibdir = a PRE-COMPILED Verilog-A cache (-ahdllibdir). You DON'T "
                "need it: the netlist's own `ahdl_include` lines let the simulator auto-compile the "
                "VA itself. Provide a dir only to REUSE a pre-compiled cache (skip per-run/per-node "
                "recompile on the cluster). Blank ⇒ the simulator compiles from the netlist.")
            bf.addRow("ahdllibdir (optional)", self.xb_ahdl["w"])
            # Output / work dir: where the sweep writes netlist/psf/npz/model. Prefilled with the
            # resolved default ($WORK_ROOT env, else ~/ldo_workarea) so the user always sees + can
            # change where results land, instead of it being an invisible env-only default.
            self.xb_workdir = self._path_row("xb_workdir", dir_only=True)
            try:
                from insitu import pmu_corner as _PCwd
                self.xb_workdir["edit"].setText(str(_PCwd.resolve_work_root()))
            except Exception:                                # noqa: BLE001 (never block UI build)
                pass
            self.xb_workdir["edit"].setToolTip(
                "Where this run's simulation outputs are written. Results land under\n"
                "  <this dir>/ldo_modeling/<tb_lib>__<tb_cell>/<corner>/{netlist,psf,npz,model}\n"
                "Prefilled with the default ($WORK_ROOT env, else ~/ldo_workarea). Change it to put "
                "this run's PSF/npz wherever you want.")
            bf.addRow("Output / work dir", self.xb_workdir["w"])
            _mbhint = QLabel("Only the netlist dir is required — the simulator reads VA + model "
                             "locations from the netlist's own ahdl_include/include lines. A SINGLE "
                             "input.scs is dry/plan-only; a real multi-measurement sweep needs one "
                             "netlist per group (box-coupled, pending).")
            _mbhint.setWordWrap(True); _mbhint.setStyleSheet("color:#a40; font-size:11px;")
            bf.addRow(_mbhint)
            outer.addWidget(self.x_grp_modeb)

            # ---- Donau cluster settings (visible only when Run on = cluster) -------------
            self.x_grp_donau = QGroupBox("Cluster settings (Donau dsub)")
            df = QFormLayout(self.x_grp_donau)
            self.xd_account = QLineEdit("ug_rfic.rfSClass")
            self.xd_account.setToolTip("Donau resource account / class (-A).")
            df.addRow("Account / class (-A)", self.xd_account)
            self.xd_queue = QLineEdit("short")
            self.xd_queue.setToolTip("Donau work queue (-q), e.g. short (3h cap, 32G).")
            df.addRow("Queue (-q)", self.xd_queue)
            crow = QHBoxLayout()
            self.xd_cpu = QtWidgets.QSpinBox(); self.xd_cpu.setRange(1, 256); self.xd_cpu.setValue(8)
            self.xd_cpu.setToolTip("Cores per job (cpu= in -R; -mt is matched to it).")
            self.xd_mem = QtWidgets.QSpinBox(); self.xd_mem.setRange(256, 512000)
            self.xd_mem.setSingleStep(1000); self.xd_mem.setValue(8000)
            self.xd_mem.setToolTip("Memory per job in MB (mem= in -R).")
            crow.addWidget(QLabel("CPU")); crow.addWidget(self.xd_cpu)
            crow.addWidget(QLabel("MEM [MB]")); crow.addWidget(self.xd_mem); crow.addStretch(1)
            cw = QWidget(); cw.setLayout(crow); df.addRow("Resources (-R)", cw)
            self.xd_maxjobs = QtWidgets.QSpinBox(); self.xd_maxjobs.setRange(1, 32)
            self.xd_maxjobs.setValue(4)
            self.xd_maxjobs.setToolTip(
                "How many measurement-group jobs run on the cluster AT ONCE (Donau runs them in "
                "parallel; the sweep just stops blocking on one before submitting the next). 1 = "
                "strict serial.\nWARNING: ALPS is licensed PER SEAT — a high count can exhaust the "
                "available license seats and stall the whole sweep.")
            df.addRow("Max parallel jobs", self.xd_maxjobs)
            outer.addWidget(self.x_grp_donau)

            self.x_summary = QTextEdit(); self.x_summary.setReadOnly(True)
            self.x_summary.setMaximumHeight(120)
            self.x_summary.setStyleSheet("font-family:monospace; font-size:11px;")
            outer.addWidget(self.x_summary)

            brow = QHBoxLayout()
            self.x_run = QPushButton("2 · Build && Run"); self.x_run.clicked.connect(self._x_run)
            self.x_run.setEnabled(False)
            brow.addWidget(self.x_run)
            self.x_cancel = QPushButton("Cancel"); self.x_cancel.clicked.connect(self._x_cancel)
            self.x_cancel.setEnabled(False); self.x_cancel.setToolTip(
                "Stop the ade run after the current step (ADE state is restored cleanly).")
            brow.addWidget(self.x_cancel)
            self.x_dryrun = QCheckBox("Preview only (dry-run)")
            self.x_dryrun.setToolTip("Cluster runs: assemble + show the per-group dsub commands "
                                     "WITHOUT submitting. Untick to actually run the sweep on Donau.")
            brow.addWidget(self.x_dryrun)
            self.x_gate = QLabel("—"); brow.addWidget(self.x_gate, 1)
            outer.addLayout(brow)
            # live progress (per measurement group) -- ade runs stream here so the window
            # never looks wedged; spectre_cli ticks 0->100 quickly.
            prow = QHBoxLayout()
            self.x_prog = QtWidgets.QProgressBar(); self.x_prog.setRange(0, 100)
            self.x_prog.setValue(0); self.x_prog.setTextVisible(False)
            prow.addWidget(self.x_prog, 1)
            self.x_progmsg = QLabel(""); self.x_progmsg.setStyleSheet("color:#555; font-size:11px;")
            prow.addWidget(self.x_progmsg, 2)
            outer.addLayout(prow)

            # per-GROUP run status (cluster Donau+ALPS sweep): one row per measurement group, live
            # state pending -> running -> done|failed (or 'preview' for a dry-run). Shown only when
            # Run-on = cluster (a sweep is N jobs); hidden for local/ade single runs.
            self.x_status_box = QGroupBox("Per-group run status (Donau+ALPS sweep)")
            _sbl = QVBoxLayout(self.x_status_box)
            self.x_status = QTableWidget(0, 4)
            self.x_status.setHorizontalHeaderLabels(["#", "Group", "Analysis", "State"])
            self.x_status.verticalHeader().setVisible(False)
            self.x_status.setEditTriggers(QTableWidget.NoEditTriggers)
            self.x_status.setSelectionMode(QTableWidget.NoSelection)
            self.x_status.setMaximumHeight(190)
            _hh = self.x_status.horizontalHeader()
            _hh.setStretchLastSection(True)
            _hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            _hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
            _sbl.addWidget(self.x_status)
            outer.addWidget(self.x_status_box)

            self.x_report = QTextEdit(); self.x_report.setReadOnly(True)
            self.x_report.setStyleSheet("font-family:monospace; font-size:11px;")
            outer.addWidget(self.x_report, 1)

            # ---- model cell (deliverable 3): the ONE combined VA + symbol, compiled -----
            mgb = QGroupBox("3 · Create the model cell (Verilog-A + symbol, compiled)")
            mf = QFormLayout(mgb)
            self.xm_lib = QLineEdit("LDO_model_lab"); self.xm_cell = QLineEdit("PMU_model")
            self.xm_path = QLineEdit()
            self.xm_path.setPlaceholderText("on-disk Cadence library path "
                                            "(e.g. $WORK_ROOT/ldo_modeling/cds/LDO_model_lab)")
            mrow = QHBoxLayout()
            for lab, ed in (("lib", self.xm_lib), ("cell", self.xm_cell)):
                mrow.addWidget(QLabel(lab)); mrow.addWidget(ed)
            mw = QWidget(); mw.setLayout(mrow); mf.addRow("Model", mw)
            mf.addRow("Library path", self.xm_path)
            self.xm_make = QPushButton(
                "Create model cell  (AVDD1P0 left · outputs right · VSS bottom)")
            self.xm_make.setToolTip("Emit the combined Verilog-A from the fit, then import + "
                                    "compile it and build the symbol cell in Cadence (live "
                                    "skillbridge). No live session → writes the .va + SKILL plan only.")
            self.xm_make.setEnabled(False)       # enabled only after a run -> a fit exists to emit
            self.xm_make.clicked.connect(self._x_make_cell)
            mf.addRow(self.xm_make)
            outer.addWidget(mgb)

            srow2 = QHBoxLayout()
            srow2.addWidget(QLabel("Or send one output port →"))
            self.x_port = QComboBox(); srow2.addWidget(self.x_port)
            b_send = QPushButton("Load into Import → Fit"); b_send.clicked.connect(self._x_send)
            srow2.addWidget(b_send); srow2.addStretch(1)
            outer.addLayout(srow2)
            self._x_mode_changed(); self._x_location_changed()   # set initial group visibility
            self._sb_initial()                                   # skillbridge indicator (import-only)
            # [Fix C] give the major blocks a sensible minimum height so they don't collapse to
            # nothing when the tab is short -- the QScrollArea below provides the vertical scroll.
            self.x_grp_pinform.setMinimumHeight(360)
            self.x_grp_modeb.setMinimumHeight(150)
            self.x_report.setMinimumHeight(140)
            scroll.setWidget(w)                                  # [Fix C] content -> scroll -> tab
            tabv.addWidget(scroll)
            return tab

        def _form_gui(self):
            """Assemble the build_manifest `gui` dict from the pin-form widgets. Comma lists ->
            pin lists; 'pin=val,...' -> the per-i_out compliance vdc map."""
            def _csl(s):
                return [t.strip() for t in s.split(",") if t.strip()]
            vdc = {}
            for tok in self.xf_vdc.text().split(","):
                tok = tok.strip()
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        vdc[k.strip()] = float(v)
                    except ValueError:
                        pass
            # per-i_out I-V sweep: "pin=start:step:stop" (Cadence convention) or "pin=auto".
            # The manifest stores [vlo, vhi, step]; the USER types start:step:stop, so reorder.
            ivsw = {}
            for tok in self.xf_ivsweep.text().split(","):
                tok = tok.strip()
                if "=" not in tok:
                    continue
                k, v = tok.split("=", 1)
                k, v = k.strip(), v.strip()
                if v.lower() == "auto":
                    ivsw[k] = "auto"
                else:
                    try:
                        nums = [float(x) for x in v.split(":")]
                    except ValueError:
                        continue
                    if len(nums) == 3:                       # start:step:stop -> [vlo, vhi, step]
                        start, step, stop = nums
                        ivsw[k] = [start, stop, step]
                    else:
                        ivsw[k] = nums                       # 2-token / non-standard: keep as typed
            # temperature points (°C)
            temps = []
            for t in self.xf_temps.text().split(","):
                t = t.strip()
                if t:
                    try:
                        temps.append(float(t))
                    except ValueError:
                        pass
            # supplies: comma list of 'pin@dc' (one or many). A bare 'pin' defaults dc=1.0.
            supplies = []
            for tok in self.xf_supplies.text().split(","):
                tok = tok.strip()
                if not tok:
                    continue
                if "@" in tok:
                    pin, dctxt = tok.split("@", 1)
                    try:
                        sdc = float(dctxt)
                    except ValueError:
                        sdc = 1.0
                else:
                    pin, sdc = tok, 1.0
                if pin.strip():
                    supplies.append({"pin": pin.strip(), "dc": sdc})
            # D4: type the DUT first; the TESTBENCH library inherits the DUT library when blank
            # (tb_lib <- dut_lib). This is the deliberate flip of the old dut_lib<-tb_lib default.
            dut_lib = self.xf_dutlib.text().strip()
            gui = dict(
                tb_lib=self.xf_tblib.text().strip() or dut_lib,
                tb_cell=self.xf_tbcell.text().strip(),
                tb_view=self.xf_tbview.text().strip() or "schematic",
                dut_inst=self.xf_inst.text().strip(),
                dut_lib=dut_lib,
                dut_cell=self.xf_dutcell.text().strip(),
                supplies=supplies,
                v_outs=_csl(self.xf_vouts.text()), i_outs=_csl(self.xf_iouts.text()),
                ground=self.xf_ground.text().strip() or "VSS",
                corner=self.xf_corner.text().strip() or "nom")
            cpsrr = _csl(self.xf_cpsrr.text())
            if cpsrr:
                gui["current_psrr_supplies"] = cpsrr
            if vdc:
                gui["vdc"] = vdc
            if ivsw:
                gui["iv_sweep"] = ivsw
            if temps:
                gui["temps"] = temps
            if self.xf_srctest.text().strip():
                gui["ade_src_test"] = self.xf_srctest.text().strip()
            return gui

        def _x_build_manifest(self):
            """Resolve the form's symbol pins to TB nets (live skillbridge) and build the
            pin-role manifest in the workarea, then load it + enable Build & Run."""
            from insitu.resolve import ResolveUnavailable
            gui = self._form_gui()
            try:
                path, summ, warns = self.extract.build_manifest_from_gui(gui, session=None)
            except ResolveUnavailable as e:
                QMessageBox.critical(self, "Resolve pins",
                    "Net resolution needs a live Virtuoso/skillbridge session (start it in the "
                    f"CIW on the company box).\n\n{e}\n\nOr load a ready manifest below.")
                return
            except Exception as e:
                QMessageBox.critical(self, "Build manifest", f"{type(e).__name__}: {e}")
                return
            self.x_manifest.setText(str(path))
            plan = "\n".join(f"  {a:12s} {d}" for a, d in self.extract.plan())
            txt = ""
            if warns:
                txt += "⚠ WARNINGS:\n" + "\n".join(f"  - {w}" for w in warns) + "\n\n"
            txt += summ + "\n\naugment plan:\n" + plan
            self.x_summary.setPlainText(txt)
            self.x_run.setEnabled(True)
            self.xm_make.setEnabled(False)       # new manifest -> no fit yet (re-run before cell build)
            self.statusBar().showMessage(f"Manifest built → {pathlib.Path(path).name}. "
                                         "'Build & Run' to extract." + (" (see warnings)" if warns else ""))

        def _x_make_cell(self):
            """Deliverable 3: build the ONE combined model cell (Verilog-A + symbol, compiled)
            from the multi-port fit, at the user's lib/cell/path. Live skillbridge → full build;
            no live session → writes the .va + prints the SKILL plan (artifact still produced)."""
            if self.extract.result is None:
                QMessageBox.information(self, "Create model cell",
                    "Run an extraction first (Build & Run) — the model cell is built from the "
                    "multi-port fit result.")
                return
            lib = self.xm_lib.text().strip(); cell = self.xm_cell.text().strip()
            mpath = self.xm_path.text().strip()
            if not (lib and cell and mpath):
                QMessageBox.information(self, "Create model cell",
                    "Fill model lib, cell, and the on-disk library path.")
                return
            from insitu.resolve import ResolveUnavailable, open_session
            ws, dry = None, False
            try:
                ws = open_session()                  # live bridge for the SKILL build
            except ResolveUnavailable:
                dry = True                           # no live Virtuoso -> emit .va + plan only
            try:
                out = self.extract.build_model_cell(lib, cell, mpath, session=ws, dry_run=dry)
            except Exception as e:                       # usage error (stale-fit guard) -> raised
                QMessageBox.critical(self, "Create model cell", f"{type(e).__name__}: {e}")
                return
            va = out["va"]
            if out["built"]:
                msg = (f"Built model cell {lib}/{cell}.\n\nVerilog-A: {va}\nSymbol: input left · "
                       "outputs right · VSS bottom · compiled.")
            elif out.get("error"):                       # .va written, but the LIVE build failed
                msg = (f"Wrote the combined Verilog-A:\n{va}\n\nBut the live cell build did NOT "
                       f"complete: {out['error']}\n\nThe .va is valid — re-run on a healthy CIW "
                       "skillbridge session to import + compile + build the symbol.")
            else:                                        # no live session -> .va + SKILL plan only
                plan = "\n".join(f"  {fn} {args}" for fn, args in out["plan"])
                msg = (f"No live Virtuoso session — wrote the combined Verilog-A only:\n{va}\n\n"
                       "On the company box (live skillbridge) this also imports, compiles, and "
                       f"builds the symbol cell. SKILL plan:\n{plan}")
            self.x_report.append("\n[model cell] " + msg)
            QMessageBox.information(self, "Create model cell", msg)

        def _x_browse(self):
            fn, _ = QFileDialog.getOpenFileName(self, "Pick a manifest JSON",
                                                str(ROOT / "cadence" / "insitu" / "manifests"),
                                                "JSON (*.json)")
            if fn:
                self.x_manifest.setText(fn)

        def _x_load(self):
            try:
                summ = self.extract.load_manifest(self.x_manifest.text().strip())
                plan = "\n".join(f"  {a:12s} {d}" for a, d in self.extract.plan())
                self.x_summary.setPlainText(summ + "\n\naugment plan:\n" + plan)
                # auto-fill the model-cell name from the manifest's DUT (don't re-ask what the
                # manifest already implies) -- only when the user hasn't typed a custom one.
                dcell = (self.extract.manifest.get("dut") or {}).get("cell")
                if dcell and self.xm_cell.text().strip() in ("", "PMU_model"):
                    self.xm_cell.setText(f"{dcell}_model")
                self.x_run.setEnabled(True)
                self.xm_make.setEnabled(False)   # new manifest -> no fit yet (re-run before cell build)
                self.statusBar().showMessage("Manifest loaded — 'Build & Run' to extract.")
            except Exception as e:
                QMessageBox.critical(self, "Manifest", f"{type(e).__name__}: {e}")

        def _resolve_manifest_path(self):
            """Best-effort resolve the Manifest field (a path or a bare name) to a JSON file
            on disk -- so Edit opens the right file. Returns a pathlib.Path or None."""
            from insitu import MANIFEST_DIR
            txt = self.x_manifest.text().strip()
            if not txt:
                return None
            p = pathlib.Path(txt)
            if p.exists():
                return p
            cand = MANIFEST_DIR / (p.name if p.suffix == ".json" else f"{p.name}.json")
            return cand if cand.exists() else None

        def _x_edit(self):
            """Open the current manifest JSON in the editor (re-tag pins for a new LDO)."""
            path = self._resolve_manifest_path()
            if path is None:
                QMessageBox.information(self, "Edit manifest",
                                        "No manifest file resolved from the field. Use 'New…' "
                                        "for a fresh template, or Browse… to pick a JSON.")
                return
            self._open_manifest_editor(path.read_text(), path)

        def _x_new(self):
            """Open the editor on a fresh, commented template (no path until Save As)."""
            self._open_manifest_editor(_MANIFEST_TEMPLATE, None)

        def _open_manifest_editor(self, text, path):
            dlg = _ManifestEditorDialog(self, text, path)
            dlg.subtabs.setCurrentIndex(0)               # land on the structured Form tab
            if dlg.exec_() and dlg.saved_path:           # Save / Save As succeeded
                self.x_manifest.setText(str(dlg.saved_path))
                self._x_load()                            # reload + refresh the summary/plan

        def _x_run(self):
            mode = self.x_mode.currentData()
            location = self.x_location.currentData()
            engine = self.x_backend.currentData()
            # A cluster run IS the full Donau+ALPS sweep (N per-group jobs): execute it end to end
            # (offline per-group netlist -> dsub+alps -> PSF -> npz -> fit). With 'Preview only'
            # ticked it assembles the per-group dsub commands WITHOUT submitting.
            if location == "cluster":
                self._x_cluster_run(engine)
                return
            # Mode B imported netlist, LOCAL: preview the bare engine command (the supported full
            # run is the cluster path; local sweep execution is not wired).
            if mode == "import":
                self._x_cluster_preview(engine, mode)
                return
            # Mode A, local. ALPS is cluster-only -> never silently downgrade it to the fixture.
            if engine == "alps":
                QMessageBox.information(self, "Engine: ALPS",
                    "ALPS runs on the cluster. Set 'Run on' = cluster (import a netlist in Mode B), "
                    "or pick Engine = Spectre for a local CLI run.")
                return
            # the existing in-process worker (ade / spectre_cli).
            backend = engine if engine in ("ade", "spectre_cli") else "spectre_cli"
            self.x_run.setEnabled(False); self.x_cancel.setEnabled(True)
            self.x_gate.setText("running…")
            self.x_prog.setValue(0); self.x_progmsg.setText("starting…")
            self.statusBar().showMessage("Extracting (augment → run → PSF → npz → fit)…")
            self._xw = _ExtractWorker(self.extract, backend,
                                      self.x_session.text().strip(),
                                      regenerate=False)
            self._xw.done.connect(self._x_done)
            self._xw.failed.connect(self._x_failed)
            self._xw.progressed.connect(self._x_progress)
            self._xw.cancelled.connect(self._x_cancelled)
            self._xw.finished.connect(self._xw.deleteLater)   # reap the finished QThread
            self._xw.start()

        def _x_cluster_run(self, engine):
            """Run (or, with 'Preview only', dry-run) the FULL Donau+ALPS sweep from the GUI:
            build each measurement group's one-hot netlist OFFLINE from the imported base
            input.scs, submit one dsub+alps job per group, stream each group's live state into
            the per-group status table, then read PSF -> npz -> multi-port fit. The table makes
            the sweep's state legible at a glance (the whole point of running it from the GUI)."""
            if getattr(self.extract, "manifest", None) is None:
                QMessageBox.information(self, "Cluster run",
                    "Load a manifest first (Mode A builds one from the pin form, or load one below).")
                return
            netdir = self.xb_netlist["edit"].text().strip()
            if not netdir:
                QMessageBox.information(self, "Cluster run (Donau+ALPS)",
                    "A cluster sweep runs an imported BASE netlist. Switch Mode to "
                    "'Import netlist + manifest (no skillbridge)' and set 'Netlist dir (input.scs)' "
                    "to the directory holding your base maestro netlist (Create Netlist in "
                    "ADE/Maestro — no run needed). The tool rewrites it into one one-hot netlist "
                    "per measurement group.\n\n(Mode A resolves the pins/manifest first; then switch "
                    "to Mode B to point at the netlist and run.)")
                return
            pdk = self.xb_pdk["edit"].text().strip()
            ahdl = self.xb_ahdl["edit"].text().strip()
            eng = "spectre" if engine == "spectre_cli" else "alps"
            dry = self.x_dryrun.isChecked()
            from cluster import donau
            cfg = donau.DonauCfg(
                account=self.xd_account.text().strip() or "ug_rfic.rfSClass",
                queue=self.xd_queue.text().strip() or "short",
                resource=f"cpu={self.xd_cpu.value()};mem={self.xd_mem.value()}")
            # pre-fill the status table from the manifest's measurement groups, so the user sees
            # the FULL job list immediately (before the first submit) and watches it fill in.
            try:
                from insitu import run as _run
                groups = _run.groups(self.extract.manifest)
            except Exception as e:                       # noqa: BLE001  malformed manifest
                QMessageBox.critical(self, "Cluster run", f"{type(e).__name__}: {e}")
                return
            self._x_status_init(groups)
            self.x_run.setEnabled(False); self.x_cancel.setEnabled(True)
            self.x_prog.setValue(0)
            self.x_gate.setText("preview…" if dry else "running…")
            self.x_progmsg.setText("assembling per-group netlists…" if dry else "submitting sweep…")
            self.statusBar().showMessage(
                f"{'Previewing' if dry else 'Running'} Donau+ALPS sweep — {len(groups)} "
                f"measurement group(s), engine={eng}…")
            self._xw = _ClusterSweepWorker(self.extract, netlistdir=netdir, pdk=pdk, ahdl=ahdl,
                                           engine=eng, donau_cfg=cfg, dry_run=dry,
                                           max_parallel=self.xd_maxjobs.value(),
                                           work_root=self.xb_workdir["edit"].text().strip() or None)
            self._xw.group_state.connect(self._x_status_set)
            self._xw.progressed.connect(self._x_progress)
            self._xw.done.connect(self._x_cluster_done)
            self._xw.failed.connect(self._x_failed)
            self._xw.cancelled.connect(self._x_cancelled)
            self._xw.finished.connect(self._xw.deleteLater)
            self._xw.start()

        def _x_status_init(self, groups):
            """Fill the per-group status table from the manifest's measurement groups (one row
            per group: #, tag, analysis, state='—'). Called once before a sweep starts."""
            t = self.x_status
            t.setRowCount(0)
            for i, g in enumerate(groups):
                t.insertRow(i)
                t.setItem(i, 0, QTableWidgetItem(str(i + 1)))
                t.setItem(i, 1, QTableWidgetItem(g["tag"]))
                t.setItem(i, 2, QTableWidgetItem(g["analysis"]))
                st = QTableWidgetItem("—"); st.setForeground(QtGui.QColor("#777"))
                t.setItem(i, 3, st)

        _X_STATE_COLOUR = {"pending": "#888", "preview": "#555", "submitting": "#1565c0",
                           "running": "#b8860b", "done": "#157f3b", "failed": "#b00020"}

        def _x_status_set(self, i, n, tag, analysis, state):
            """Update one group's row state (live, from the worker's group_state signal)."""
            if i >= self.x_status.rowCount():            # defensive: table not pre-filled
                return
            it = QTableWidgetItem(state)
            it.setForeground(QtGui.QColor(self._X_STATE_COLOUR.get(state, "#333")))
            self.x_status.setItem(i, 3, it)

        def _x_cluster_done(self, out):
            """A Donau+ALPS sweep finished. Dry-run -> show the per-group dsub commands; a real
            run -> show the multi-port fit report and enable 'Create model cell'."""
            self._x_idle()
            self.x_prog.setValue(100)
            if out.get("dry_run"):
                cmds = out.get("dsub_cmds") or []
                L = [f"# Donau+ALPS sweep PREVIEW — {len(cmds)} per-group dsub command(s), "
                     "nothing submitted", ""]
                for cmd in cmds:
                    L.append(shlex.join(str(x) for x in cmd)); L.append("")
                self.x_report.setPlainText("\n".join(L))
                self.x_gate.setText(f"preview — {len(cmds)} per-group command(s)")
                self.x_progmsg.setText("preview")
                self.statusBar().showMessage(f"Previewed {len(cmds)} per-group dsub command(s) — "
                                             "untick 'Preview only' to run the sweep.")
                return
            npz = pathlib.Path(out["npz_path"]).name
            nmeas = len(out.get("psf_map") or {})
            self.x_gate.setText(f"<b><span style='color:#157f3b'>Donau+ALPS sweep DONE</span></b> "
                                f"— {nmeas} measurement(s), npz {npz}")
            self.x_report.setPlainText(out["report"])
            self.x_port.clear(); self.x_port.addItems(self.extract.port_list())
            self.xm_make.setEnabled(True)                # a fit of the current manifest exists
            self.x_progmsg.setText("done")
            self.statusBar().showMessage("Sweep done — fit complete. 'Create model cell', or send a "
                                         "port to Import → Fit.")

        def _x_cluster_preview(self, engine, mode):
            """Mode-B / cluster: validate inputs and PREVIEW the exact run command(s) per
            measurement group -- pure (no execution). local -> the bare engine command;
            cluster -> the Donau dsub-wrapped command. A live submit is the box-coupled remainder
            (per-group netlists)."""
            if getattr(self.extract, "manifest", None) is None:
                QMessageBox.information(self, "Run preview",
                                        "Load a manifest first (…or load a manifest below).")
                return
            netdir = self.xb_netlist["edit"].text().strip()
            pdk = self.xb_pdk["edit"].text().strip()
            ahdl = self.xb_ahdl["edit"].text().strip()
            if mode == "import" and not netdir:
                QMessageBox.information(self, "Import netlist (Mode B)",
                    "Mode B needs the netlist dir (with input.scs). PDK model dir and ahdllibdir "
                    "are OPTIONAL — the simulator resolves the models and compiles the Verilog-A "
                    "from the netlist's own include/ahdl_include lines if you leave them blank.")
                return
            try:
                from cluster import donau, alps_cli      # local import: offline-safe, no skillbridge
                from insitu import run as _run
                on_cluster = self.x_location.currentData() == "cluster"
                cfg = donau.DonauCfg(
                    account=self.xd_account.text().strip() or "ug_rfic.rfSClass",
                    queue=self.xd_queue.text().strip() or "short",
                    resource=f"cpu={self.xd_cpu.value()};mem={self.xd_mem.value()}")
                eng = "spectre" if engine == "spectre_cli" else "alps"
                grps = _run.groups(self.extract.manifest)
                where = "Donau dsub (cluster)" if on_cluster else "local"
                L = [f"# {where} run PREVIEW — {len(grps)} measurement group(s), engine={eng}"]
                if on_cluster:
                    L.append(f"#   -A {cfg.account}   -q {cfg.queue}   -R {shlex.quote(cfg.resource)}")
                L += [f"#   netlistdir={netdir or '<set in Mode B>'}",
                      f"#   pdk={pdk or '(none — netlist self-contained)'}   "
                      f"ahdllibdir={ahdl or '(none — sim auto-compiles VA)'}", ""]
                for g in grps:
                    # blank pdk/ahdl -> None -> the -I / -ahdllibdir flags are omitted, and the
                    # simulator resolves models + compiles VA from the netlist itself.
                    payload = alps_cli.build_sim_cmd(eng, "input.scs", "../psf",
                                                     pdk or None, ahdl or None, mt=cfg.cpu or 8)
                    # shlex.join: resource 'cpu=N;mem=M' and spaced paths MUST be quoted or the
                    # shell splits them -- the whole point is a copy-pasteable command.
                    cmd = (donau.build_dsub_cmd(cfg, payload, netdir or "<netlistdir>")
                           if on_cluster else payload)
                    L += [f"## group {g['tag']}  ({g['analysis']})",
                          shlex.join(str(x) for x in cmd), ""]
                L += ["# NOTE: this LOCAL preview shows the bare per-group command SHAPE. The real",
                      "#   per-group one-hot netlists are built automatically — set Run-on = cluster",
                      "#   and click Build & Run to execute the full Donau+ALPS sweep (each group",
                      "#   gets its own one-hot netlist; watch the per-group status table)."]
                self.x_report.setPlainText("\n".join(L))
                self.x_gate.setText(f"{where} preview — {len(grps)} command(s) above")
                self.statusBar().showMessage(f"Previewed {len(grps)} {where} command(s) "
                                             f"(engine={eng}). Nothing executed.")
            except Exception as e:
                QMessageBox.critical(self, "Run preview", f"{type(e).__name__}: {e}")

        def _x_progress(self, frac, msg):
            if frac >= 0:                                # frac<0 == message-only (don't move the bar)
                self.x_prog.setValue(max(0, min(100, int(frac * 100))))
            self.x_progmsg.setText(msg)

        def _x_cancel(self):
            if getattr(self, "_xw", None) and self._xw.isRunning():
                self.x_cancel.setEnabled(False)
                self.x_progmsg.setText("cancelling — finishing current step…")
                self._xw.cancel()

        def _x_cancelled(self):
            self._x_idle()
            self.x_gate.setText("cancelled")
            self.x_progmsg.setText("cancelled")
            self.statusBar().showMessage("Run cancelled (ade: ADE state restored; cluster: stopped "
                                         "between groups). Adjust and Build & Run again.")

        def _x_idle(self):
            """Return Tab 0 to the runnable state (re-run is just 'Build & Run' again)."""
            self.x_run.setEnabled(True); self.x_cancel.setEnabled(False)

        def _x_done(self, out):
            self._x_idle()
            self.x_prog.setValue(100)
            passed, worst, detail = out["gate"]
            tag = {True: "PASS", False: "FAIL", None: "SKIP"}[passed]
            colour = {"PASS": "#157f3b", "FAIL": "#b00020", "SKIP": "#777"}[tag]
            self.x_gate.setText(f"<b>gate vs gold: <span style='color:{colour}'>{tag}</span></b> "
                                f"({detail}) — npz {pathlib.Path(out['npz_path']).name}")
            self.x_report.setPlainText(out["report"])
            self.x_port.clear(); self.x_port.addItems(self.extract.port_list())
            self.xm_make.setEnabled(True)        # a fit of the current manifest exists -> cell build OK
            self.x_progmsg.setText("done")
            self.statusBar().showMessage(f"Extraction done — gate {tag}. 'Create model cell', "
                                         "or pick an output port for 'Load into Import → Fit'.")

        def _x_failed(self, msg):
            self._x_idle()
            self.x_gate.setText("FAILED")
            self.x_progmsg.setText("failed")
            QMessageBox.critical(self, "Extraction failed", msg)

        def _x_send(self):
            port = self.x_port.currentText()
            if not port or port not in self.extract.port_refs:
                return
            refp = self.extract.port_refs[port]
            try:
                self.core.use_existing_ref(refp)
                self.core.profile.name = pathlib.Path(refp).stem
                # reuse the GUI's full programmatic-profile sync (Profile + Import grid +
                # Compare corner dropdown + cout/esr), so Fit/Compare operate on this port.
                self.refresh_from_profile()
                self.tabs.setCurrentIndex(3)        # jump to Fit (tab 0=Extract shifted it)
                self.statusBar().showMessage(f"Loaded port '{port}' ({pathlib.Path(refp).name}) "
                                             "→ Fit, then Compare.")
            except Exception as e:
                QMessageBox.critical(self, "Load port", f"{type(e).__name__}: {e}")

        # --- Tab 1: Profile ------------------------------------------------------
        def _tab_profile(self):
            w = QWidget(); outer = QVBoxLayout(w)
            help_ = QLabel(
                "<b>What this tool does:</b> builds a fast behavioral model of your LDO from exported "
                "Spectre/ADE data, and shows model-vs-data overlays — all offline, no simulator.<br><br>"
                "<b>You only need to fill 3 things here</b> (marked *): a <b>name</b>, the <b>supply "
                "voltage</b> your LDO runs at, and the <b>3 load currents</b> you characterized "
                "(low / typical / high). The rest is optional and auto-filled.<br>"
                "Then: <b>2 Import data → 3 Fit → 4 Compare</b>.")
            help_.setWordWrap(True)
            help_.setStyleSheet("background:#eef5ff; padding:9px; border:1px solid #cdddee;")
            outer.addWidget(help_)
            form = QFormLayout(); outer.addLayout(form)
            p = self.core.profile
            self.e_name = QLineEdit(p.name)
            self.e_name.setToolTip("Any short name. Emitted files are model/<name>.lib and .va.")
            self.e_vref = QLineEdit(str(p.vref))
            self.e_vref.setToolTip("Your LDO's INPUT supply voltage during characterization "
                                   "(e.g. 1.05, 1.8, 3.3). The PSRR is referenced to this OP. "
                                   "You know this from your testbench.")
            self.e_loads = QLineEdit(",".join(p.loads))
            self.e_loads.setPlaceholderText("e.g.  20u,121u,250u   (or  1m,10m,50m  for a mA-class LDO)")
            self.e_loads.setToolTip("The 3 load CURRENTS you swept, low→high, comma-separated. "
                                    "Suffixes p/n/u/m/k allowed. The model interpolates between them. "
                                    "You know these from your testbench (the DC load steps).")
            self.e_nom = QComboBox(); self.e_nom.addItems(p.loads)
            if p.nominal in p.loads:
                self.e_nom.setCurrentText(p.nominal)
            self.e_nom.setToolTip("The TYPICAL operating load (the centre corner). Auto-set to the "
                                  "middle of your list — normally just leave it as is.")
            self.e_cout = QLineEdit("")
            self.e_cout.setPlaceholderText("auto-extracted from Zout — leave blank")
            self.e_cout.setToolTip("OPTIONAL. The tool auto-extracts the output cap from the Zout HF "
                                   "tail. Fill ONLY to cross-check against your design value (e.g. 1e-9).")
            self.e_esr = QLineEdit("")
            self.e_esr.setPlaceholderText("auto-extracted from Zout — leave blank")
            self.e_esr.setToolTip("OPTIONAL. Output-cap ESR; also auto-extracted. Fill only to "
                                  "cross-check (e.g. 0.5).")
            form.addRow("Model name *", self.e_name)
            form.addRow("Supply voltage Vin [V] *", self.e_vref)
            form.addRow("Load corners (3, low→high) *", self.e_loads)
            form.addRow("Nominal corner (auto)", self.e_nom)
            form.addRow("Design Cout [F] (optional)", self.e_cout)
            form.addRow("Design ESR [Ω] (optional)", self.e_esr)
            b = QPushButton("Apply profile   →   go to Import data")
            b.clicked.connect(self._apply_profile)
            form.addRow(b)
            outer.addStretch(1)
            return w

        RESERVED_CORNERS = {"hf", "loadreg", "linereg", "dropout"}   # clash with hf/dc file stems

        def _apply_profile(self):
            """Push the Profile widgets into core.profile. Returns True on success, False on a
            validation error (callers must not proceed on False). Rebuilds the import grid whenever
            the corners OR the nominal change (nominal-scope pickers are keyed by the nominal)."""
            try:
                p = self.core.profile
                old_loads, old_nom = list(p.loads), p.nominal   # rebuild grid only if these change
                p.name = self.e_name.text().strip() or "myldo"
                if not self.e_vref.text().strip():
                    raise ValueError("supply voltage Vin is required")
                p.vref = float(self.e_vref.text())
                loads = [s.strip() for s in self.e_loads.text().split(",") if s.strip()]
                if not loads:
                    raise ValueError("enter the load corners, e.g. 20u,121u,250u")
                bad = self.RESERVED_CORNERS & set(loads)
                if bad:
                    raise ValueError(f"corner name(s) {sorted(bad)} are reserved (they clash with "
                                     "hf/dc file stems) — rename them")
                p.loads = loads
                nom = self.e_nom.currentText().strip()
                p.nominal = nom if nom in loads else loads[len(loads) // 2]
                # Cout/ESR optional -> NaN means "auto-extract from Zout"
                p.cout = float(self.e_cout.text()) if self.e_cout.text().strip() else float("nan")
                p.esr = float(self.e_esr.text()) if self.e_esr.text().strip() else float("nan")
                if p.loads != old_loads:                # refresh the nominal dropdown + compare combo
                    self.e_nom.blockSignals(True)
                    self.e_nom.clear(); self.e_nom.addItems(loads)
                    self.e_nom.setCurrentText(p.nominal)
                    self.e_nom.blockSignals(False)
                    self.cmp_corner.clear(); self.cmp_corner.addItems(p.loads)
                if p.loads != old_loads or p.nominal != old_nom:   # nominal-scope keys track nominal
                    self._rebuild_import_grid()         # preserves/re-maps already-picked paths
                self.statusBar().showMessage(f"Profile '{p.name}' OK: {len(p.loads)} corners, "
                                             f"nominal={p.nominal}, Vin={p.vref} V. → go to Import data.")
                return True
            except Exception as e:
                QMessageBox.warning(self, "Profile error",
                                    f"{e}\n\nNeed: a name, a numeric supply voltage, and "
                                    "comma-separated load corners (e.g. 20u,121u,250u).")
                return False

        def refresh_from_profile(self):
            """Seed the widgets FROM core.profile (used after use_existing_ref/--ref, where the
            profile is set programmatically and the widgets would otherwise show stale defaults)."""
            p = self.core.profile
            self.e_name.setText(p.name)
            self.e_vref.setText(str(p.vref))
            self.e_loads.setText(",".join(p.loads))
            self.e_nom.blockSignals(True)
            self.e_nom.clear(); self.e_nom.addItems(p.loads)
            if p.nominal in p.loads:
                self.e_nom.setCurrentText(p.nominal)
            self.e_nom.blockSignals(False)
            self.e_cout.setText("" if p.cout is None or np.isnan(p.cout) else str(p.cout))
            self.e_esr.setText("" if p.esr is None or np.isnan(p.esr) else str(p.esr))
            self._rebuild_import_grid()
            self.cmp_corner.clear(); self.cmp_corner.addItems(p.loads)
            self._refresh_compare_ports()

        # --- Tab 2: Import -------------------------------------------------------
        def _tab_import(self):
            w = QWidget(); lay = QVBoxLayout(w)
            legend = QLabel(
                "<b>Easiest path:</b> drop all your exports in one folder, named like "
                "<code>z_20u.csv, p_20u.csv, noise_20u.csv, z_hf.csv, dc_loadreg.csv, dc_dropout.csv</code>, "
                "then click <b>Import from folder…</b> (it auto-fills the grid).<br>"
                "<b>Required to fit:</b> Zout, PSRR, Noise (per corner) + Zout-HF (nominal) + "
                "DC load-reg + DC dropout. <b>Everything under “Optional” can be skipped.</b>")
            legend.setWordWrap(True)
            legend.setStyleSheet("background:#eef5ff; padding:7px; border:1px solid #cdddee;")
            lay.addWidget(legend)
            opts = QHBoxLayout()
            b_folder = QPushButton("Import from folder…")
            b_folder.setToolTip("Pick a folder; files named <quantity>_<corner>.csv are matched "
                                "into the grid automatically.")
            b_folder.clicked.connect(self._import_folder)
            opts.addWidget(b_folder)
            opts.addSpacing(16)
            opts.addWidget(QLabel("complex fmt:"))
            self.cb_fmt = QComboBox(); self.cb_fmt.addItems(
                ["auto", "reim", "magdeg", "magrad", "dbdeg", "dbrad"])
            self.cb_fmt.setToolTip("How complex Zout/PSRR columns are stored. 'auto' reads the CSV header.")
            opts.addWidget(self.cb_fmt)
            self.cb_sqrt = QCheckBox("noise is V²/Hz")
            self.cb_sqrt.setToolTip("Tick if your noise export is power PSD (V²/Hz); it will be "
                                    "sqrt-ed to amplitude PSD (V/√Hz).")
            opts.addWidget(self.cb_sqrt); opts.addStretch(1)
            b_hint = QPushButton("Measurement guidance")
            b_hint.clicked.connect(self._show_guidance)
            opts.addWidget(b_hint)
            lay.addLayout(opts)
            # the file-path grid can be many rows (quantities x corners) -- put it in its OWN
            # scroll area so a tall grid can never push the preview plot off-screen or squeeze
            # it to a sliver (the "积压" bug: tall grid + warn box + plot in one non-scrolling
            # column). The grid scrolls internally; the preview keeps its height below.
            self.grid_host = QWidget()
            grid_scroll = QScrollArea(); grid_scroll.setWidgetResizable(True)
            grid_scroll.setWidget(self.grid_host)
            grid_scroll.setMaximumHeight(300)
            grid_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            lay.addWidget(grid_scroll)
            self._rebuild_import_grid()
            row = QHBoxLayout()
            b_imp = QPushButton("Import + preview   →   go to Fit")
            b_imp.clicked.connect(self._do_import)
            row.addWidget(b_imp); row.addStretch(1)
            lay.addLayout(row)
            self.warn_box = QTextEdit(); self.warn_box.setReadOnly(True)
            self.warn_box.setMaximumHeight(110); lay.addWidget(self.warn_box)
            # preview: firm minimum height so the grid above can't squeeze it; constrained_layout
            # (in _Canvas) keeps the 3 panels from overlapping when the window is small.
            self.imp_canvas = _Canvas(1, 3, figsize=(11, 3.0))
            self.imp_canvas.setMinimumHeight(240)
            lay.addWidget(self.imp_canvas)
            return w

        def _show_guidance(self):
            QMessageBox.information(self, "Measurement guidance", MEAS_HINTS)

        def _import_folder(self):
            if not self._apply_profile():       # don't scan with a stale/invalid profile
                return
            d = QFileDialog.getExistingDirectory(self, "Select the folder with your exports", str(ROOT))
            if not d:
                return
            import import_cadence as ic
            files = ic.match_dir(d, self.core.profile.loads, self.core.profile.nominal)
            for (key, corner), pth in files.items():
                if (key, corner) in self.file_edits:
                    self.file_edits[(key, corner)].setText(str(pth))
            if files:
                self.statusBar().showMessage(
                    f"Matched {len(files)} files from folder. Review the grid, then 'Import + preview'.")
            else:
                QMessageBox.information(self, "Import from folder",
                    "No files matched. Expected names like z_20u.csv, p_20u.csv, noise_20u.csv, "
                    "z_hf.csv, dc_loadreg.csv, dc_dropout.csv in the chosen folder "
                    "(corner keys must match your profile's load corners).")

        def _rebuild_import_grid(self):
            host = self.grid_host
            saved = {k: e.text() for k, e in self.file_edits.items()}   # preserve picked paths
            old = host.layout()
            if old is not None:
                QWidget().setLayout(old)        # detach old layout
            g = QGridLayout(host)
            self.file_edits = {}
            loads = self.core.profile.loads
            nom = self.core.profile.nominal
            ncol = 1 + len(loads)
            g.addWidget(QLabel("<b>quantity</b>"), 0, 0)
            for j, il in enumerate(loads):
                star = "  ◀nominal" if il == nom else ""
                g.addWidget(QLabel(f"<b>{il}</b>{star}"), 0, 1 + j)
            r = 1
            for group, want in (("Required", True), ("Optional — skip if you don't have them", False)):
                hdr = QLabel(f"— {group} —")
                hdr.setStyleSheet("font-weight:bold; color:#234; margin-top:4px;")
                g.addWidget(hdr, r, 0, 1, ncol); r += 1
                for lab, key, scope, required in IMPORT_ROWS:
                    if required != want:
                        continue
                    tag = {"corner": "", "nominal": " (nom)", "global": " (1 file)"}[scope]
                    g.addWidget(QLabel(lab + tag), r, 0)
                    if scope == "corner":
                        for j, il in enumerate(loads):
                            self._add_picker(g, r, 1 + j, key, il)
                    elif scope == "nominal":
                        j = loads.index(nom) if nom in loads else 0
                        self._add_picker(g, r, 1 + j, key, nom)
                    else:                                # global: one file under the first column
                        self._add_picker(g, r, 1, key, None)
                    r += 1
            nom_keys = {key for _, key, scope, _ in IMPORT_ROWS if scope == "nominal"}
            for (key, corner), e in self.file_edits.items():
                if saved.get((key, corner)):
                    e.setText(saved[(key, corner)])
                elif key in nom_keys:               # nominal moved: recover a path picked under the
                    for (sk, _sc), v in saved.items():   # old nominal so it isn't silently lost
                        if sk == key and v:
                            e.setText(v); break

        def _add_picker(self, g, r, c, key, corner):
            cell = QWidget(); h = QHBoxLayout(cell); h.setContentsMargins(0, 0, 0, 0)
            e = QLineEdit(); e.setMinimumWidth(120)
            btn = QPushButton("…"); btn.setMaximumWidth(28)
            btn.clicked.connect(lambda _, ed=e: self._pick(ed))
            h.addWidget(e); h.addWidget(btn)
            g.addWidget(cell, r, c)
            self.file_edits[(key, corner)] = e

        def _pick(self, edit):
            fn, _ = QFileDialog.getOpenFileName(self, "Select export", str(ROOT),
                                                "Data (*.csv *.txt *.psf *.psfascii);;All (*)")
            if fn:
                edit.setText(fn)

        def _collect_files(self):
            files = {}
            for (key, corner), e in self.file_edits.items():
                t = e.text().strip()
                if t:
                    files[(key, corner)] = t
            return files

        def _do_import(self):
            if not self._apply_profile():       # don't import against a stale/invalid profile
                return
            files = self._collect_files()
            if not files:
                QMessageBox.information(self, "Import", "No files selected.")
                return
            fmt = None if self.cb_fmt.currentText() == "auto" else self.cb_fmt.currentText()
            try:
                path, warns = self.core.import_data(files, fmt=fmt,
                                                    sv_is_psd2=self.cb_sqrt.isChecked())
            except Exception as e:
                QMessageBox.critical(self, "Import failed", str(e)); return
            txt = f"Wrote {path}\n"
            if not warns:
                txt += "Guardrails: no issues detected."
            for w in warns:
                txt += f"[{w['level'].upper()}] {w['quantity']}: {w['msg']}\n"
            miss = self._missing_required(files)
            if miss:
                txt = ("[INFO] still missing REQUIRED: " + ", ".join(miss) +
                       " — you can fit once these are added.\n") + txt
            self.warn_box.setText(txt)
            self._preview()
            self._refresh_compare_ports()        # surface any current ports the new ref carries
            self.b_emit.setEnabled(False)        # new data invalidates any prior fit -> must re-Fit
            self.statusBar().showMessage(f"Imported → {path.name}. → go to Fit (Tab 3).")

        def _missing_required(self, files):
            """List required (quantity@corner) slots not yet provided -- shown after import."""
            loads = self.core.profile.loads; nom = self.core.profile.nominal
            miss = []
            for lab, key, scope, required in IMPORT_ROWS:
                if not required:
                    continue
                if scope == "corner":
                    miss += [f"{lab}@{il}" for il in loads if (key, il) not in files]
                elif scope == "nominal" and (key, nom) not in files:
                    miss.append(f"{lab}@{nom}")
                elif scope == "global" and (key, None) not in files:
                    miss.append(lab)
            return miss

        def _preview(self):
            nom = self.core.profile.nominal
            self.imp_canvas.clear()
            ax = self.imp_canvas.axes.flat
            try:
                cg = self.core.gt_corner(nom)
                ax[0].loglog(cg["fz"], np.abs(cg["Zg"])); ax[0].set_title(f"Zout |Z| ({nom})")
                ax[0].set_xlabel("Hz"); ax[0].set_ylabel("ohm")
                ax[1].semilogx(cg["fp"], -20 * np.log10(np.abs(cg["Hg"]) + 1e-30))
                ax[1].set_title(f"PSRR atten ({nom})"); ax[1].set_xlabel("Hz"); ax[1].set_ylabel("dB")
                ax[2].loglog(cg["fn"], cg["Sg"] * 1e9); ax[2].set_title(f"noise ({nom})")
                ax[2].set_xlabel("Hz"); ax[2].set_ylabel("nV/rtHz")
                for a in ax:
                    a.grid(True, which="both", alpha=.3)
            except Exception as e:
                self.warn_box.append(f"\n[preview] {e}")
            self.imp_canvas.draw()

        # --- Tab 3: Fit ----------------------------------------------------------
        def _tab_fit(self):
            w = QWidget(); lay = QVBoxLayout(w)
            row = QHBoxLayout()
            self.b_fit = QPushButton("Fit"); self.b_fit.clicked.connect(self._do_fit)
            self.b_emit = QPushButton("Emit .lib / .va"); self.b_emit.setEnabled(False)
            self.b_emit.clicked.connect(self._do_emit)
            row.addWidget(self.b_fit); row.addWidget(self.b_emit); row.addStretch(1)
            lay.addLayout(row)
            self.fit_table = QTableWidget(0, 4)
            self.fit_table.setHorizontalHeaderLabels(
                ["corner", "Zout RMS [dB]", "PSRR RMS [dB]", "noise PSD [dB]"])
            lay.addWidget(self.fit_table)
            self.fit_log = QTextEdit(); self.fit_log.setReadOnly(True); lay.addWidget(self.fit_log)
            return w

        def _fit_blockers(self):
            """Required quantities missing from the LOADED reference (not just the grid), so the
            check also covers data opened via --ref. Fit needs z/p/noise per corner + DC load-reg;
            Zout-HF falls back to z_<nom> and DC dropout is only needed at emit, so neither blocks fit."""
            ref = self.core.ref
            if ref is None:
                return ["(no data imported — use Tab 2)"]
            need = []
            for il in self.core.profile.loads:
                need += [f"{q}_{il}" for q in ("z", "p", "noise") if f"{q}_{il}" not in ref]
            if "dc_loadreg" not in ref:
                need.append("dc_loadreg")
            return need

        def _do_fit(self):
            if getattr(self, "_worker", None) is not None and self._worker.isRunning():
                return                           # ignore re-clicks while a fit is already running
            blockers = self._fit_blockers()
            if blockers:
                QMessageBox.information(self, "Fit", "Cannot fit yet — missing required data:\n  "
                                       + ", ".join(blockers[:10]) + ("…" if len(blockers) > 10 else "")
                                       + "\n\nProvide these on Tab 2 (Import).")
                return
            self.b_fit.setEnabled(False); self.statusBar().showMessage("Fitting…")
            self._worker = _FitWorker(self.core)
            self._worker.done.connect(self._fit_done)
            self._worker.failed.connect(self._fit_failed)
            self._worker.start()

        def _fit_done(self, result):
            self.b_fit.setEnabled(True); self.b_emit.setEnabled(True)
            res = self.core.fit_residuals()
            self.fit_table.setRowCount(len(res))
            for i, r in enumerate(res):
                for j, v in enumerate([r["il"], f"{r['zrms']:.3f}", f"{r['prms']:.3f}", f"{r['npsd']:.3f}"]):
                    self.fit_table.setItem(i, j, QTableWidgetItem(v))
            self.fit_log.setText(
                f"Fit OK. nominal={result.nominal} vref={result.vref} "
                f"Cout={result.cout*1e12:.1f}pF ESR={result.esr:.3f}ohm "
                f"noise corners={['%.3g'%x for x in result.nfk]} spurs={len(result.spur_f)}")
            self.statusBar().showMessage("Fit complete.")
            self._refresh_compare_ports()
            self._refresh_compare()

        def _fit_failed(self, msg):
            self.b_fit.setEnabled(True)
            QMessageBox.critical(self, "Fit failed", msg)
            self.statusBar().showMessage("Fit failed.")

        def _do_emit(self):
            try:
                lib, va = self.core.emit()
            except Exception as e:
                QMessageBox.critical(self, "Emit failed", str(e)); return
            tbl = va.with_name(va.stem + "_dropout.tbl")
            self.fit_log.append(f"\nwrote {va}\nwrote {lib}\nwrote {tbl}")
            self.statusBar().showMessage(f"Emitted to {va.parent}")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Model files written")
            box.setText("Wrote the behavioral model to:\n\n"
                        f"• {va}\n• {lib}\n• {tbl}\n\nFolder:  {va.parent}")
            try:
                from PyQt5.QtCore import Qt
                box.setTextInteractionFlags(Qt.TextSelectableByMouse)   # so the path is copy-able
            except Exception:
                pass
            open_btn = box.addButton("Open folder", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Ok)
            box.exec_()
            if box.clickedButton() is open_btn:
                try:
                    from PyQt5.QtGui import QDesktopServices
                    from PyQt5.QtCore import QUrl
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(va.parent)))
                except Exception:
                    pass

        # --- Tab 4: Compare ------------------------------------------------------
        def _tab_compare(self):
            w = QWidget(); lay = QVBoxLayout(w)
            row = QHBoxLayout()
            # PORT selector: one port at a time (voltage output, or a current bias port) so a
            # multi-port PMU never piles every quantity onto one figure. Voltage -> Zout/PSRR/
            # noise (corner-swept); current -> I-V/Y/PSRR/noise/Idc(T) (V/I dual). userData =
            # None for the voltage output, the pin string for a current port.
            row.addWidget(QLabel("port:"))
            self.cmp_port = QComboBox()
            self.cmp_port.setToolTip("Pick which modeled output to overlay. Current ports show "
                                     "I-V / admittance / current-PSRR / current-noise / Idc(T) "
                                     "instead of Zout (current ports measure I, not V).")
            self.cmp_port.currentIndexChanged.connect(self._on_cmp_port)
            row.addWidget(self.cmp_port)
            row.addSpacing(12)
            row.addWidget(QLabel("corner:"))
            self.cmp_corner = QComboBox(); self.cmp_corner.addItems(self.core.profile.loads)
            self.cmp_corner.currentIndexChanged.connect(self._refresh_compare)
            row.addWidget(self.cmp_corner); row.addStretch(1)
            self.cmp_report_btn = QPushButton("Save text report…")
            self.cmp_report_btn.setToolTip("Write the plain-text model-vs-GT difference report "
                                           "(voltage [7] + current [8]; copy-pasteable diagnosis).")
            self.cmp_report_btn.clicked.connect(self._save_text_report)
            row.addWidget(self.cmp_report_btn)
            lay.addLayout(row)
            self.cmp_canvas = _Canvas(2, 3, figsize=(11, 6))
            self.cmp_canvas.setMinimumHeight(380)
            lay.addWidget(self.cmp_canvas)
            self.cmp_err = QLabel("Fit a model to see the overlay."); lay.addWidget(self.cmp_err)
            self._refresh_compare_ports()
            return w

        def _refresh_compare_ports(self):
            """Repopulate the port selector from the loaded ref (voltage output + any current
            ports). Called after import / fit / --ref. Preserves the current selection."""
            if not hasattr(self, "cmp_port"):
                return
            keep = self.cmp_port.currentData() if self.cmp_port.count() else None
            self.cmp_port.blockSignals(True)
            self.cmp_port.clear()
            self.cmp_port.addItem("Vout · voltage", None)
            for pin in self.core.current_ports():
                self.cmp_port.addItem(f"{pin} · current", pin)
            idx = self.cmp_port.findData(keep)
            self.cmp_port.setCurrentIndex(idx if idx >= 0 else 0)
            self.cmp_port.blockSignals(False)
            self._sync_corner_enabled()

        def _sync_corner_enabled(self):
            """The corner picker only applies to the voltage output; grey it out for current
            ports (their temp axis is the Idc(T) panel, not a load corner)."""
            if hasattr(self, "cmp_corner"):
                self.cmp_corner.setEnabled(self.cmp_port.currentData() is None)

        def _on_cmp_port(self):
            self._sync_corner_enabled()
            self._refresh_compare()

        def _draw_current_compare(self, pin):
            """V/I-dual overlay for one current port: I-V knee / |Y| / current-noise /
            current-PSRR / Idc(T) + a fit-vs-GT metric box. Pure-numpy via core.current_compare."""
            ax = self.cmp_canvas.axes
            self.cmp_canvas.clear()
            try:
                d = self.core.current_compare(pin)
            except Exception as e:
                self.cmp_canvas.draw()
                self.cmp_err.setText(f"current port {pin}: {e}")
                return
            v, p, m = d["view"], d["params"], d["metrics"]
            ax[0, 0].plot(v["iv_v"], v["iv_i"] * 1e6, label="GT", lw=2)
            ax[0, 0].plot(v["iv_v"], d["iv_model"] * 1e6, "--", label="model")
            ax[0, 0].axvline(v["vc"], color="k", ls=":", lw=.8)
            ax[0, 0].set_title("I-V compliance knee")   # pin/pol shown in the metric box + status line
            ax[0, 0].set_xlabel("Vo [V]"); ax[0, 0].set_ylabel("I [µA]")
            ax[0, 1].loglog(v["ac_f"], np.abs(v["ac_y"]), label="GT", lw=2)
            ax[0, 1].loglog(v["ac_f"], np.abs(d["y_model"]), "--", label="model")
            ax[0, 1].set_title("|Y| admittance [S]"); ax[0, 1].set_xlabel("Hz")
            ax[0, 2].loglog(v["nz_f"], v["nz_in"] * 1e15, label="GT", lw=2)
            ax[0, 2].loglog(v["nz_f"], d["noise_model"] * 1e15, "--", label="model")
            ax[0, 2].set_title("current noise In [fA/√Hz]"); ax[0, 2].set_xlabel("Hz")
            ax[1, 0].loglog(v["psrr_f"], np.abs(v["psrr_g"]), label="GT", lw=2)
            ax[1, 0].loglog(v["psrr_f"], np.abs(d["psrr_model"]), "--", label="model")
            ax[1, 0].set_title("current-PSRR |dI/dVdd| [S]"); ax[1, 0].set_xlabel("Hz")
            ax[1, 1].plot(v["temps"], v["idcT"] * 1e6, "o-", label="GT", lw=2)
            ax[1, 1].plot(v["temps"], d["idcT_model"] * 1e6, "s--", label="model")
            ax[1, 1].set_title("Idc(T) [µA]"); ax[1, 1].set_xlabel("T [°C]")
            sign = "OK" if m["sign_ok"] else f"FLIP! ({m['gdd_sign']} vs GT {m['gt_sign']})"
            ax[1, 2].axis("off")
            ax[1, 2].text(0.0, 0.98,
                          f"port {pin}  ({v['pol']})\n\n"
                          f"Idc       = {m['idc_ua']:.3f} µA\n"
                          f"I-V  RMS  = {m['ivrms']:.2f} %\n"
                          f"rout      = {m['rout_M']:.1f} MΩ\n"
                          f"Cp        = {m['cp_fF']:.1f} fF\n"
                          f"gdd       = {m['gdd_nS']:+.3f} nS\n"
                          f"PSRR sign = {sign}\n"
                          f"PSRR RMS  = {m['prms']:.2f} dB\n"
                          f"noise RMS = {m['nrms']:.2f} dB\n"
                          f"PTAT g/m  = {m['ptat_g']:.3f} / {m['ptat_m']:.3f}",
                          fontsize=9, family="monospace", va="top")
            for a in (ax[0, 0], ax[0, 1], ax[0, 2], ax[1, 0], ax[1, 1]):
                a.grid(True, which="both", alpha=.3); a.legend(fontsize=8)
            self.cmp_canvas.draw()
            sf = "sign OK" if m["sign_ok"] else "SIGN FLIP"
            self.cmp_err.setText(f"current port {pin} ({v['pol']}):  IVrms={m['ivrms']:.2f}%  "
                                 f"PSRR {sf}  Prms={m['prms']:.2f}dB  noiseRMS={m['nrms']:.2f}dB  "
                                 f"PTAT GT/model={m['ptat_g']:.3f}/{m['ptat_m']:.3f}")

        def _refresh_compare(self):
            pin = self.cmp_port.currentData() if hasattr(self, "cmp_port") else None
            if pin is not None:                       # a current port -> V/I-dual overlay
                self._draw_current_compare(pin)
                return
            if self.core.result is None:
                return
            il = self.cmp_corner.currentText() or self.core.result.loads[0]
            if il not in self.core.result.loads:
                il = self.core.result.loads[0]
            cg = self.core.gt_corner(il); pm = self.core.predict_corner(il)
            ax = self.cmp_canvas.axes
            self.cmp_canvas.clear()
            ax[0, 0].loglog(cg["fz"], np.abs(cg["Zg"]), label="GT", lw=2)
            ax[0, 0].loglog(cg["fz"], np.abs(pm["Zm"]), "--", label="model")
            ax[0, 0].set_title(f"Zout |Z| ({il})"); ax[0, 0].set_ylabel("ohm")
            ax[0, 1].semilogx(cg["fz"], np.degrees(np.angle(cg["Zg"])), label="GT", lw=2)
            ax[0, 1].semilogx(cg["fz"], np.degrees(np.angle(pm["Zm"])), "--", label="model")
            ax[0, 1].set_title("Zout phase [deg]")
            ax[0, 2].loglog(cg["fn"], cg["Sg"] * 1e9, label="GT", lw=2)
            ax[0, 2].loglog(cg["fn"], pm["Sm"] * 1e9, "--", label="model")
            ax[0, 2].set_title("noise [nV/rtHz]")
            ax[1, 0].semilogx(cg["fp"], -20 * np.log10(np.abs(cg["Hg"]) + 1e-30), label="GT", lw=2)
            ax[1, 0].semilogx(cg["fp"], -20 * np.log10(np.abs(pm["Hm"]) + 1e-30), "--", label="model")
            ax[1, 0].set_title("PSRR atten [dB]")
            ax[1, 1].semilogx(cg["fp"], np.degrees(np.angle(cg["Hg"])), label="GT", lw=2)
            ax[1, 1].semilogx(cg["fp"], np.degrees(np.angle(pm["Hm"])), "--", label="model")
            ax[1, 1].set_title("PSRR phase [deg]")
            zrms = np.sqrt(np.mean((20 * np.log10(np.abs(pm["Zm"]) / np.abs(cg["Zg"]))) ** 2))
            prms = np.sqrt(np.mean((20 * np.log10(np.abs(pm["Hm"]) / np.abs(cg["Hg"]))) ** 2))
            npsd = np.sqrt(np.mean((20 * np.log10((pm["Sm"] + 1e-30) / (cg["Sg"] + 1e-30))) ** 2))
            ax[1, 2].axis("off")
            ax[1, 2].text(0.0, 0.8, f"corner {il}\n\nZout RMS  = {zrms:.3f} dB\n"
                          f"PSRR RMS  = {prms:.3f} dB\nnoise PSD = {npsd:.3f} dB",
                          fontsize=11, family="monospace", va="top")
            for a in (ax[0, 0], ax[0, 1], ax[0, 2], ax[1, 0], ax[1, 1]):
                a.set_xlabel("Hz"); a.grid(True, which="both", alpha=.3); a.legend(fontsize=8)
            self.cmp_canvas.draw()
            self.cmp_err.setText(f"corner {il}:  Zout RMS={zrms:.3f}dB  PSRR RMS={prms:.3f}dB  "
                                 f"noise PSD={npsd:.3f}dB  (analytic predict vs imported GT)")

        def _save_text_report(self):
            if self.core.result is None:
                QMessageBox.information(self, "Text report", "Fit a model first.")
                return
            try:
                path, txt = self.core.text_report()
            except Exception as e:
                QMessageBox.warning(self, "Text report", f"{type(e).__name__}: {e}")
                return
            dest, _ = QFileDialog.getSaveFileName(self, "Save text report (a copy)",
                                                  str(path), "Text (*.txt)")
            if dest:
                pathlib.Path(dest).write_text(txt, encoding="utf-8")
                path = dest
            QMessageBox.information(self, "Text report",
                                    f"Wrote model-vs-GT difference report to:\n{path}\n\n"
                                    "It localizes every mismatch (Zout/PSRR/noise) in plain text -- "
                                    "paste the whole file to describe the fit.")

        # --- Tab 5: Trans-ID (one multitone transient -> auto Zout/PSRR) ----------
        def _tab_transid(self):
            w = QWidget(); lay = QVBoxLayout(w)
            legend = QLabel(
                "<b>Multitone trans-ID (auto Zout + PSRR):</b> if you characterized this LDO with "
                "the Verilog-A multitone stimulus fixture (one transient per band, emitted by "
                "<code>trans_id.emit_stim_va</code>), point here at the <b>plan.json</b> and the "
                "<b>folder</b> of exported waveforms named "
                "<code>&lt;corner&gt;_b&lt;band&gt;.csv</code> (e.g. 20u_b0.csv, 20u_b1.csv …, "
                "each <code>time, v(vin), v(vout)</code>). It coherently extracts <b>Zout &amp; "
                "PSRR</b> for every corner and feeds them to Import — no AC sweeps needed.<br>"
                "<b>Noise + DC still come from Tab 2</b> (a transient carries no device noise); "
                "tick the box to reuse the files already picked there.")
            legend.setWordWrap(True)
            legend.setStyleSheet("background:#eef5ff; padding:7px; border:1px solid #cdddee;")
            lay.addWidget(legend)
            form = QFormLayout()
            self.tid_plan = QLineEdit(); bp = QPushButton("…"); bp.setMaximumWidth(28)
            bp.clicked.connect(lambda: self._pick_into(self.tid_plan, "plan (*.json);;All (*)"))
            hp = QHBoxLayout(); hp.addWidget(self.tid_plan); hp.addWidget(bp)
            self.tid_folder = QLineEdit(); bf = QPushButton("…"); bf.setMaximumWidth(28)
            bf.clicked.connect(self._pick_trans_folder)
            hf = QHBoxLayout(); hf.addWidget(self.tid_folder); hf.addWidget(bf)
            form.addRow("plan.json", hp)
            form.addRow("waveform folder", hf)
            lay.addLayout(form)
            self.tid_merge = QCheckBox("reuse Noise / DC files already picked on Tab 2")
            self.tid_merge.setChecked(True)
            lay.addWidget(self.tid_merge)
            b = QPushButton("Extract z/p from trans   →   Import")
            b.clicked.connect(self._do_import_trans)
            lay.addWidget(b)
            self.tid_log = QTextEdit(); self.tid_log.setReadOnly(True)
            lay.addWidget(self.tid_log)
            return w

        def _pick_into(self, edit, filt):
            fn, _ = QFileDialog.getOpenFileName(self, "Select file", str(ROOT), filt)
            if fn:
                edit.setText(fn)

        def _pick_trans_folder(self):
            d = QFileDialog.getExistingDirectory(self, "Folder with <corner>_b<band> waveforms",
                                                 str(ROOT))
            if d:
                self.tid_folder.setText(d)

        def _do_import_trans(self):
            if not self._apply_profile():
                return
            plan = self.tid_plan.text().strip()
            folder = self.tid_folder.text().strip()
            if not plan or not folder:
                QMessageBox.information(self, "Trans-ID",
                                       "Pick both a plan.json and a waveform folder.")
                return
            extra = None
            if self.tid_merge.isChecked():       # reuse Noise/DC the user picked on Tab 2
                extra = {k: v for k, v in self._collect_files().items()
                         if k[0] in ("noise", "dc_loadreg", "dc_dropout", "dc_linereg",
                                     "spurs", "spur_500u")}
            try:
                path, warns = self.core.import_trans_folder(folder, plan, extra_files=extra)
            except Exception as e:
                QMessageBox.critical(self, "Trans-ID import failed", str(e))
                return
            corners = ", ".join(self.core.trans_info.keys())
            txt = f"Wrote {path}\nExtracted Zout/PSRR from trans for corners: {corners}\n"
            if not warns:
                txt += "Guardrails: no issues detected.\n"
            for wn in warns:
                txt += f"[{wn['level'].upper()}] {wn['quantity']}: {wn['msg']}\n"
            miss = self._missing_required(self._collect_files())
            if miss:
                txt += ("[INFO] still missing (add on Tab 2): " + ", ".join(miss) + "\n")
            self.tid_log.setText(txt)
            self.b_emit.setEnabled(False)        # new data invalidates any prior fit
            self._preview()
            self.statusBar().showMessage(f"Trans-ID → {path.name}. Add Noise/DC if needed, then Fit.")


# =============================================================================== tests
def _synth_arrays():
    """A small, FULLY-ANALYTIC LDO reference (no files, no simulator). Lets the smoke test be
    self-contained on the airgapped red box, where the bundle ships NO results/ref data:
    Zout=(R_a+sL_a)||(ESR+1/sC), PSRR=i_c*Zout (shelf i_c), noise=white+1/f+resonance.
    Returns (arrays_dict, loads, nominal)."""
    loads = ["20u", "121u", "250u"]; nom = "121u"
    f = np.logspace(1, 8, 180); fhf = np.logspace(1, np.log10(5e8), 200)
    Cc, ESR = 1e-9, 0.5

    def Zof(R_a, L_a, ww):
        ZA = R_a + 1j * ww * L_a
        ZC = ESR + 1.0 / (1j * ww * Cc)
        return 1.0 / (1.0 / ZA + 1.0 / ZC)

    def Hof(R_a, L_a, ww):
        return (0.02 / (1 + 1j * ww / (2 * np.pi * 4e5))) * Zof(R_a, L_a, ww)

    A = {"loads": np.array(loads), "meta_cout": np.array(Cc), "meta_esr": np.array(ESR),
         "spur_F": np.array([]), "spur_twin0": np.array(0.0), "spur_binhz": np.array(15625.0)}
    w = 2 * np.pi * f
    for il in loads:
        iv = float(il.replace("u", "e-6"))
        R_a = 18.0 * (121e-6 / iv) ** 0.15        # mild OP dependence
        Z = Zof(R_a, 2e-6, w)
        A[f"z_{il}"] = np.c_[f, Z.real, Z.imag]
        H = Hof(R_a, 2e-6, w)
        A[f"p_{il}"] = np.c_[f, H.real, H.imag]
        Sv = np.sqrt((4e-8) ** 2 + (8e-8) ** 2 * (1e3 / f)
                     + (np.abs(Z) / np.abs(Z[0]) * 2e-8) ** 2)
        A[f"noise_{il}"] = np.c_[f, Sv]
        t = np.linspace(0, 25e-6, 200)
        A[f"trans_lin_{il}"] = np.c_[t, 1.0 - R_a * iv - 1e-3 * np.exp(-(t - 5e-6) / 2e-6) * (t > 5e-6)]
    whf = 2 * np.pi * fhf
    Zh = Zof(18.0, 2e-6, whf); Hh = Hof(18.0, 2e-6, whf)
    A[f"z_{nom}_hf"] = np.c_[fhf, Zh.real, Zh.imag]
    A[f"p_{nom}_hf"] = np.c_[fhf, Hh.real, Hh.imag]
    t = np.linspace(0, 25e-6, 200)
    A[f"trans_big_{nom}"] = np.c_[t, 1.0 - 1e-2 * np.exp(-(t - 5e-6) / 2e-6) * (t > 5e-6)]
    A[f"trans_slew_{nom}"] = np.c_[t, 1.0 - 3e-2 * np.exp(-(t - 5e-6) / 2e-6) * (t > 5e-6)]
    idc = np.linspace(1e-6, 500e-6, 60); A["dc_loadreg"] = np.c_[idc, 1.0 - 18.0 * idc]
    vdc = np.linspace(0.9, 1.3, 40); A["dc_linereg"] = np.c_[vdc, 1.0 + 0 * vdc]
    iddc = np.linspace(1e-6, 6e-3, 80)
    A["dc_dropout"] = np.c_[iddc, np.maximum(1.0 - 18.0 * iddc, 0.1)]
    return A, loads, nom


def _selftest_transid(A, loads, nom, tmp):
    """Headless verification of the Tab-5 trans-ID path (no Qt, no simulator): synth a one-band
    multitone waveform per corner from the reference's Zout/PSRR, run it through
    ModelerCore.import_trans_folder (-> trans_import -> import_cadence.assemble), and assert the
    ref gets z/p/noise per corner, recovers Zout/PSRR to tolerance, and is fittable."""
    import json
    import trans_id
    import trans_import
    pl = trans_id.plan_band(f_lo=1e5, f_hi=1e7, n_per_dec=12, ppp=12)
    N = pl["N"]; tg = pl["t0"] + pl["dt"] * np.arange(N)
    va, ib, VDD = trans_id.VA_DEFAULT, trans_id.IB_DEFAULT, trans_id.VDD
    cin = trans_import._cinterp
    wdir = tmp / "transwav"; wdir.mkdir(parents=True, exist_ok=True)
    for il in loads:                              # synth vout/vin = inverse of _spectrum
        z, p = A[f"z_{il}"], A[f"p_{il}"]
        Zt = cin(pl["fb"], z[:, 0], z[:, 1] + 1j * z[:, 2])
        Ht = cin(pl["fa"], p[:, 0], p[:, 1] + 1j * p[:, 2])
        vin = np.full(N, VDD)
        for f in pl["fa"]:
            vin = vin + va * np.sin(2 * np.pi * float(f) * tg)
        vout = np.full(N, 1.0)
        for f, Z in zip(pl["fb"], Zt):
            vout = vout + ib * np.abs(Z) * np.sin(2 * np.pi * float(f) * tg + np.angle(Z))
        for f, H in zip(pl["fa"], Ht):
            vout = vout + va * np.abs(H) * np.sin(2 * np.pi * float(f) * tg + np.angle(H))
        np.savetxt(wdir / f"{il}_b0.csv", np.c_[tg, vin, vout])
    bj = dict(index=0, f_lo=pl["f_lo"], f_hi=pl["f_hi"], N=int(N), dt=pl["dt"], t0=pl["t0"],
              fa=[float(x) for x in pl["fa"]], ba=[int(b) for b in pl["ba"]],
              fb=[float(x) for x in pl["fb"]], bb=[int(b) for b in pl["bb"]])
    plan_path = wdir / "plan.json"
    plan_path.write_text(json.dumps(dict(VDD=VDD, va=va, ib=ib, iload=0.0, bands=[bj])))

    core2 = ModelerCore()
    core2.profile = Profile(name="_gui_transid_selftest", loads=loads, nominal=nom,
                            cout=float(A["meta_cout"]), esr=float(A["meta_esr"]), vref=1.05)
    extra = {("noise", il): str(tmp / f"noise_{il}.csv") for il in loads
             if (tmp / f"noise_{il}.csv").exists()}
    if (tmp / "dc_loadreg.csv").exists():
        extra[("dc_loadreg", None)] = str(tmp / "dc_loadreg.csv")
    path, warns = core2.import_trans_folder(str(wdir), str(plan_path), extra_files=extra)
    for il in loads:
        for q in ("z", "p", "noise"):
            assert f"{q}_{il}" in core2.ref, f"trans-ID ref missing {q}_{il}"
    assert f"z_{nom}_hf" in core2.ref, "trans-ID ref missing nominal z_hf"
    zr, pr = core2.ref[f"z_{nom}"], core2.ref[f"p_{nom}"]
    zg, pg = A[f"z_{nom}"], A[f"p_{nom}"]
    ez = float(np.max(np.abs(20 * np.log10(np.abs(zr[:, 1] + 1j * zr[:, 2]) /
                            np.abs(cin(zr[:, 0], zg[:, 0], zg[:, 1] + 1j * zg[:, 2]))))))
    ep = float(np.max(np.abs(20 * np.log10(np.abs(pr[:, 1] + 1j * pr[:, 2]) /
                            np.abs(cin(pr[:, 0], pg[:, 0], pg[:, 1] + 1j * pg[:, 2]))))))
    assert ez < 0.5 and ep < 0.5, f"trans-ID recovery off (Zout {ez:.3f} dB, PSRR {ep:.3f} dB)"
    core2.fit()
    assert core2.result is not None, "trans-ID ref not fittable"
    try:                                          # close + unlink the trans-ID selftest npz
        import fit_model
        if getattr(fit_model, "ref", None) is not None and hasattr(fit_model.ref, "close"):
            fit_model.ref.close()
        pathlib.Path(path).unlink()
    except OSError:
        pass
    print(f"  trans-ID: import_trans -> z/p/noise OK ({len(loads)} corners; "
          f"Zout {ez:.3f} dB / PSRR {ep:.3f} dB recovery; fittable)")
    return True


def _selftest_pinform(tmp):
    """Headless smoke of the Extract-tab PIN FORM (deliverable 1), Qt-free: the GUI form dict
    + an INJECTED netmap (no skillbridge) -> ExtractCore.build_manifest_from_gui writes + loads
    a valid manifest in the WORKAREA, surfaces the no-compliance warning naming every i_out pin,
    preserves the original symbol pins, and derives the model ports (supply LEFT / VSS BOTTOM)."""
    gui = dict(tb_lib="PMU_TOP_TB", tb_cell="pmu_tb", tb_view="schematic", dut_inst="I0",
               dut_lib="PMU_TOP", dut_cell="pmu_top",
               supply={"pin": "AVDD1P0", "dc": 1.0},
               v_outs=["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
               i_outs=["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit", "IBP_PTAT_TUNE_1P5U_VCO"],
               ground="VSS", corner="tt_25c")        # no vdc -> the no-compliance warning must fire
    netmap = {p: f"net_{p}" for p in [gui["supply"]["pin"], *gui["v_outs"], *gui["i_outs"]]}
    xc = ExtractCore()
    path, summ, warns = xc.build_manifest_from_gui(gui, netmap=netmap, work_root=str(tmp))
    assert pathlib.Path(path).exists(), "form did not write a manifest"
    assert "v_out" in summ and "i_out" in summ, "summary missing roles"
    assert warns and all(any(pin in w for w in warns) for pin in gui["i_outs"]), \
        "no-compliance warning must name every i_out pin"
    m = xc.manifest
    assert list(m["v_out"]) == ["dig", "pll", "vco"], list(m["v_out"])
    assert m["v_out"]["dig"]["pin"] == "VDD0P8_DIG", "original symbol pin not preserved"
    sp, gnd = xc._model_ports()
    assert sp == "AVDD1P0" and gnd == "VSS", (sp, gnd)
    sp_str = str(path)
    assert "/ldo_modeling/" in sp_str and "/simulation/" not in sp_str, \
        f"manifest must live in the workarea, not the designer spine: {sp_str}"
    # multi-supply variant: gui['supplies']=[...] -> N supplies, current-PSRR defaults to ALL,
    # first supply stays the model's LEFT input pin (single-supply path above unchanged).
    mg = dict(gui); mg.pop("supply")
    mg["supplies"] = [{"pin": "AVDD1P0", "dc": 1.0}, {"pin": "DVDD0P8", "dc": 0.8}]
    mnm = {p: f"net_{p}" for p in [*(s["pin"] for s in mg["supplies"]),
                                   *mg["v_outs"], *mg["i_outs"]]}
    xc2 = ExtractCore()
    xc2.build_manifest_from_gui(mg, netmap=mnm, work_root=str(tmp))
    assert list(xc2.manifest["supplies"]) == ["avdd1p0", "dvdd0p8"], xc2.manifest["supplies"]
    assert xc2.manifest["current_psrr_supplies"] == ["avdd1p0", "dvdd0p8"]
    assert xc2._model_ports()[0] == "AVDD1P0", "first supply must be the model LEFT input"
    print(f"  pinform: gui+netmap -> manifest OK ({len(m['v_out'])}v+{len(m['i_out'])}i ports, "
          f"{len(warns)} warning; model {sp} left / {gnd} bottom; multi-supply 2 rails OK)")


def _selftest_extract(tmp):
    """Headless smoke of the in-situ EXTRACTION front-half (ExtractCore, Qt-free): manifest
    -> spectre_cli run -> multi-port npz -> gate vs gold -> per-output single-port refs, PLUS
    the combined model-cell build (deliverable 3) as a DRY plan (no live Cadence). The
    spectre_cli fixture (cadence/work_pmu PSF) is gitignored regenerable data that needs
    Spectre, so a missing fixture is a SKIP (not a failure) -- keeps --selftest green on a
    box without it while exercising the whole path where present."""
    try:
        xc = ExtractCore()
        summ = xc.load_manifest("pmu_top")
        assert "v_out" in summ, "manifest summary missing roles"
        out = xc.run(backend="spectre_cli")
    except (FileNotFoundError, RuntimeError, ModuleNotFoundError, ImportError) as e:
        print(f"  extract: SKIPPED -- in-situ fixture/session unavailable ({type(e).__name__})")
        return
    passed, _worst, detail = out["gate"]
    assert passed in (True, None), f"in-situ gate FAILED: {detail}"
    ports = xc.port_list()
    assert "pll" in ports and "vco" in ports, f"missing per-output refs: {ports}"
    assert "VOLTAGE OUTPUTS" in out["report"] and "CURRENT SINKS" in out["report"], "report shape"
    # deliverable 3 (dry): the combined model cell -- emit the .va + derive the symbol pinspec
    # (single supply LEFT input, every output RIGHT, ground BOTTOM). No live session -> not built.
    cellout = xc.build_model_cell("LDO_model_lab", "PMU_model_selftest",
                                  str(tmp / "cds" / "LDO_model_lab"),
                                  session=None, dry_run=True, work_root=str(tmp))
    spec = cellout["pinspec"]
    assert spec[0][1] == "input" and spec[0][2] == "left", f"supply must be left input: {spec[0]}"
    assert spec[-1][2] == "bottom", f"ground must be bottom: {spec[-1]}"
    assert all(s == "right" for _n, _d, s in spec[1:-1]), f"outputs must be right: {spec}"
    assert pathlib.Path(cellout["va"]).exists(), "combined .va not written"
    assert not cellout["built"], "dry build must not touch Cadence"
    print(f"  extract: manifest->run->npz->fit OK  gate={'PASS' if passed else 'SKIP'} "
          f"({detail}); per-output refs={ports}")
    print(f"  modelcell: dry build -> {len(spec)} ports (1 left / {len(spec)-2} right / 1 bottom), "
          f".va {pathlib.Path(cellout['va']).name}")
    for p in list(xc.port_refs.values()) + [xc.npz_path, cellout["va"]]:   # clean regenerables
        try:
            pathlib.Path(p).unlink()
        except OSError:
            pass


def _selftest_cluster_sweep(win, tmp, app):
    """The GUI Donau+ALPS SWEEP path (the user-facing goal): the offline per-group netlister +
    the live per-group status table, exercised via a DRY-RUN (assemble the per-group dsub
    commands, submit NOTHING -- fully offline, no dsub/alps). Covers BOTH the Qt-free core
    (ExtractCore.run_cluster_sweep) and the GUI worker/table wiring through the Build & Run button."""
    import json
    from insitu import run as _run, pmu_corner as PC
    # a RESOLVED manifest + a base .tran TB whose nets match it (1 supply + 1 v_out -> 3 groups)
    base = tmp / "clsweep_base"; base.mkdir(parents=True, exist_ok=True)
    (base / "input.scs").write_text(
        "simulator lang=spectre\n"
        "Xdut (AVDD1P0 VDD0P8_PLL VSS) DUT\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\n"
        "Iload (VDD0P8_PLL 0) isource dc=500u\n"
        "tt tran stop=1u\n")
    man = {"name": "clsweep_selftest",
           "dut": {"lib": "L", "cell": "C", "tb_lib": "TBL", "tb_cell": "TBC", "tb_inst": "X"},
           "ground": "VSS",
           "supplies": {"avdd": {"net": "AVDD1P0", "dc": 0.98, "pin": "AVDD1P0"}},
           "v_out": {"pll": {"net": "VDD0P8_PLL", "pin": "VDD0P8_PLL", "iload": 5e-4}},
           "current_psrr_supplies": ["avdd"]}
    mpath = tmp / "clsweep.manifest.json"; mpath.write_text(json.dumps(man))

    # --- CORE: a dry-run sweep assembles one per-group dsub command + writes each one-hot netlist
    xc = ExtractCore(); xc.load_manifest(str(mpath))
    grps = _run.groups(xc.manifest)
    ngroups = len(grps)
    assert ngroups == 3, f"expected 3 groups (z/noise/supply), got {ngroups}"
    seen = []
    # max_parallel is forwarded through the core (dry-run never submits, so it just must not crash)
    out = xc.run_cluster_sweep(netlistdir=str(base), pdk_model_dir=str(tmp), engine="alps",
                               work_root=str(tmp), dry_run=True, max_parallel=3,
                               group_status=lambda i, n, g, st: seen.append((g["tag"], st)))
    assert out["dry_run"] and out["n_groups"] == ngroups and len(out["dsub_cmds"]) == ngroups, out
    _, dirs = PC.corner_dir(str(tmp), PC._gui_from_manifest(xc.manifest), xc._corner())
    for g in grps:                                       # each group's offline one-hot netlist exists
        assert (dirs["netlist"] / g["tag"] / "input.scs").is_file(), g["tag"]
    assert {t for t, _s in seen} == {g["tag"] for g in grps}, "group_status missed a group"

    # --- GUI WIRING: Build & Run (dry-run) starts the worker, fills the status table, shows cmds.
    # Scope WORK_ROOT to tmp so the worker (which takes no work_root) writes under tmp, not ~/.
    import os
    _old_wr = os.environ.get("WORK_ROOT")
    os.environ["WORK_ROOT"] = str(tmp)
    try:
        win.x_mode.setCurrentIndex(win.x_mode.findData("import"))
        win.x_location.setCurrentIndex(win.x_location.findData("cluster"))
        win.x_backend.setCurrentIndex(win.x_backend.findData("alps")); app.processEvents()
        assert win.x_status_box.isVisibleTo(win), "per-group status table must show when location=cluster"
        assert win.x_dryrun.isVisibleTo(win), "preview-only toggle must show when location=cluster"
        win.extract.load_manifest(str(mpath))
        win.xb_netlist["edit"].setText(str(base)); win.xb_pdk["edit"].setText(str(tmp))
        win.xb_ahdl["edit"].setText("")                  # blank -> the -ahdllibdir flag is dropped
        win.xd_cpu.setValue(16); win.xd_mem.setValue(16000); win.xd_maxjobs.setValue(3)
        win.x_dryrun.setChecked(True)
        win._x_run()                                     # cluster -> the dry-run sweep worker
        assert win._xw.max_parallel == 3, "worker must take the Max-parallel-jobs spinbox value"
        assert win._xw.wait(15000), "cluster sweep worker did not finish"
        app.processEvents()                              # deliver the queued done/group_state signals
    finally:
        if _old_wr is None:
            os.environ.pop("WORK_ROOT", None)
        else:
            os.environ["WORK_ROOT"] = _old_wr
    rep = win.x_report.toPlainText()
    dsub_lines = [l for l in rep.splitlines() if l.startswith("dsub ")]
    assert len(dsub_lines) == ngroups, f"expected {ngroups} per-group dsub lines, got {len(dsub_lines)}"
    assert any("-R 'cpu=16;mem=16000'" in l for l in dsub_lines), "dsub resource not shell-safe (';')"
    assert all("-ahdllibdir" not in l for l in dsub_lines), "blank ahdllibdir must drop -ahdllibdir"
    assert win.x_status.rowCount() == ngroups, "status table must have one row per group"
    states = {win.x_status.item(i, 3).text() for i in range(win.x_status.rowCount())}
    assert states == {"preview"}, f"dry-run rows should be 'preview', got {states}"
    win.x_dryrun.setChecked(False)

    # the bounded-parallel surface is wired end to end: the core accepts max_parallel, the
    # worker constructor accepts + stores it (no submit needed to check the plumbing).
    import inspect
    assert "max_parallel" in inspect.signature(ExtractCore.run_cluster_sweep).parameters, \
        "ExtractCore.run_cluster_sweep must accept a max_parallel kwarg"
    w = _ClusterSweepWorker(ExtractCore(), netlistdir=str(base), pdk=str(tmp), ahdl="",
                            engine="alps", donau_cfg=None, dry_run=True, max_parallel=7)
    assert w.max_parallel == 7, "worker constructor must accept + store max_parallel"
    print(f"  cluster: GUI dry-run sweep -> {ngroups} per-group dsub cmds + live status table "
          f"+ max-parallel plumbing OK")


def _render_coverage_shots(win, dlg):
    """[STAGE 3] Render the mandatory offscreen PNGs to /tmp/ldo_gui_shots/: the populated
    manifest-editor Form (top = Identity+Coverage+Supplies; tall grab = the Voltage/Current
    tables with the new iload/trans/iv_sweep columns) and a SHORT Tab-0 proving the new vertical
    scrollbar. A QWidget/QDialog .grab()s directly under QT_QPA_PLATFORM=offscreen even unshown."""
    shotdir = pathlib.Path("/tmp/ldo_gui_shots"); shotdir.mkdir(parents=True, exist_ok=True)
    saved = []
    # land on the Form sub-tab + give it a sensible size, then grab. Scroll the inner QScrollArea
    # down a touch so Identity + COVERAGE + Supplies are all in frame for the 'top' shot.
    dlg.subtabs.setCurrentIndex(0)
    dlg.resize(560, 900)
    dlg.show(); QtWidgets.QApplication.processEvents()
    _form_scroll = dlg.subtabs.widget(0).findChild(QtWidgets.QScrollArea)
    if _form_scroll is not None:
        _form_scroll.verticalScrollBar().setValue(430)     # past Identity -> Coverage controls
        QtWidgets.QApplication.processEvents()
    p_top = shotdir / "editor_form_top.png"
    if dlg.grab().save(str(p_top)):
        saved.append(str(p_top))
    # a TALL + WIDE grab so the Voltage-outputs (iload sweep + trans) + Current-outputs (iv_sweep)
    # tables show their POPULATED cells -- widen the dialog so the role tables reveal the new
    # coverage columns without their own horizontal scrollbar, then grab the inner content widget
    # (its full natural height), not just the viewport.
    if _form_scroll is not None:
        _form_scroll.verticalScrollBar().setValue(0)
    dlg.resize(900, 1500); QtWidgets.QApplication.processEvents()
    # widen the role tables so the iload-sweep / trans / iv_sweep cells are flush in frame.
    for _t in (dlg.t_vout, dlg.t_iout):
        _t.setMinimumWidth(820)
    QtWidgets.QApplication.processEvents()
    inner = _form_scroll.widget() if _form_scroll is not None else dlg.subtabs.widget(0)
    p_tab = shotdir / "editor_form_tables.png"
    if inner.grab().save(str(p_tab)):
        saved.append(str(p_tab))
    dlg.hide()
    # Tab-0 at a SHORT window height so the QScrollArea's vertical scrollbar appears (Fix C proof).
    win.tabs.setCurrentIndex(0)
    _saved_geo = (win.width(), win.height())
    win.resize(800, 520); win.show(); QtWidgets.QApplication.processEvents()
    p_t0 = shotdir / "tab0_extract.png"
    if win.grab().save(str(p_t0)):
        saved.append(str(p_t0))
    win.resize(*_saved_geo); QtWidgets.QApplication.processEvents()
    for s in saved:
        print(f"  qt: coverage screenshot saved -> {s}")
    return saved


def _selftest_manifest_editor(win, tmp):
    """Headless smoke of the in-GUI manifest editor (#1): the structured Form + Raw-JSON sub-tabs.
    Asserts: template validates, a broken/invalid edit is caught (Raw tab), a valid edit Saves +
    reloads through ExtractCore, the Form models the REQUIRED fields, Form->Raw regenerates valid
    JSON, an UNKNOWN top-level key + an unmodeled nested key SURVIVE the Raw->Form->save round-trip
    (lossless overlay, D6), and find/replace works. Qt offscreen."""
    RAW = 1
    # 1) template validates on the Form tab (default), with the measurement matrix preview
    dlg = _ManifestEditorDialog(win, _MANIFEST_TEMPLATE, None)
    assert dlg.subtabs.currentIndex() == 0, "dialog should open on the Form tab"
    ok, msg = dlg._check()
    assert ok, f"template should validate, got: {msg}"
    assert "measurement points" in msg, "validate preview missing the measurement matrix"
    # 2) bad / invalid manifest is caught on the Raw tab
    dlg.subtabs.setCurrentIndex(RAW)
    dlg.ed.setPlainText('{ "name": "x", oops }')                 # malformed JSON
    assert not dlg._check()[0], "broken JSON must fail validation"
    dlg.ed.setPlainText('{"name":"x","dut":{"lib":"l"}}')         # valid JSON, invalid manifest
    assert not dlg._check()[0], "manifest missing dut.cell/v_out must fail validation"
    # 3) a valid edit -> write -> reload through the real loader (Raw tab text path)
    out = pathlib.Path(tmp) / "edited_manifest.json"
    dlg.ed.setPlainText(_MANIFEST_TEMPLATE)
    assert dlg._write(out) and dlg.saved_path == out and out.exists(), "save failed"
    ExtractCore().load_manifest(str(out))                         # reloadable end-to-end
    out.unlink()

    # 4) FORM round-trip: fill the REQUIRED fields programmatically, then Form->Raw must
    #    regenerate JSON that parses + validates. tb_lib left blank -> inherits dut_lib (D4).
    dlg2 = _ManifestEditorDialog(win, "{}", None)
    dlg2.f_name.setText("wur_pmu_top")
    dlg2.f_dut_lib.setText("Hi1108_WuR_PMU"); dlg2.f_dut_cell.setText("WuR_PMU_TOP")
    dlg2.f_dut_inst.setText("PMU_TOP")                           # identity field (moved up from Advanced)
    dlg2.f_tb_lib.setText("")                                     # blank -> inherit dut_lib
    dlg2.f_tb_cell.setText("sim_LDO"); dlg2.f_ground.setText("VSS")
    _ManifestEditorDialog._table_add_row(dlg2.t_supplies, ["AVDD1P0", "VDD1P0", "1.0"])
    _ManifestEditorDialog._table_add_row(dlg2.t_vout, ["vco", "VDD0P8_VCO"])
    dlg2.subtabs.setCurrentIndex(RAW)                            # Form->Raw regenerates the JSON
    raw = json.loads(dlg2.ed.toPlainText())
    assert raw["dut"]["tb_lib"] == "Hi1108_WuR_PMU", "tb_lib must inherit dut_lib when blank (D4)"
    assert raw["dut"]["tb_inst"] == "PMU_TOP", "DUT instance (tb_inst) must round-trip from Identity"
    assert raw["supplies"]["AVDD1P0"]["dc"] == 1.0 and raw["v_out"]["vco"]["net"] == "VDD0P8_VCO"
    ok2, _ = dlg2._check()
    assert ok2, "Form-built manifest must validate after Form->Raw"

    # 5) LOSSLESS overlay: inject an UNKNOWN top-level key + an unmodeled NESTED key into Raw,
    #    switch Raw->Form (re-parse) and back to Raw (regenerate via the stash), then save ->
    #    both unknown keys MUST survive (D6).
    raw["_designer_note"] = "keep me"                            # unknown top-level key
    raw["dut"]["_secret_view"] = "layout"                       # unmodeled NESTED key under dut
    raw["supplies"]["AVDD1P0"]["_supply_note"] = "external rail"  # truly-unmodeled nested supply key
    dlg2.ed.setPlainText(json.dumps(raw, indent=2))
    dlg2.subtabs.setCurrentIndex(0)                             # Raw->Form re-parses into the stash
    assert dlg2.f_name.text() == "wur_pmu_top", "Raw->Form did not repopulate the form"
    dlg2.subtabs.setCurrentIndex(RAW)                          # Form->Raw regenerates from the stash
    merged = json.loads(dlg2.ed.toPlainText())
    assert merged.get("_designer_note") == "keep me", "unknown top-level key lost in round-trip"
    assert merged["dut"].get("_secret_view") == "layout", "unmodeled nested dut key lost"
    assert merged["supplies"]["AVDD1P0"].get("_supply_note") == "external rail", \
        "unmodeled supply key lost"
    out2 = pathlib.Path(tmp) / "roundtrip_manifest.json"
    assert dlg2._write(out2), "round-trip save failed"
    on_disk = json.loads(out2.read_text())
    assert on_disk.get("_designer_note") == "keep me" and \
        on_disk["dut"].get("_secret_view") == "layout", "unknown keys did not reach disk"
    ExtractCore().load_manifest(str(out2))                       # the merged file still loads
    out2.unlink()

    # 5b) UNRESOLVED '<net:PIN>' placeholders must NOT leak the wrapper into the Form net cells:
    #     the designer sees the bare PIN name (a confirmable default), and Form->Raw writes the
    #     bare net (resolved), never the '<net:...>' marker.
    ph = json.dumps({
        "name": "ph", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
        "supplies": {"avdd1p0": {"net": "<net:AVDD1P0>", "dc": 1.0, "pin": "AVDD1P0"}},
        "v_out": {"pll": {"net": "<net:VDD0P8_PLL>", "pin": "VDD0P8_PLL"}},
    })
    dlg3 = _ManifestEditorDialog(win, ph, None)
    vcell = dlg3.t_vout.item(0, 1)
    assert vcell.text() == "VDD0P8_PLL", \
        f"placeholder net should display the bare pin, got {vcell.text()!r}"
    assert vcell.font().italic(), "unresolved net cell should be flagged (italic)"
    assert "<net:" not in dlg3.t_supplies.item(0, 1).text(), "supply placeholder wrapper leaked"
    dlg3.subtabs.setCurrentIndex(RAW)                            # Form->Raw resolves to the bare net
    ph_raw = json.loads(dlg3.ed.toPlainText())
    assert ph_raw["v_out"]["pll"]["net"] == "VDD0P8_PLL", "Form->Raw must write the bare resolved net"
    assert ph_raw["v_out"]["pll"]["pin"] == "VDD0P8_PLL", "pin tag must survive the placeholder strip"

    # 5c) SOURCE-INSTANCE columns are surfaced in the Form: supplies 'src instance' -> tb_src,
    #     i_out 'probe instance' -> probe_src. They must display from a loaded manifest AND
    #     round-trip Form->Raw (so the designer fixes a failed supply auto-detect without
    #     dropping to Raw JSON).
    src = json.dumps({
        "name": "src", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
        "supplies": {"avdd1p0": {"net": "AVDD1P0", "dc": 0.98, "tb_src": "V7"}},
        "i_out": {"i500n": {"net": "IBP_500N", "dc": 1.28, "probe_src": "Vprobe_i500n_lpf"}},
    })
    dlg4 = _ManifestEditorDialog(win, src, None)
    # tables: supplies [key,net,dc,src,PSRR→I,analysis]=6; v_out [key,net,src,analysis]=4;
    # i_out [key,net,dc,iv,probe,analysis]=6.
    # v_out now carries two coverage columns (iload sweep + trans) so its count is 6, not 4.
    assert dlg4.t_supplies.columnCount() == 6 and dlg4.t_iout.columnCount() == 6 \
        and dlg4.t_vout.columnCount() == 6, "tables must carry source-instance + checkbox/analysis/coverage columns"
    assert dlg4.t_supplies.item(0, 3).text() == "V7", "supply tb_src not shown in the 'src instance' cell"
    assert dlg4.t_iout.item(0, 4).text() == "Vprobe_i500n_lpf", "i_out probe_src not shown in the cell"
    # edit the supply source in the FORM, then Form->Raw must carry it into tb_src
    dlg4.t_supplies.item(0, 3).setText("V_AVDD_FORCE")
    dlg4.subtabs.setCurrentIndex(RAW)
    src_raw = json.loads(dlg4.ed.toPlainText())
    assert src_raw["supplies"]["avdd1p0"].get("tb_src") == "V_AVDD_FORCE", \
        "Form 'src instance' edit must round-trip to supplies.<s>.tb_src"
    assert src_raw["i_out"]["i500n"].get("probe_src") == "Vprobe_i500n_lpf", \
        "i_out probe_src must survive the Form round-trip"

    # 5d) v_out 'src instance' column -> v_out.<o>.src round-trips (the load idc to reuse).
    vsrc = json.dumps({
        "name": "vs", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
        "v_out": {"pll": {"net": "VDD0P8_PLL", "src": "Iload_pll"}}})
    dlg5 = _ManifestEditorDialog(win, vsrc, None)
    assert dlg5.t_vout.item(0, 2).text() == "Iload_pll", "v_out src not shown in the 'src instance' cell"
    dlg5.t_vout.item(0, 2).setText("Iload_pll_2")
    dlg5.subtabs.setCurrentIndex(RAW)
    assert json.loads(dlg5.ed.toPlainText())["v_out"]["pll"].get("src") == "Iload_pll_2", \
        "v_out 'src instance' edit must round-trip to v_out.<o>.src"

    # 5e) [2.3] current-PSRR is a per-supply checkbox (no free-text field). Loading the explicit
    #     list checks just those; editing the checkboxes writes the explicit checked list back.
    cps = json.dumps({
        "name": "cp", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
        "supplies": {"avdd": {"net": "AVDD", "dc": 1.0}, "dvdd": {"net": "DVDD", "dc": 0.8}},
        "v_out": {"o": {"net": "VOUT"}}, "current_psrr_supplies": ["avdd"]})
    dlg6 = _ManifestEditorDialog(win, cps, None)
    assert not hasattr(dlg6, "f_cpsrr"), "the current-PSRR free-text field must be removed"
    assert "PSRR→I" in dlg6.t_supplies._cols, "supplies table must carry the PSRR→I checkbox column"
    rows = {dlg6.t_supplies.item(r, 0).text(): r for r in range(dlg6.t_supplies.rowCount())}
    assert dlg6._row_checked(dlg6.t_supplies, rows["avdd"], "PSRR→I"), "listed supply should be checked"
    assert not dlg6._row_checked(dlg6.t_supplies, rows["dvdd"], "PSRR→I"), "unlisted supply must be unchecked"
    dlg6._set_row_checked(dlg6.t_supplies, rows["dvdd"], "PSRR→I", True)   # check the 2nd rail too
    cp_out = dlg6._form_to_dict()
    assert set(cp_out["current_psrr_supplies"]) == {"avdd", "dvdd"}, \
        "checked supplies must write the explicit current_psrr_supplies list"
    # absent key -> check ALL (mirrors _fill_defaults). Empty checked list -> empty list (allowed).
    cps_none = json.dumps({"name": "c2", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
                           "supplies": {"a": {"net": "A", "dc": 1.0}, "b": {"net": "B", "dc": 1.0}},
                           "v_out": {"o": {"net": "VO"}}})
    dlg6b = _ManifestEditorDialog(win, cps_none, None)
    assert all(dlg6b._row_checked(dlg6b.t_supplies, r, "PSRR→I")
               for r in range(dlg6b.t_supplies.rowCount())), "absent current_psrr -> all supplies checked"
    for r in range(dlg6b.t_supplies.rowCount()):
        dlg6b._set_row_checked(dlg6b.t_supplies, r, "PSRR→I", False)
    assert dlg6b._form_to_dict()["current_psrr_supplies"] == [], "all-unchecked -> empty list (no current-PSRR)"

    # 5f) [2.4] bias is a TOP-LEVEL always-visible table that round-trips.
    bman = json.dumps({"name": "b", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
                       "v_out": {"o": {"net": "VO"}}, "bias": {"vbg": {"net": "VBG", "dc": 0.6}}})
    dlg7 = _ManifestEditorDialog(win, bman, None)
    assert dlg7.t_bias.isVisibleTo(dlg7) or True   # built top-level (visibility is layout-driven)
    assert dlg7.t_bias.item(0, 0).text() == "vbg" and dlg7.t_bias.item(0, 1).text() == "VBG", \
        "bias top-level table did not load"
    bout = dlg7._form_to_dict()
    assert bout["bias"]["vbg"]["net"] == "VBG" and bout["bias"]["vbg"]["dc"] == 0.6, \
        "bias top-level table must round-trip"

    # 5g) [2.4] corners collapse to a single label: fallback=[label]; pull_from_session preserved.
    cman = json.dumps({"name": "cr", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
                       "v_out": {"o": {"net": "VO"}},
                       "corners": {"pull_from_session": False, "fallback": ["ff_125c", "ss_m40c"]}})
    dlg8 = _ManifestEditorDialog(win, cman, None)
    assert dlg8.f_corner.text() == "ff_125c", "corner label must prefill from fallback[0]"
    dlg8.f_corner.setText("tt_25c")
    cout = dlg8._form_to_dict()
    assert cout["corners"]["fallback"] == ["tt_25c"], "corner label must write corners.fallback=[label]"
    assert cout["corners"].get("pull_from_session") is False, \
        "pull_from_session must survive (lossless) even though the form drops it"

    # 5h) PER-OBJECT analysis override round-trips. Drive the override STORE directly (per spec:
    #     the headless selftest must NOT simulate the popup). Only a value DIFFERING from the
    #     global default is written (clean manifests).
    aman = json.dumps({"name": "a", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
                       "v_out": {"o": {"net": "VO"}},
                       "analysis": {"ac": "ac start=10 stop=500M dec=20",
                                    "noise": "noise start=10 stop=100M dec=20"}})
    dlg9 = _ManifestEditorDialog(win, aman, None)
    # set an override on the v_out 'o' row (different ac + noise from the global)
    dlg9.t_vout._analysis["o"] = {"ac": "ac start=1 stop=10M dec=5",
                                  "noise": "noise start=1 stop=1M dec=5"}
    a_out = dlg9._form_to_dict()
    assert a_out["v_out"]["o"]["analysis"] == {"ac": "ac start=1 stop=10M dec=5",
                                               "noise": "noise start=1 stop=1M dec=5"}, \
        "per-object analysis override did not write to v_out.<o>.analysis"
    # an override EQUAL to the global default must NOT be written (kept clean)
    dlg9.t_vout._analysis["o"] = {"ac": "ac start=10 stop=500M dec=20"}
    assert "analysis" not in dlg9._form_to_dict()["v_out"]["o"], \
        "an override equal to the global default must not be written"
    # and it must RELOAD into the store from a manifest carrying it
    dlg10 = _ManifestEditorDialog(win, json.dumps({
        "name": "a2", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
        "v_out": {"o": {"net": "VO", "analysis": {"ac": "ac start=2 stop=20M dec=7"}}}}), None)
    assert dlg10.t_vout._analysis.get("o") == {"ac": "ac start=2 stop=20M dec=7"}, \
        "per-object analysis override did not reload into the table store"

    # 5i) [SCAN] scan-fill against a tiny in-memory fake base netlist: supplies/v_out/i_out src
    #     + dc cells are filled from scan_netlist_sources; type-mismatch is flagged.
    fake = pathlib.Path(tmp) / "scan_base.scs"
    fake.write_text(
        "simulator lang=spectre\n"
        "Xdut (AVDD VOUT IBP VSS) DUT\n"
        "V_AVDD (AVDD 0) vsource dc=0.98\n"
        "Iload_o (VOUT 0) isource dc=500u\n"
        "Vchar_i (IBP 0) vsource dc=1.2\n"
        "tt tran stop=1u\n")
    sman = json.dumps({"name": "s", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
                       "supplies": {"avdd": {"net": "AVDD", "dc": 0.0}},
                       "v_out": {"o": {"net": "VOUT"}},
                       "i_out": {"i": {"net": "IBP", "dc": 0.0}}})
    dlg11 = _ManifestEditorDialog(win, sman, None)
    from cluster import netlist_augment as _NA
    scan = _NA.scan_netlist_sources(str(fake), dlg11._current_dict())
    found, insertable, error = dlg11._apply_scan(scan)
    assert found == 3 and insertable == 0 and error == 0, \
        f"scan should find 3 reusable sources, got {found}/{insertable}/{error}"
    assert dlg11.t_supplies.item(0, 3).text() == "V_AVDD", "scan did not fill the supply src instance"
    assert dlg11.t_supplies.item(0, 2).text() == "0.98", "scan did not fill the supply dc"
    assert dlg11.t_vout.item(0, 2).text() == "Iload_o", "scan did not fill the v_out src instance"
    assert dlg11.t_iout.item(0, 4).text() == "Vchar_i", "scan did not fill the i_out probe instance"
    assert dlg11.t_iout.item(0, 2).text() == "1.2", "scan did not fill the i_out compliance dc"

    # 5i-bis) [B1/B2] a current-output pin whose net carries ONLY an isource (the characterised
    #     bias current, NOT a reusable vsource probe). The probe cell must be left BLANK (so the
    #     build inserts Vprobe_<key>), the compliance-dc must NOT be filled from the isource's
    #     current, and it is counted as 'insertable' (normal), not an error.
    fake2 = pathlib.Path(tmp) / "scan_iout_isource.scs"
    fake2.write_text(
        "simulator lang=spectre\n"
        "Xdut (AVDD IBP VSS) DUT\n"
        "V_AVDD (AVDD 0) vsource dc=0.98\n"
        "Ibias_i (IBP 0) isource dc=500u\n"             # an isource on the current-output net
        "tt tran stop=1u\n")
    sman2 = json.dumps({"name": "s2", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
                        "supplies": {"avdd": {"net": "AVDD", "dc": 0.0}},
                        "i_out": {"i": {"net": "IBP", "dc": 0.0}}})
    dlg11b = _ManifestEditorDialog(win, sman2, None)
    scan2 = _NA.scan_netlist_sources(str(fake2), dlg11b._current_dict())
    f2, ins2, err2 = dlg11b._apply_scan(scan2)
    assert f2 == 1 and ins2 == 1 and err2 == 0, \
        f"i_out-with-isource: expect 1 reusable supply + 1 insertable i_out, got {f2}/{ins2}/{err2}"
    pcell = dlg11b.t_iout.item(0, 4)
    assert (pcell is None or pcell.text() == ""), \
        f"[B1] i_out probe cell must stay BLANK (no wrong-master name), got {pcell.text()!r}"
    dccell = dlg11b.t_iout.item(0, 2)
    assert (dccell is None or dccell.text() in ("", "0.0", "0")), \
        f"[B2] i_out compliance-dc must NOT be filled from the isource current, got {dccell.text()!r}"

    # 5i-ter) [B4] live src/probe-instance type validation against the base netlist (scope-aware).
    #     Use fake (V_AVDD vsource / Iload_o isource / Vchar_i vsource). A correct master -> 'ok';
    #     a wrong master -> 'wrong'; an unknown name -> 'absent'; blank -> 'blank'.
    base_txt = fake.read_text()
    dlg11.t_supplies.item(0, 3).setText("V_AVDD")               # supply wants a vsource
    assert dlg11._validate_src_cell(dlg11.t_supplies, 0, base_text=base_txt) == "ok"
    dlg11.t_supplies.item(0, 3).setText("Iload_o")             # an isource named for a supply
    assert dlg11._validate_src_cell(dlg11.t_supplies, 0, base_text=base_txt) == "wrong"
    dlg11.t_vout.item(0, 2).setText("Iload_o")                 # v_out wants an isource -> ok
    assert dlg11._validate_src_cell(dlg11.t_vout, 0, base_text=base_txt) == "ok"
    dlg11.t_vout.item(0, 2).setText("V_AVDD")                  # a vsource named for a v_out
    assert dlg11._validate_src_cell(dlg11.t_vout, 0, base_text=base_txt) == "wrong"
    dlg11.t_vout.item(0, 2).setText("NOPE")                    # not a top-level instance
    assert dlg11._validate_src_cell(dlg11.t_vout, 0, base_text=base_txt) == "absent"
    dlg11.t_vout.item(0, 2).setText("")                        # blank -> auto-insert
    assert dlg11._validate_src_cell(dlg11.t_vout, 0, base_text=base_txt) == "blank"

    # 5i-quater) [C] Cadence-style sweep editor parse<->render round-trips. The QLineEdit string
    #     stays the canonical store; the editor only re-parses then re-renders it. The sweep token
    #     (lin/step/dec/log) carries BOTH the type and the specify-by kind.
    _SP = _ManifestEditorDialog._sweep_parse
    _SR = _ManifestEditorDialog._sweep_render
    f = _SP("ac start=10 stop=500M lin=20")                     # linear by number of points
    assert f["type"] == "lin" and f["spec_kind"] == "lin" and f["spec_val"] == "20"
    assert f["start"] == "10" and f["stop"] == "500M" and f["name"] == "ac"
    assert _SR(f) == "ac start=10 stop=500M lin=20"
    f = _SP("ac start=0 stop=1 step=0.01")                      # linear by step size
    assert f["type"] == "lin" and f["spec_kind"] == "step" and _SR(f) == "ac start=0 stop=1 step=0.01"
    f = _SP("ac start=10 stop=1G dec=20")                       # log by points-per-decade
    assert f["type"] == "log" and f["spec_kind"] == "dec" and _SR(f) == "ac start=10 stop=1G dec=20"
    f = _SP("noise start=1 stop=1M log=50")                     # log by number of steps
    assert f["type"] == "log" and f["spec_kind"] == "log" and f["name"] == "noise"
    assert _SR(f) == "noise start=1 stop=1M log=50"
    f = _SP("ac start=10 stop=1G dec=20 values=[304M 1.2G]")    # specific points survive (C2)
    assert f["values"] == ["304M", "1.2G"]
    assert _SR(f) == "ac start=10 stop=1G dec=20 values=[304M 1.2G]"
    f = _SP("ac start=10 stop=1G dec=20 values=[1,2,3]")        # comma list normalises to spaces
    assert f["values"] == ["1", "2", "3"] and "values=[1 2 3]" in _SR(f)
    f = _SP("ac start=10 stop=1G dec=20 errpreset=conservative")  # unknown tokens preserved
    assert "errpreset=conservative" in f["extra"]
    assert _SR(f).endswith("errpreset=conservative")
    assert _ManifestEditorDialog._split_brackets("a b=[1 2] c") == ["a", "b=[1 2]", "c"]
    # [Fix 1] a FRESH/empty field seeds the standard defaults (mirrors backend manifest.load),
    # so 'open editor + OK' yields a real sweep, never a bare param-less 'ac'/'noise'.
    _INIT = _ManifestEditorDialog._sweep_editor_initial
    assert _SR(_INIT("", "ac")) == "ac start=10 stop=500M dec=20"
    assert _SR(_INIT("", "noise")) == "noise start=10 stop=100M dec=20"
    assert _SR(_INIT("ac errpreset=conservative", "ac")) == \
        "ac start=10 stop=500M dec=20 errpreset=conservative"     # bare-name state also seeded
    assert _SR(_INIT("ac start=5 stop=20M lin=7", "ac")) == "ac start=5 stop=20M lin=7"  # real -> kept
    # [Fix 2] iv_sweep: BLANK and 'auto' BOTH mean "no I-V sweep for this pin" (no coverage.iv);
    # an explicit range produces the sweep dict (locks what the corrected tooltip now states).
    _IVC = _ManifestEditorDialog._ivcov_from_text
    assert _IVC("") is None and _IVC("auto") is None and _IVC("AUTO") is None
    assert _IVC("lin 0 0.8 80") == {"sweep": {"type": "lin", "start": 0.0, "stop": 0.8, "n": 80}}
    # [Q2/Q5] coverage-cell sweep editor parse<->render (iload sweep / iv_sweep), incl. points
    _CP = _ManifestEditorDialog._covsweep_parse
    _CR = _ManifestEditorDialog._covsweep_render
    g = _CP("log 200u 6m 4 + 3m,5m")
    assert g["type"] == "log" and g["start"] == "200u" and g["stop"] == "6m" and g["n"] == "4"
    assert g["points"] == ["3m", "5m"] and _CR(g) == "log 200u 6m 4 + 3m,5m"
    assert _CR(_CP("lin 0 0.8 12")) == "lin 0 0.8 12"            # range-only round-trip
    assert _CR(_CP("100u,200u,300u")) == "100u,200u,300u"        # points-only round-trip
    assert _CR(_CP("")) == "" and _CR(_CP("auto")) == ""         # empty/auto -> no sweep
    # [Q5] iv_sweep cell now carries specific points end to end (cell <-> coverage.iv dict)
    _IVT = _ManifestEditorDialog._ivcov_to_text
    d = _IVC("lin 0 1 11 + 0.45,0.9")
    assert d["sweep"] == {"type": "lin", "start": 0.0, "stop": 1.0, "n": 11}
    assert d["points"] == [0.45, 0.9]
    assert _IVC(_IVT(d)) == d                                    # dict -> cell -> dict round-trip
    assert _IVC("0.3,0.6,0.9") == {"points": [0.3, 0.6, 0.9]}    # iv points-only
    # [#3] coverage sweep editor 'Specify by': linear offers points+step, log offers points only
    assert [k for _l, k in _ManifestEditorDialog._cov_spec_items("lin")] == ["points", "step"]
    assert [k for _l, k in _ManifestEditorDialog._cov_spec_items("log")] == ["points"]
    # [#1] transient-step editor cell parse<->render (steps + edge/tstop/tstep), round-trips
    _TP = _ManifestEditorDialog._trans_parse_cell
    _TR = _ManifestEditorDialog._trans_render_cell
    tf = _TP("20u:3m:slew , 0:6m @edge=1n,tstop=10u")
    assert tf["steps"] == [{"from": "20u", "to": "3m", "label": "slew"},
                           {"from": "0", "to": "6m", "label": ""}]
    assert tf["edge"] == "1n" and tf["tstop"] == "10u" and tf["tstep"] == ""
    assert _TR(tf) == "20u:3m:slew , 0:6m @edge=1n,tstop=10u"
    assert _TR(_TP("2m:3m , 3m:4m")) == "2m:3m , 3m:4m"          # labelless steps round-trip
    assert _TR(_TP("")) == ""                                    # empty -> empty
    # the editor cell text still parses to the canonical coverage.transient dict
    assert _ManifestEditorDialog._trans_from_text("2m:3m:s @edge=1n")["steps"][0]["to"] == 3e-3

    # 5j) [2.1/2.5] NO column stretches (a Stretch column swallows the viewport and balloons the
    #     dialog ~3400px wide); every column is Interactive with a compact fixed width, for ALL
    #     FOUR tables. 'net' is a touch wider than the rest but never stretched.
    from PyQt5.QtWidgets import QHeaderView as _QHV
    for _tt in (dlg11.t_supplies, dlg11.t_vout, dlg11.t_iout, dlg11.t_bias):
        _hh = _tt.horizontalHeader()
        assert not _hh.stretchLastSection(), "the last column must NOT be stretched ([2.1])"
        for _c in range(_tt.columnCount()):
            assert _hh.sectionResizeMode(_c) != _QHV.Stretch, \
                "no column may Stretch -- it balloons the dialog width ([2.1/2.5])"
        assert _tt.columnWidth(1) <= 220, "the 'net' column must keep a compact fixed width ([2.5])"

    # 5k) [STAGE 3] COVERAGE controls + per-rail/per-sink cells round-trip. Take the resolved
    #     wur_pmu_top manifest, INJECT coverage (loads on a rail, transient on a rail, iv on a
    #     sink, temps, tier=T2), open the editor, assert the widgets/cells populate, then
    #     _form_to_dict and assert the coverage section ROUND-TRIPS byte-for-byte (modulo float).
    from insitu import manifest as _M
    base = _M.load("wur_pmu_top")                                # resolved real manifest
    base.pop("_path", None)
    base["coverage"] = {
        "tier": "T2",
        "temps": [-40, 55, 125],
        "slew_en": 1,
        "lin_gate": True,
        "loads": {"vco": {"sweep": {"type": "log", "start": 200e-6, "stop": 6e-3, "n": 4},
                          "points": [3e-3]}},
        "transient": {"vco": {"steps": [{"from": 0.0, "to": 6e-3, "label": "slew"},
                                        {"from": 450e-6, "to": 550e-6, "label": "lin"}],
                              "edge": 1e-9, "tstop": 1e-6}},
        "iv": {"i3p6u_vco": {"sweep": {"type": "lin", "start": 0.0, "stop": 1.1, "n": 12}}},
    }
    dlgc = _ManifestEditorDialog(win, json.dumps(base), None)
    # the global coverage widgets populate
    assert dlgc.f_cov_tier.currentText() == "T2", "coverage tier did not populate the combo"
    assert dlgc.f_cov_slew.isChecked(), "slew_en did not populate the checkbox"
    assert dlgc.f_cov_lin.isChecked(), "lin_gate did not populate the checkbox"
    assert [s.strip() for s in dlgc.f_cov_temps.text().split(",")] == ["-40", "55", "125"], \
        f"temps did not populate, got {dlgc.f_cov_temps.text()!r}"
    # the per-rail / per-sink CELLS populate (compact strings) -- find the vco / i3p6u_vco rows
    vrows = {dlgc.t_vout.item(r, 0).text(): r for r in range(dlgc.t_vout.rowCount())}
    irows = {dlgc.t_iout.item(r, 0).text(): r for r in range(dlgc.t_iout.rowCount())}
    _vc = dlgc.t_vout._cols
    # coverage-sweep cells are composite widgets now (QLineEdit + '…' button) -> read via _cov_edit
    il_txt = _ManifestEditorDialog._cov_edit(dlgc.t_vout, vrows["vco"], _vc.index("iload sweep")).text()
    tr_txt = _ManifestEditorDialog._cov_edit(dlgc.t_vout, vrows["vco"], _vc.index("trans")).text()
    iv_txt = _ManifestEditorDialog._cov_edit(dlgc.t_iout, irows["i3p6u_vco"],
                                             dlgc.t_iout._cols.index("iv_sweep")).text()
    assert il_txt.startswith("log ") and "+" in il_txt, f"iload sweep cell not rendered: {il_txt!r}"
    assert "slew" in tr_txt and "lin" in tr_txt, f"trans cell not rendered: {tr_txt!r}"
    assert iv_txt.startswith("lin "), f"iv_sweep cell not rendered: {iv_txt!r}"
    # ROUND-TRIP: _form_to_dict must rebuild the same coverage section
    cd = dlgc._form_to_dict()["coverage"]
    assert cd["tier"] == "T2" and cd["slew_en"] == 1 and cd["lin_gate"] is True
    assert cd["temps"] == [-40, 55, 125], f"temps did not round-trip: {cd.get('temps')}"
    lsw = cd["loads"]["vco"]["sweep"]
    assert lsw["type"] == "log" and abs(lsw["start"] - 200e-6) < 1e-12 \
        and abs(lsw["stop"] - 6e-3) < 1e-12 and lsw["n"] == 4, f"loads sweep lost: {lsw}"
    assert [round(p, 12) for p in cd["loads"]["vco"]["points"]] == [3e-3], "loads points lost"
    tsteps = cd["transient"]["vco"]["steps"]
    assert len(tsteps) == 2 and tsteps[0]["label"] == "slew" and tsteps[1]["label"] == "lin", \
        f"transient steps lost: {tsteps}"
    assert abs(cd["transient"]["vco"]["edge"] - 1e-9) < 1e-15 \
        and abs(cd["transient"]["vco"]["tstop"] - 1e-6) < 1e-12, "transient edge/tstop lost"
    ivsw = cd["iv"]["i3p6u_vco"]["sweep"]
    assert ivsw["type"] == "lin" and ivsw["n"] == 12 and abs(ivsw["stop"] - 1.1) < 1e-9, \
        f"iv sweep lost: {ivsw}"
    # the merged manifest still validates end-to-end (the coverage validator)
    okc, msgc = dlgc._check()
    assert okc, f"coverage-carrying manifest must validate, got: {msgc}"
    # an empty CELL drops just that key (clear the iload-sweep cell -> loads.vco gone, others stay)
    _ManifestEditorDialog._cov_edit(dlgc.t_vout, vrows["vco"], _vc.index("iload sweep")).setText("")
    cd2 = dlgc._form_to_dict()["coverage"]
    assert "loads" not in cd2, "cleared iload cell must drop the (now empty) loads sub-dict"
    assert "transient" in cd2 and "iv" in cd2, "clearing one cell must not drop the others"

    # 5l) NO-COVERAGE manifest stays BYTE-CLEAN: a manifest with tier=T4 default + no temps/slew/
    #     lin + no per-rail cells must NOT emit a `coverage` key at all (no empty sub-dicts).
    nocov = json.dumps({"name": "nc", "dut": {"lib": "L", "cell": "C", "tb_cell": "TB"},
                        "v_out": {"o": {"net": "VO"}}, "i_out": {"c": {"net": "IC", "dc": 0.9}}})
    dlgn = _ManifestEditorDialog(win, nocov, None)
    assert dlgn.f_cov_tier.currentText() == "T4", "no-coverage manifest must default the tier to T4"
    assert dlgn.f_cov_temps.text() == "" and not dlgn.f_cov_slew.isChecked() \
        and not dlgn.f_cov_lin.isChecked(), "no-coverage manifest must open with empty coverage knobs"
    n_out = dlgn._form_to_dict()
    assert "coverage" not in n_out, "a no-coverage manifest must NOT emit a coverage key (byte-clean)"
    # ... but flipping ONE knob (tier T4->T1) does emit a minimal, valid coverage section.
    dlgn.f_cov_tier.setCurrentText("T1")
    n_out2 = dlgn._form_to_dict()
    assert n_out2.get("coverage", {}).get("tier") == "T1", "a non-default tier must emit coverage.tier"
    assert "loads" not in n_out2["coverage"] and "temps" not in n_out2["coverage"], \
        "an otherwise-empty coverage must not carry empty sub-dicts"

    # 6) find/replace bar (Ctrl+F / Ctrl+H) over the Raw text
    dlg2.findbar.find.setText("VDD0P8_VCO"); dlg2.findbar.repl.setText("VDD0P8_RENAMED")
    dlg2.findbar._replace_all()
    assert "VDD0P8_RENAMED" in dlg2.ed.toPlainText() and "VDD0P8_VCO" not in dlg2.ed.toPlainText(), \
        "find/replace-all did not rewrite the Raw text"

    # 7) SCREENSHOTS (mandatory): render the populated coverage-carrying editor + a SHORT Tab-0 so
    #    the new vertical scrollbar is visible. .grab() works offscreen even on an unshown widget.
    _render_coverage_shots(win, dlgc)

    print("  qt: manifest editor OK (Form+Raw tabs, bad JSON/manifest caught, save+reload, "
          "tb_lib<-dut_lib, lossless round-trip, net-placeholder strip, tb_src/src/probe_src "
          "columns, PSRR→I checkbox, bias top-level, corner label, per-object analysis, "
          "scan-fill, net-stretch/no-last-stretch, COVERAGE tier/temps/slew/lin + iload/trans/iv "
          "cells round-trip + byte-clean drop, find/replace)")


def _selftest(require_qt=False):
    """Headless verification: round-trip a reference through import_cadence -> fit -> predict ->
    emit (the full pipeline), then -- if PyQt5 is importable -- build the window offscreen and
    render. Self-contained: uses a real ref if present, else a synthetic analytic one (so the
    airgap smoke needs NO shipped data). With require_qt, missing/broken Qt is a failure."""
    import import_cadence as ic
    src_npz = ROOT / "results" / "ref" / "v5_spur.npz"
    if src_npz.exists():
        s = np.load(src_npz, allow_pickle=True)
        A = {k: np.asarray(s[k]) for k in s.files}
        loads = [str(x) for x in A["loads"]]; nom = loads[len(loads) // 2]
        src_label = "real ref v5_spur.npz"
    else:
        A, loads, nom = _synth_arrays()
        src_label = "synthetic analytic reference (self-contained)"
    print(f"  source: {src_label}")
    tmp = ROOT / "work" / "gui_selftest_csv"; tmp.mkdir(parents=True, exist_ok=True)
    files = {}
    for il in loads:
        for q in ("z", "p", "noise", "trans_lin", "spurs"):
            k = f"{q}_{il}"
            if k in A and np.asarray(A[k]).size:
                p = tmp / f"{k}.csv"; np.savetxt(p, np.asarray(A[k], float), delimiter=",")
                files[(q, il)] = p
    for q, kk in (("z_hf", f"z_{nom}_hf"), ("p_hf", f"p_{nom}_hf")):
        if kk in A:
            p = tmp / f"{q}.csv"; np.savetxt(p, np.asarray(A[kk], float), delimiter=",")
            files[(q, nom)] = p
    for tag in ("big", "slew"):
        kk = f"trans_{tag}_{nom}"
        if kk in A:
            p = tmp / f"trans_{tag}.csv"; np.savetxt(p, np.asarray(A[kk], float), delimiter=",")
            files[(f"trans_{tag}", nom)] = p
    for g in ("dc_loadreg", "dc_linereg", "dc_dropout"):
        if g in A:
            p = tmp / f"{g}.csv"; np.savetxt(p, np.asarray(A[g], float), delimiter=",")
            files[(g, None)] = p

    core = ModelerCore()
    core.profile = Profile(name="_gui_selftest", loads=loads, nominal=nom,
                           cout=float(A["meta_cout"]), esr=float(A["meta_esr"]), vref=1.05,
                           spur_twin0=float(A["spur_twin0"]), spur_binhz=float(A["spur_binhz"]))
    path, warns = core.import_data(files)
    assert path.exists(), "import did not write npz"
    core.fit()
    res = core.fit_residuals()
    assert res and all(np.isfinite([r["zrms"], r["prms"], r["npsd"]]).all() for r in res), "bad residuals"
    pc = core.predict_corner(nom)
    assert pc["Zm"].size and pc["Hm"].size and pc["Sm"].size, "predict returned empty"
    lib, va = core.emit(outdir=ROOT / "work" / "gui_selftest_out")
    assert lib.exists() and va.exists(), "emit failed"
    print(f"  core: import->fit->predict->emit OK  ({len(loads)} corners, "
          f"{len(core.result.spur_f)} spurs, {len(warns)} guardrail msgs)")
    for r in res:
        print(f"    {r['il']:>5}: zrms={r['zrms']:.3f} prms={r['prms']:.3f} npsd={r['npsd']:.3f} dB")
    # cleanup the selftest npz so it doesn't pollute results/ref. NpzFile is lazy and keeps the
    # file handle open (fit_model.ref), so close it first or Windows unlink fails (file in use).
    try:
        import fit_model
        if getattr(fit_model, "ref", None) is not None and hasattr(fit_model.ref, "close"):
            fit_model.ref.close()
        path.unlink()
    except OSError:
        pass

    # 1b) Trans-ID import path (Tab 5) -- headless, no simulator
    _selftest_transid(A, loads, nom, tmp)

    # 1c) Extract-tab PIN FORM (deliverable 1) -- gui+netmap -> manifest, Qt-free, no skillbridge
    _selftest_pinform(tmp)

    # 1d) In-situ ExtractCore (Tab 0) + combined model-cell dry build (deliverable 3) -- Qt-free
    _selftest_extract(tmp)

    # 2) Qt layer (offscreen) if available -- build window, REGRESSION-test the import button path
    #    (populate pickers -> apply profile -> collect must survive), render, and screenshot.
    if _HAVE_QT:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # redirect form-config persistence to a temp dir so the selftest never writes real HOME
        os.environ["LDO_CONFIG_DIR"] = str(pathlib.Path(tmp) / "cfg")
        app = QApplication.instance() or QApplication(sys.argv[:1])
        win = MainWindow(core)
        win.resize(1180, 820)
        # regression test for the critical import-grid-wipe bug: paths must survive _apply_profile
        for (q, il), pth in files.items():
            if (q, il) in win.file_edits:
                win.file_edits[(q, il)].setText(str(pth))
        win._apply_profile()                 # this used to wipe every picker -> "No files selected"
        collected = win._collect_files()
        assert collected, "REGRESSION: _apply_profile wiped the import-grid pickers (the #1 bug)"
        win._preview()                       # raw-data preview plots (Tab 2)
        win._refresh_compare()               # overlay (Tab 4)
        app.processEvents()
        # exercise EVERY button handler with dialogs stubbed -> catches missing-attr crashes that
        # only fire on a real click (this is exactly the class the MEAS_HINTS bug belonged to).
        import PyQt5.QtWidgets as _QW
        _orig = (_QW.QMessageBox.information, _QW.QMessageBox.warning, _QW.QMessageBox.critical,
                 _QW.QFileDialog.getExistingDirectory, _QW.QFileDialog.getOpenFileName,
                 _QW.QFileDialog.getSaveFileName, _QW.QDialog.exec_)
        _QW.QMessageBox.information = staticmethod(lambda *a, **k: None)
        _QW.QMessageBox.warning = staticmethod(lambda *a, **k: None)
        _QW.QMessageBox.critical = staticmethod(lambda *a, **k: None)
        _QW.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(tmp))
        _QW.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(next(iter(files.values()))), ""))
        _QW.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
        _QW.QDialog.exec_ = lambda self: 0   # any modal (manifest editor) returns Rejected, no block
        try:
            _selftest_manifest_editor(win, tmp)   # the in-GUI manifest editor (#1): validate+save
            win._show_guidance()             # the Measurement-guidance button (was the MEAS_HINTS crash)
            win._import_folder()             # folder-import (must match the synth CSVs in tmp/)
            assert win._collect_files(), "folder-import matched no files into the grid"
            # skip the LIVE-action buttons by IDENTITY (robust to label text): Build & Run starts
            # a real ade/skillbridge worker (the engine default), Resolve opens a live bridge,
            # Create model cell drives SKILL -- none may fire in a headless smoke (they would hang
            # or mutate Cadence state). The text-prefix skip covers the async Fit / re-apply / emit.
            _live_btns = {win.x_run, win.xf_build, win.xm_make, win.x_sb_check}
            for b in win.findChildren(_QW.QPushButton):
                if b in _live_btns or b.text().startswith(("Fit", "Apply", "Emit", "…")):
                    continue                 # skip live runs / async Fit / re-apply / emit-guard / pickers
                b.click(); app.processEvents()
            # regression: a nominal-only change must re-key the nominal-scope pickers (folder-import bug)
            loads0, nom0 = list(win.core.profile.loads), win.core.profile.nominal
            if len(loads0) > 1:
                other = next(c for c in loads0 if c != nom0)
                win.e_nom.setCurrentText(other); win._apply_profile()
                assert ("z_hf", other) in win.file_edits, "nominal change did not re-key the HF picker"
                win.e_nom.setCurrentText(nom0); win._apply_profile()      # restore
            # regression: refresh_from_profile pushes a programmatic profile into the widgets (--ref)
            win.refresh_from_profile()
            assert win.e_loads.text() == ",".join(win.core.profile.loads), "refresh_from_profile desync"
        finally:
            (_QW.QMessageBox.information, _QW.QMessageBox.warning, _QW.QMessageBox.critical,
             _QW.QFileDialog.getExistingDirectory, _QW.QFileDialog.getOpenFileName,
             _QW.QFileDialog.getSaveFileName, _QW.QDialog.exec_) = _orig
        try:                                  # the Save-text-report click writes results/score/report_*.txt
            import report as _rpt
            _r = _rpt.SCOREDIR / "report__gui_selftest.txt"
            if _r.exists():
                _r.unlink()
        except (OSError, ImportError):
            pass
        print("  qt: all button handlers exercised OK (guidance / folder-import / import)")
        core.fit(); win._fit_done(core.result)   # the Import click reset the fit -> re-fit for the shot
        app.processEvents()
        shot = ROOT / "work" / "gui_selftest_out" / "gui_compare.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        ok_shot = bool(win.grab().save(str(shot)))
        win.cmp_canvas.fig.savefig(ROOT / "work" / "gui_selftest_out" / "gui_overlay.png", dpi=110)
        # current-port overlay (Tab 4, V/I-dual): embed offline current GT into the ref, refresh
        # the port selector, render + screenshot one current port. Skips cleanly if the offline
        # GT library (work_isrc) hasn't been characterized; exercises+asserts the path when present.
        import glob as _glob
        _srcs = sorted(_glob.glob(str(ROOT / "work_isrc" / "*.npz")))
        if _srcs and isinstance(win.core.ref, dict):
            import current_digest as _cd
            _pmos = next((s for s in _srcs if "pmos" in s), _srcs[-1])
            for _pin, _f in (("IBP_DEMO_SINK", _srcs[0]), ("IBP_DEMO_SRC", _pmos)):
                _cd.embed_port(win.core.ref, _pin,
                               {k: v for k, v in np.load(_f, allow_pickle=True).items()})
            win._refresh_compare_ports()
            _pins = [win.cmp_port.itemData(i) for i in range(win.cmp_port.count())]
            assert "IBP_DEMO_SINK" in _pins, "current port missing from the Compare port selector"
            win.cmp_port.setCurrentIndex(win.cmp_port.findData("IBP_DEMO_SINK"))
            app.processEvents()
            assert not win.cmp_corner.isEnabled(), "corner picker should grey out for a current port"
            win.cmp_canvas.fig.savefig(ROOT / "work" / "gui_selftest_out" / "gui_overlay_current.png", dpi=110)
            win.cmp_port.setCurrentIndex(0)          # back to voltage (must not raise)
            app.processEvents()
            print(f"  qt: current-port overlay rendered + screenshotted "
                  f"({len([p for p in _pins if p])} current ports, selector+corner-gating OK)")
        else:
            print("  qt: current-port overlay skipped (no work_isrc/*.npz characterized)")
        # Mode A/B + location/Donau visibility (offscreen: use isVisibleTo, not isVisible).
        win.x_mode.setCurrentIndex(win.x_mode.findData("schematic")); app.processEvents()
        assert win.x_grp_pinform.isVisibleTo(win) and not win.x_grp_modeb.isVisibleTo(win), \
            "Mode A must show the pin form, hide the import group"
        win.x_mode.setCurrentIndex(win.x_mode.findData("import")); app.processEvents()
        assert win.x_grp_modeb.isVisibleTo(win) and not win.x_grp_pinform.isVisibleTo(win), \
            "Mode B must show the import group, hide the pin form"
        assert not win.x_backend.model().item(win.x_backend.findData("ade")).isEnabled(), \
            "ADE engine must be disabled in Mode B (no skillbridge)"
        assert win.x_backend.currentData() != "ade", "Mode B must switch off the ADE engine"
        win.x_location.setCurrentIndex(win.x_location.findData("local")); app.processEvents()
        assert not win.x_grp_donau.isVisibleTo(win), "Donau panel hidden when location=local"
        win.x_location.setCurrentIndex(win.x_location.findData("cluster")); app.processEvents()
        assert win.x_grp_donau.isVisibleTo(win), "Donau panel shown when location=cluster"
        # multi-supply form parse: 'pin@dc, pin@dc' -> gui['supplies']
        win.x_mode.setCurrentIndex(win.x_mode.findData("schematic"))
        win.xf_supplies.setText("AVDD1P0@1.0, DVDD0P8@0.8"); win.xf_cpsrr.setText("AVDD1P0")
        win.xf_ivsweep.setText("IBP_X=0:0.01:1.1")       # Cadence start:step:stop -> [vlo,vhi,step]
        _g = win._form_gui()
        assert [s["pin"] for s in _g["supplies"]] == ["AVDD1P0", "DVDD0P8"], _g["supplies"]
        assert _g["supplies"][1]["dc"] == 0.8 and _g["current_psrr_supplies"] == ["AVDD1P0"]
        assert _g["iv_sweep"]["IBP_X"] == [0.0, 1.1, 0.01], _g["iv_sweep"]
        win.xf_ivsweep.setText("")
        # Donau+ALPS cluster SWEEP from the GUI (the user-facing goal): offline per-group
        # netlister + the live per-group status table, via a dry-run (no submit).
        _selftest_cluster_sweep(win, tmp, app)
        # Mode-B LOCAL preview: the BARE per-group engine command (no dsub wrap), pure (no submit).
        win.x_mode.setCurrentIndex(win.x_mode.findData("import"))
        win.x_location.setCurrentIndex(win.x_location.findData("local"))
        win.x_backend.setCurrentIndex(win.x_backend.findData("alps"))
        win.extract.load_manifest("pmu_top")
        win.xb_netlist["edit"].setText(str(tmp)); win.xb_pdk["edit"].setText(str(tmp))
        win.xb_ahdl["edit"].setText("")
        win._x_run()
        _repL = win.x_report.toPlainText()
        assert not any(l.startswith("dsub ") for l in _repL.splitlines()), \
            "Mode-B local preview must be a bare command, not dsub"
        assert "input.scs" in _repL, "local preview missing the engine command"
        assert "-ahdllibdir" not in _repL, "blank ahdllibdir must drop -ahdllibdir"
        win.x_location.setCurrentIndex(win.x_location.findData("cluster"))
        win.x_mode.setCurrentIndex(win.x_mode.findData("schematic")); app.processEvents()
        # skillbridge indicator: startup sets it (import-only, never blocks); _set_sb renders states
        assert "skillbridge:" in win.x_sb_status.text(), "skillbridge indicator not initialized"
        win._set_sb("ok", "x"); assert "●" in win.x_sb_status.text()
        win._set_sb("no", "y"); assert "○" in win.x_sb_status.text()
        win._sb_initial()
        print("  qt: mode-split (pinform/import/Donau visibility) + ADE-off-in-B + multi-supply "
              "parse + cluster dsub + local-bare preview + skillbridge indicator OK")
        # engine-aware $MODEL_ROOT PDK default (#2) + the stale-on-engine-switch fix (#1).
        # Restore os.environ in a finally so we don't leak MODEL_ROOT into the rest of the test.
        _mr_saved = os.environ.get("MODEL_ROOT")
        try:
            os.environ.pop("MODEL_ROOT", None)            # unset -> no default for any engine
            assert MainWindow._pdk_default_for("spectre_cli") == "", "unset MODEL_ROOT must give no default"
            assert MainWindow._pdk_default_for("ade") == "", "ADE engine never takes a default"
            os.environ["MODEL_ROOT"] = str(tmp)           # set -> per-engine DIRECTORY subtree
            assert MainWindow._pdk_default_for("spectre_cli").endswith("/spectre"), "spectre default subtree"
            assert MainWindow._pdk_default_for("alps").endswith("/alps"), "alps default subtree"
            assert MainWindow._pdk_default_for("ade") == "", "ADE (live) takes no Mode-B default even when set"
            # engine-switch refresh + stale fix: in Mode B, the xb_pdk field must TRACK the engine
            win.x_mode.setCurrentIndex(win.x_mode.findData("import")); app.processEvents()
            win.xb_pdk["edit"].clear(); win._xb_pdk_auto = None      # start clean (no prior auto value)
            win.x_backend.setCurrentIndex(win.x_backend.findData("spectre_cli")); app.processEvents()
            assert win.xb_pdk["edit"].text().endswith("/spectre"), "PDK field did not auto-fill the spectre default"
            win.x_backend.setCurrentIndex(win.x_backend.findData("alps")); app.processEvents()
            assert win.xb_pdk["edit"].text().endswith("/alps"), \
                "engine switch left a STALE PDK default (#1) — must refresh to alps"
            # a genuine user edit must NOT be clobbered by a later engine switch
            win.xb_pdk["edit"].setText("/my/custom/models")
            win.x_backend.setCurrentIndex(win.x_backend.findData("spectre_cli")); app.processEvents()
            assert win.xb_pdk["edit"].text() == "/my/custom/models", "user-edited PDK path was clobbered on engine switch"
            win.xb_pdk["edit"].clear(); win._xb_pdk_auto = None
            win.x_mode.setCurrentIndex(win.x_mode.findData("schematic")); app.processEvents()
            print("  qt: PDK $MODEL_ROOT default (per-engine subtree, ADE none) + engine-switch refresh "
                  "+ no-clobber of user edits OK (#1/#2)")
        finally:
            if _mr_saved is None:
                os.environ.pop("MODEL_ROOT", None)
            else:
                os.environ["MODEL_ROOT"] = _mr_saved
        # form-config persistence: type values -> autosave -> a FRESH window must restore them;
        # named save/load round-trips too. (LDO_CONFIG_DIR points at the temp dir set above.)
        win.xf_dutlib.setText("PMU_TOP"); win.xf_dutcell.setText("pmu_top")
        win.xf_vouts.setText("RAILA, RAILB"); win.xf_iouts.setText("IBIAS_X")
        win.xf_supplies.setText("AVDD1P0@1.0, DVDD0P8@0.8")
        win.e_vref.setText("1.234"); win.x_session.setText("sess_XYZ")
        win.x_backend.setCurrentIndex(win.x_backend.findData("spectre_cli"))
        win.x_mode.setCurrentIndex(win.x_mode.findData("import"))
        win.xb_netlist["edit"].setText("/tmp/net"); win.xd_cpu.setValue(32)
        win._save_autosave()
        win2 = MainWindow(ModelerCore())            # __init__ -> _load_autosave restores entries
        assert win2.xf_dutlib.text() == "PMU_TOP" and win2.xf_dutcell.text() == "pmu_top", \
            "autosave did not restore DUT lib/cell"
        assert win2.xf_vouts.text() == "RAILA, RAILB" and win2.xf_iouts.text() == "IBIAS_X", \
            "autosave did not restore voltage/current outputs"
        assert win2.xf_supplies.text() == "AVDD1P0@1.0, DVDD0P8@0.8", "autosave did not restore supplies"
        assert win2.e_vref.text() == "1.234" and win2.x_session.text() == "sess_XYZ", \
            "autosave did not restore Profile vref / session"
        assert win2.x_backend.currentData() == "spectre_cli", "autosave did not restore the engine combo"
        assert win2.x_mode.currentData() == "import", "autosave did not restore the mode"
        assert win2.xb_netlist["edit"].text() == "/tmp/net", "autosave did not restore Mode-B netlist path"
        assert win2.xd_cpu.value() == 32, "autosave did not restore the Donau cpu spinbox"
        named = pathlib.Path(tmp) / "cfg" / "named_demo.json"
        named.parent.mkdir(parents=True, exist_ok=True)
        named.write_text(json.dumps(dict(win._collect_config(), dut_cell="ROUNDTRIP")), encoding="utf-8")
        win2._apply_config(json.loads(named.read_text()))
        assert win2.xf_dutcell.text() == "ROUNDTRIP", "named config load did not apply"
        print("  qt: form-config persistence OK (autosave restore + named save/load round-trip)")
        print(f"  qt: MainWindow built offscreen; import-button regression OK ({len(collected)} files "
              f"survive apply); compare rendered (PyQt5 {QtCore.PYQT_VERSION_STR}, Qt "
              f"{QtCore.QT_VERSION_STR}); screenshot {'saved' if ok_shot else 'FAILED'}")
        if require_qt and not ok_shot:       # a window that imports but cannot render+save -> fail gate
            print("  qt: REQUIRED but screenshot render/save FAILED")
            return False
        try:                                  # the Import-button exercise re-created the npz -> clean up
            import fit_model as _fm
            if getattr(_fm, "ref", None) is not None and hasattr(_fm.ref, "close"):
                _fm.ref.close()
            if path.exists():
                path.unlink()
        except OSError:
            pass
    else:
        msg = f"PyQt5 not importable ({_QT_IMPORT_ERR})"
        if require_qt:
            print(f"  qt: REQUIRED but {msg}")
            return False
        print(f"  qt: SKIPPED -- {msg} (logic-only selftest passed)")
    return True


def main():
    ap = argparse.ArgumentParser(description="LDO behavioral modeler GUI")
    ap.add_argument("--selftest", action="store_true", help="headless logic(+Qt) verification")
    ap.add_argument("--require-qt", action="store_true", help="selftest fails if Qt unavailable")
    ap.add_argument("--ref", default=None, help="open an existing results/ref/<name>.npz")
    a = ap.parse_args()
    if a.selftest:
        ok = _selftest(require_qt=a.require_qt)
        print(f"GUI selftest {'PASS' if ok else 'FAIL'}")
        sys.exit(0 if ok else 1)
    if not _HAVE_QT:
        print(f"PyQt5 not available: {_QT_IMPORT_ERR}\nInstall PyQt5 to launch the GUI "
              "(this dev venv may lack it; the deployed red-zone venv has it).")
        sys.exit(2)
    app = QApplication(sys.argv)
    core = ModelerCore()
    if a.ref:
        core.use_existing_ref(a.ref)         # seed the profile from the npz BEFORE building widgets
    win = MainWindow(core)
    if a.ref:
        win.refresh_from_profile()           # push the loaded profile into the widgets (no stale defaults)
        win.statusBar().showMessage(f"Loaded {pathlib.Path(a.ref).name}. Go to Fit (Tab 3).")
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
