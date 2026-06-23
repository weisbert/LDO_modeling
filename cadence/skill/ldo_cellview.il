;; ldo_cellview.il -- import an emitted behavioral .va into a Cadence library as a
;; Verilog-A cellview, from inside Virtuoso (CIW). The Python/skillbridge twin is
;; cadence/skill_lib.py; this is the in-session equivalent. Same file-level recipe
;; the box's dreg_gen tool uses (write veriloga.va + master.tag, then ddUpdateLibList).
;;
;;   load("/abs/path/to/cadence/skill/ldo_cellview.il")
;;   ldoEnsureLib("LDO_model_lab" "/abs/path/cadence/cds/LDO_model_lab")
;;   ldoImportVA("LDO_model_lab" "ldo_model" "/abs/path/model/ldo_model.va"
;;               "/abs/path/model/ldo_model_dropout.tbl")   ; tbl optional ($table_model)

(defun ldoCopyFile (src dst "tt")
  (let ((in (infile src)) (out (outfile dst "w")) line)
    (cond
      ((or (null in) (null out)) (when in (close in)) (when out (close out)) nil)
      (t (while (gets line in) (fprintf out "%s" line)) (close in) (close out) t))))

;; Create the library if absent (no tech: behavioral/schematic only). Returns lib path.
(defun ldoEnsureLib (libName libPath "tt")
  (let ((obj (ddGetObj libName)))
    (cond
      (obj (or obj~>writePath obj~>readPath))
      (t (ddCreateLib libName libPath)
         (ddUpdateLibList)
         libPath))))

;; Create/refresh <lib>/<cell>/veriloga from vaPath. Returns the cellview dir.
;; NB: NO type template here -- there are 4 formals (3 required + 1 @optional tblPath), and a
;; 3-char template ("ttt") mis-validates the 4th arg ("argument #4 should be a string"). The body
;; already guards a nil tblPath, so we let SKILL skip type-checking (the Python caller always
;; passes strings). [ldoCompileVA below keeps its "ttt" -- it has exactly 3 formals.]
(defun ldoImportVA (libName cellName vaPath @optional tblPath)
  (let ((libObj (ddGetObj libName)) libPath cellDir vaDir)
    (unless libObj (error "ldoImportVA: lib %s not found -- run ldoEnsureLib first" libName))
    (setq libPath (or libObj~>writePath libObj~>readPath))
    (setq cellDir (strcat libPath "/" cellName))
    (unless (isDir cellDir) (createDir cellDir))
    (setq vaDir (strcat cellDir "/veriloga"))
    (unless (isDir vaDir) (createDir vaDir))
    (unless (ldoCopyFile vaPath (strcat vaDir "/veriloga.va"))
      (error "ldoImportVA: cannot copy %s" vaPath))
    (when tblPath
      (ldoCopyFile tblPath (strcat vaDir "/" (car (last (parseString tblPath "/"))))))
    (let ((p (outfile (strcat vaDir "/master.tag") "w")))
      (fprintf p "-- Master.tag File, Rev:1.0\nveriloga.va\n") (close p))
    (ddUpdateLibList)
    (printf "ldoImportVA: %s/%s/veriloga ready\n" libName cellName)
    vaDir))

;; Trigger the Verilog-A COMPILE/parse of an imported veriloga cellview: parse the .va
;; module, surface any compile errors, and update the cellview's CDF/parameter info.
;; Call AFTER ldoImportVA so the model cell carries its parameters and a clean compile
;; is confirmed without a full netlist/elaborate run. Returns t.
;;
;; SKILL ref (IC6.1.8 PI reference, verified):
;;   ahdlUpdateViewInfo  skartistref.pdf p.600
;;     (t_lib [?cell lt_cell] [?view lt_view] [?tool lt_tool]) -- parses the Verilog-A /
;;     SpectreHDL modules defining the cellviews, issues compile error messages, updates
;;     the cellview CDF. Omitting ?cell/?view updates every veriloga/ahdl cellview.
(defun ldoCompileVA (libName cellName @optional (viewName "veriloga") "ttt")
  (ahdlUpdateViewInfo libName ?cell cellName ?view viewName)   ; Ref: skartistref.pdf p.600
  (printf "ldoCompileVA: %s/%s/%s parsed (CDF updated; check CIW for compile errors)\n"
      libName cellName viewName)
  t)
