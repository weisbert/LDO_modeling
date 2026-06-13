;; stub_cells.il -- create a leaf STAND-IN block as a Cadence cell: a Verilog-A
;; implementation view + an auto-generated symbol (with pin names). Used to build the
;; behavioral guts of sim_yusheng/PMU_top.
;;
;; SKILL refs:
;;   ddGetObj             skdfref.pdf   p.1760
;;   schPinListToSymbolGen  (samples/local/schConfig.il pinList->symbol generator;
;;                           pinList DPL format documented there)
;;
;;   load(".../stub_cells.il")
;;   pmuBuildStubCell("sim_yusheng" "vref_bias" "/abs/vref_bias.va"
;;        list(list("vin1p0" "inputOutput") ... list("i1u" "output")))

(defun pmuCopyFile (src dst "tt")
  (let ((in (infile src)) (out (outfile dst "w")) line)
    (cond
      ((or (null in) (null out)) (when in (close in)) (when out (close out)) nil)
      (t (while (gets line in) (fprintf out "%s" line)) (close in) (close out) t))))

;; Generate the OA artifacts the OSS/Maestro netlister needs for a veriloga
;; cellview (else ADE/Maestro fails with OSSHNL-381 / "no view-specific
;; simulation data"). Mirrors skill_tools/dreg_gen/dgenRun.il:dgen_compileVerilogA.
;;   amsUpdateTextviews -> netlist.oa (+ data.dm);  ahdlUpdateViewInfo -> sim data.
;;   gated by amsIsPresent (needs AMS Designer license).
;; Refs: amsskillref.pdf p.25 (amsUpdateTextviews) p.30 (vmsUpdateCellViews) p.18
;;       (amsIsPresent); skartistref.pdf p.600 (ahdlUpdateViewInfo)
(defun pmuCompileVA (libName cellName)
  (let (oaPath)
    (setq oaPath (strcat (ddGetObj libName)~>readPath "/" cellName "/veriloga/netlist.oa"))
    (cond
      ((null (amsIsPresent))
        (warn "pmuCompileVA: amsIsPresent=nil; %s/%s netlist.oa needs manual save\n" libName cellName)
        nil)
      (t
        (errset (amsUpdateTextviews libName ?cellName cellName ?viewName "veriloga" ?incremental nil) t)
        (unless (isFile oaPath)
          (errset (vmsUpdateCellViews ?lib libName ?cell cellName ?view "veriloga" ?viewt "text.veriloga") t))
        (errset (ahdlUpdateViewInfo libName ?cell cellName ?view "veriloga") t)
        (cond
          ((isFile oaPath) (printf "pmuCompileVA: %s/%s netlist.oa OK\n" libName cellName) t)
          (t (warn "pmuCompileVA: %s/%s netlist.oa NOT produced\n" libName cellName) nil))))))

(defun pmuBuildStubCell (libName cellName vaPath pinSpecs "tttg")
  (let (libObj libPath cellDir vaDir ports pl f)
    (setq libObj (ddGetObj libName))
    (unless libObj (error "pmuBuildStubCell: lib %s not found" libName))
    (setq libPath (or libObj~>writePath libObj~>readPath))
    (setq cellDir (strcat libPath "/" cellName))
    (unless (isDir cellDir) (createDir cellDir))
    (setq vaDir (strcat cellDir "/veriloga"))
    (unless (isDir vaDir) (createDir vaDir))
    (unless (pmuCopyFile vaPath (strcat vaDir "/veriloga.va"))
      (error "pmuBuildStubCell: cannot copy %s" vaPath))
    (setq f (outfile (strcat vaDir "/master.tag") "w"))
    (fprintf f "-- Master.tag File, Rev:1.0\nveriloga.va\n")
    (close f)
    (ddUpdateLibList)
    ;; build the DPL pin list and generate the symbol  -- schConfig.il pinList format
    (setq ports nil)
    (foreach ps pinSpecs
      (setq ports (cons (list nil 'name (car ps) 'direction (cadr ps)) ports)))
    (setq ports (reverse ports))
    (setq pl (list nil 'ports ports 'prop (list nil 'partName cellName)))
    (schPinListToSymbolGen libName cellName "symbol" pl)
    ;; compile the VA -> netlist.oa so Maestro/ADE can netlist it (OSSHNL-381 fix)
    (pmuCompileVA libName cellName)
    (printf "pmuBuildStubCell: %s/%s veriloga+symbol+netlist.oa built\n" libName cellName)
    t))
