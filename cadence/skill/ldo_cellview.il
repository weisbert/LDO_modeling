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
(defun ldoImportVA (libName cellName vaPath @optional tblPath "ttt")
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
