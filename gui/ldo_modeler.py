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
import argparse
import pathlib
from dataclasses import dataclass, field

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
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

    def load_manifest(self, name_or_path):
        """Load + validate a pin-role manifest. Returns the human summary."""
        from insitu import manifest as M
        self.manifest = M.load(name_or_path)
        self.manifest_path = self.manifest.get("_path")
        return M.summary(self.manifest)

    def plan(self):
        """The augment plan (session-free preview of what 'Build & Run' will do)."""
        from insitu import augment
        return augment.build_plan(self.manifest)

    def run(self, backend="spectre_cli", session="fnxSession0", regenerate=False, tol=1e-6):
        """augment(ade) -> run -> PSF -> multi-port npz -> gate vs gold -> multi-port fit +
        per-output single-port refs. Returns dict(npz_path, gate, report, ports). Pure
        orchestration over the insitu package + fit_multiport."""
        if self.manifest is None:
            raise RuntimeError("load a manifest first")
        from insitu import cli
        # the ade run-drive builds Test_PMU_extract itself (run_ade build_first=True) -- no
        # double-build here.
        path, npz, _r = cli.produce_npz(self.manifest, backend, session, regenerate)
        self.npz_path = path
        self.gate = cli.gate_vs_gold(npz, tol=tol)
        import fit_multiport as FMP
        self.result = FMP.fit_multiport(path, self.manifest)
        self.report = FMP.report(self.result)
        self.port_refs = FMP.export_single_port_refs(path, self.manifest)
        return dict(npz_path=path, gate=self.gate, report=self.report, ports=self.port_refs)

    def port_list(self):
        return list(self.port_refs)


# =============================================================================== Qt UI
try:
    from PyQt5 import QtCore, QtWidgets
    from PyQt5.QtWidgets import (QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
                                 QHBoxLayout, QFormLayout, QGridLayout, QLabel, QLineEdit,
                                 QPushButton, QComboBox, QCheckBox, QFileDialog, QTextEdit,
                                 QTableWidget, QTableWidgetItem, QGroupBox, QMessageBox)
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
            self.fig = Figure(figsize=figsize, tight_layout=True)
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
        The ade backend can take a while (a Maestro run); spectre_cli is fast."""
        done = QtCore.pyqtSignal(object)
        failed = QtCore.pyqtSignal(str)

        def __init__(self, extract, backend, session, regenerate):
            super().__init__()
            self.extract, self.backend = extract, backend
            self.session, self.regenerate = session, regenerate

        def run(self):
            try:
                out = self.extract.run(backend=self.backend, session=self.session,
                                       regenerate=self.regenerate)
                self.done.emit(out)
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

        # --- Tab 0: Extract (in-situ, Mechanism A) -------------------------------
        def _tab_extract(self):
            w = QWidget(); outer = QVBoxLayout(w)
            help_ = QLabel(
                "<b>In-situ extraction (Mechanism A).</b> Point at a <b>pin-role manifest</b> "
                "(the designer tags each DUT pin: supply / v_out / i_out / leave-alone). "
                "<b>Build &amp; Run</b> copies the designer's testbench → <i>_extract</i>, appends "
                "AC stimuli/probes at the tagged nets, runs the analyses, and assembles the "
                "multi-port npz — which then feeds <b>2 Import → 3 Fit → 4 Compare</b> per output.<br>"
                "Backend <b>spectre_cli</b> runs offline (dev fixture, reproduces the trusted gold); "
                "<b>ade</b> drives the real Maestro run (rides Job Setup → cluster at the company).")
            help_.setWordWrap(True)
            help_.setStyleSheet("background:#eef5ff; padding:9px; border:1px solid #cdddee;")
            outer.addWidget(help_)

            form = QFormLayout(); outer.addLayout(form)
            self.x_manifest = QLineEdit("pmu_top")
            self.x_manifest.setToolTip("Manifest name (resolved in cadence/insitu/manifests/) "
                                       "or a path to a manifest JSON.")
            row = QHBoxLayout(); row.addWidget(self.x_manifest)
            b_browse = QPushButton("Browse…"); b_browse.clicked.connect(self._x_browse)
            b_load = QPushButton("Load"); b_load.clicked.connect(self._x_load)
            row.addWidget(b_browse); row.addWidget(b_load)
            rw = QWidget(); rw.setLayout(row); form.addRow("Manifest *", rw)
            self.x_backend = QComboBox(); self.x_backend.addItems(["spectre_cli", "ade"])
            self.x_backend.setToolTip("spectre_cli: offline dev fixture. ade: live Maestro run "
                                      "(needs the skillbridge session + analyses configured).")
            form.addRow("Backend", self.x_backend)
            self.x_session = QLineEdit("fnxSession0")
            self.x_session.setToolTip("ADE-XL session name (ade backend only).")
            form.addRow("Session", self.x_session)

            self.x_summary = QTextEdit(); self.x_summary.setReadOnly(True)
            self.x_summary.setMaximumHeight(120)
            self.x_summary.setStyleSheet("font-family:monospace; font-size:11px;")
            outer.addWidget(self.x_summary)

            brow = QHBoxLayout()
            self.x_run = QPushButton("Build && Run"); self.x_run.clicked.connect(self._x_run)
            self.x_run.setEnabled(False)
            brow.addWidget(self.x_run)
            self.x_gate = QLabel("—"); brow.addWidget(self.x_gate, 1)
            outer.addLayout(brow)

            self.x_report = QTextEdit(); self.x_report.setReadOnly(True)
            self.x_report.setStyleSheet("font-family:monospace; font-size:11px;")
            outer.addWidget(self.x_report, 1)

            srow = QHBoxLayout()
            srow.addWidget(QLabel("Send output port →"))
            self.x_port = QComboBox(); srow.addWidget(self.x_port)
            b_send = QPushButton("Load into Import → Fit"); b_send.clicked.connect(self._x_send)
            srow.addWidget(b_send); srow.addStretch(1)
            outer.addLayout(srow)
            return w

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
                self.x_run.setEnabled(True)
                self.statusBar().showMessage("Manifest loaded — 'Build & Run' to extract.")
            except Exception as e:
                QMessageBox.critical(self, "Manifest", f"{type(e).__name__}: {e}")

        def _x_run(self):
            self.x_run.setEnabled(False); self.x_gate.setText("running…")
            self.statusBar().showMessage("Extracting (augment → run → PSF → npz → fit)…")
            self._xw = _ExtractWorker(self.extract, self.x_backend.currentText(),
                                      self.x_session.text().strip(),
                                      regenerate=False)
            self._xw.done.connect(self._x_done)
            self._xw.failed.connect(self._x_failed)
            self._xw.start()

        def _x_done(self, out):
            passed, worst, detail = out["gate"]
            tag = {True: "PASS", False: "FAIL", None: "SKIP"}[passed]
            colour = {"PASS": "#157f3b", "FAIL": "#b00020", "SKIP": "#777"}[tag]
            self.x_gate.setText(f"<b>gate vs gold: <span style='color:{colour}'>{tag}</span></b> "
                                f"({detail}) — npz {pathlib.Path(out['npz_path']).name}")
            self.x_report.setPlainText(out["report"])
            self.x_port.clear(); self.x_port.addItems(self.extract.port_list())
            self.x_run.setEnabled(True)
            self.statusBar().showMessage(f"Extraction done — gate {tag}. "
                                         "Pick an output port and 'Load into Import → Fit'.")

        def _x_failed(self, msg):
            self.x_gate.setText("FAILED")
            self.x_run.setEnabled(True)
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
            self.grid_host = QWidget(); lay.addWidget(self.grid_host)
            self._rebuild_import_grid()
            row = QHBoxLayout()
            b_imp = QPushButton("Import + preview   →   go to Fit")
            b_imp.clicked.connect(self._do_import)
            row.addWidget(b_imp); row.addStretch(1)
            lay.addLayout(row)
            self.warn_box = QTextEdit(); self.warn_box.setReadOnly(True)
            self.warn_box.setMaximumHeight(120); lay.addWidget(self.warn_box)
            self.imp_canvas = _Canvas(1, 3, figsize=(11, 2.8)); lay.addWidget(self.imp_canvas)
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
            row.addWidget(QLabel("corner:"))
            self.cmp_corner = QComboBox(); self.cmp_corner.addItems(self.core.profile.loads)
            self.cmp_corner.currentIndexChanged.connect(self._refresh_compare)
            row.addWidget(self.cmp_corner); row.addStretch(1)
            self.cmp_report_btn = QPushButton("Save text report…")
            self.cmp_report_btn.setToolTip("Write the plain-text model-vs-GT difference report "
                                           "(copy-pasteable diagnosis; no screenshot needed).")
            self.cmp_report_btn.clicked.connect(self._save_text_report)
            row.addWidget(self.cmp_report_btn)
            lay.addLayout(row)
            self.cmp_canvas = _Canvas(2, 3, figsize=(11, 6)); lay.addWidget(self.cmp_canvas)
            self.cmp_err = QLabel("Fit a model to see the overlay."); lay.addWidget(self.cmp_err)
            return w

        def _refresh_compare(self):
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


def _selftest_extract():
    """Headless smoke of the in-situ EXTRACTION front-half (ExtractCore, Qt-free): manifest
    -> spectre_cli run -> multi-port npz -> gate vs gold -> per-output single-port refs.
    The spectre_cli fixture (cadence/work_pmu PSF) is gitignored regenerable data that needs
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
    print(f"  extract: manifest->run->npz->fit OK  gate={'PASS' if passed else 'SKIP'} "
          f"({detail}); per-output refs={ports}")
    for p in list(xc.port_refs.values()) + [xc.npz_path]:   # clean gitignored regenerables
        try:
            pathlib.Path(p).unlink()
        except OSError:
            pass


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

    # 1c) In-situ ExtractCore (Tab 0, Mechanism A) -- Qt-free, offline spectre_cli fixture
    _selftest_extract()

    # 2) Qt layer (offscreen) if available -- build window, REGRESSION-test the import button path
    #    (populate pickers -> apply profile -> collect must survive), render, and screenshot.
    if _HAVE_QT:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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
                 _QW.QFileDialog.getSaveFileName)
        _QW.QMessageBox.information = staticmethod(lambda *a, **k: None)
        _QW.QMessageBox.warning = staticmethod(lambda *a, **k: None)
        _QW.QMessageBox.critical = staticmethod(lambda *a, **k: None)
        _QW.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(tmp))
        _QW.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(next(iter(files.values()))), ""))
        _QW.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
        try:
            win._show_guidance()             # the Measurement-guidance button (was the MEAS_HINTS crash)
            win._import_folder()             # folder-import (must match the synth CSVs in tmp/)
            assert win._collect_files(), "folder-import matched no files into the grid"
            for b in win.findChildren(_QW.QPushButton):
                if b.text().startswith(("Fit", "Apply", "Emit", "…")):
                    continue                 # skip async Fit / re-apply / emit-guard / per-cell pickers
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
             _QW.QFileDialog.getSaveFileName) = _orig
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
