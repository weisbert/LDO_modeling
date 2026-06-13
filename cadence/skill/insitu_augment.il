;; ============================================================================
;; insitu_augment.il -- Mechanism A (P2): build the in-situ EXTRACTION testbench
;;
;; "Capture-and-augment, not reconstruct": we COPY the designer's working TB (which
;; already establishes the correct in-situ operating point) to <tb>_extract, then only
;; APPEND extraction stimuli/probes at the manifest-tagged DUT-boundary nets:
;;   * v_out  : a 1 A AC isource (mag = design var acm_v_out_<o>) injected at the net
;;              -> Zout (read V) and output-to-output coupling.
;;   * supply : the EXISTING supply source is made AC-able (its acm = acm_supply_<s>)
;;              -> PSRR / current-PSRR (we never add a series element).
;;   * i_out  : OUR OWN named probe vsource (dc bias + mag = acm_i_out_<c>) at the sink
;;              -> admittance (read its :p) and current-PSRR. The probe name is one we
;;              choose, so the read side never reverse-engineers DUT terminal numbering.
;; The designer's spine is never mutated (we edit the COPY). All acm_* default 0 so the
;; shared OP is untouched until exactly one is set to 1 per point (AC superposition).
;;
;; Generic primitives only -- the Python side (insitu/augment.py) calls them per the
;; pin-role manifest, so this file is DUT-agnostic.
;;
;; SKILL refs (looked up via the virtuoso-skill index, mandatory):
;;   dbOpenCellViewByType  skdfref.pdf p.900   dbCopyCellView   skdfref.pdf p.876
;;   schCreateInst         skcompref.pdf p.235  schCreateWire    skcompref.pdf p.254
;;   schCreateWireLabel    skcompref.pdf p.256  dbTransformPoint skdfref.pdf p.586
;;   dbCreateProp          skdfref.pdf p.553    dbReplaceProp    skdfref.pdf p.557
;;   dbFindProp            skdfref.pdf p.555    dbSave           skdfref.pdf p.921
;;
;;   load("cadence/skill/insitu_augment.il")
;; ============================================================================

;; --- copy the designer TB to the extraction copy (overwrite if present) -----
(defun insituCopyTB (srcLib srcCell dstCell @optional (dstLib srcLib) (view "schematic"))
  ;; Ref: dbOpenCellViewByType skdfref p.900 ; dbCopyCellView skdfref p.876
  (let (src dst)
    (setq src (dbOpenCellViewByType srcLib srcCell view view "r"))
    (unless src (error "insituCopyTB: cannot open %s/%s/%s" srcLib srcCell view))
    (setq dst (dbCopyCellView src dstLib dstCell view nil nil t))   ; overwrite=t
    (dbClose src)
    (if dst (printf "insituCopyTB: %s/%s -> %s/%s\n" srcLib srcCell dstLib dstCell)
        (error "insituCopyTB: dbCopyCellView failed"))
    (and dst t)))


;; --- world position of an instance terminal (geometry-driven connect-by-name) --
(defun insituTermPoint (inst termName)
  ;; master symbol pin center, transformed by the instance placement. Ref:
  ;; dbTransformPoint skdfref p.586. Returns (x y) or nil.
  (let (mt pin fig bb c)
    (setq mt (car (setof tm inst~>master~>terminals (equal tm~>name termName))))
    (unless mt (return nil))
    (setq pin (car mt~>pins))
    (setq fig (and pin pin~>fig))
    (unless fig (return nil))
    (setq bb fig~>bBox)
    (setq c (list (quotient (plus (caar bb) (caadr bb)) 2.0)
                  (quotient (plus (cadar bb) (cadadr bb)) 2.0)))
    (dbTransformPoint c inst~>transform)))


;; --- place a 2-terminal source and connect each terminal to a named net ------
(defun insituAddSource (cv libName cellName instName loc termNetPairs propPairs)
  ;; termNetPairs = (("PLUS" "gnd!") ("MINUS" "VDD0P8_PLL")) -> stub-wire + label each
  ;; (connect-by-name). propPairs = (("acm" "string" "acm_v_out_pll") ("dc" "string" "0.4"))
  ;; Ref: schCreateInst skcompref p.235 ; schCreateWire p.254 ; schCreateWireLabel p.256 ;
  ;;      dbCreateProp/dbReplaceProp/dbFindProp skdfref p.553/557/555
  (let (master inst p w lbl)
    (setq master (dbOpenCellViewByType libName cellName "symbol" "" "r"))
    (unless master (error "insituAddSource: no %s/%s/symbol" libName cellName))
    (setq inst (schCreateInst cv master instName loc "R0"))
    (unless inst (error "insituAddSource: schCreateInst %s failed" instName))
    (foreach pair termNetPairs
      (setq p (insituTermPoint inst (car pair)))
      (when p
        ;; a short stub from the pin, labelled with the net name (same name = same net)
        (setq w (schCreateWire cv "draw" "full"
                  (list p (list (car p) (difference (cadr p) 0.125)))
                  0.0625 0.0625 0.0))
        (setq lbl (list (car p) (difference (cadr p) 0.125)))
        (schCreateWireLabel cv (car w) lbl (cadr pair) "lowerLeft" "R0" "stick" 0.0625 nil)))
    (foreach pp propPairs
      (insituSetProp inst (car pp) (cadr pp) (caddr pp)))
    inst))


;; --- set (create or replace) a scalar property on a db object ----------------
(defun insituSetProp (obj name propType value)
  ;; Ref: dbFindProp skdfref p.555 ; dbReplaceProp p.557 ; dbCreateProp p.553
  (if (dbFindProp obj name)
      (dbReplaceProp obj name propType value)
      (dbCreateProp obj name propType value)))


;; --- find an instance by name in a cellview ----------------------------------
(defun insituFindInst (cv instName)
  (car (setof i cv~>instances (equal i~>name instName))))


;; --- make an existing supply source AC-able (set its acm to a design var) -----
(defun insituSetSupplyAcm (cv srcInstName acmVar)
  (let (inst)
    (setq inst (insituFindInst cv srcInstName))
    (unless inst (error "insituSetSupplyAcm: no instance %s in copy" srcInstName))
    (insituSetProp inst "acm" "string" acmVar)
    (printf "insituSetSupplyAcm: %s acm=%s\n" srcInstName acmVar)
    inst))


;; --- check (extract connectivity from the by-name labels) + save -------------
(defun insituSaveCV (cv)
  ;; schCheck extracts connectivity so the appended stub+label sources actually bind to
  ;; the DUT nets by name (instTerm~>net stays nil until a check runs). Ref: schCheck
  ;; skcompref p.221 ; dbSave skdfref p.921. Returns (errors warnings).
  (let (chk)
    (setq chk (schCheck cv))
    (when (and chk (greaterp (car chk) 0))
      (warn "insituSaveCV: schCheck reported %d error(s)" (car chk)))
    (dbSave cv)
    (dbClose cv)
    chk))
