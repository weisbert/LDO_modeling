;; pmu_top_symbol.il -- build the STAND-IN PMU top CELL (schematic + symbol) in
;; library sim_yusheng, the conventional way: create a schematic, place the pins
;; with schCreatePin (basic ipin/opin/iopin masters), then auto-generate the
;; symbol from the schematic's pin list with schViewToView. The generated symbol
;; carries pin-name labels + [@instanceName]/[@partName] (a hand-built symbol via
;; schCreateSymbolPin does NOT auto-create the pin-name displays -- that was the
;; "no pin names shown" bug).
;;
;; Interface (mimics a real PMU top so it can host guts + drop into a Maestro TB):
;;   supplies (inputOutput): VDD1P8, VDD1P0, VSS
;;   modeled LDO outs (output): VDD0P8_PLL, VDD0P8_VCO   (share VREF/bias)
;;   bias-current sink outs (output): IBIAS_500N, IBIAS_1U
;;   dummy control ins (input): PLL_EN, VCO_EN, BIAS_EN,
;;                              PLL_CTRL<3:0>, VCO_CTRL<3:0>, TEST_CTRL<3:0>
;; The dummy control pins are normal input terminals; they get a basic/noConn
;; marker when this cell is INSTANCED in a schematic (noConn is an instance-level
;; marker, not a symbol attribute).
;;
;; SKILL refs (IC6.1.8 PI references, verified):
;;   dbOpenCellViewByType  skdfref.pdf  p.900
;;   schCreatePin          skcompref.pdf p.244
;;   schViewToView         skcompref.pdf p.423   (funcs from samples/local/schConfig.il:
;;                         schSchemToPinList / schPinListToSymbolGen)
;;   dbSave                skdfref.pdf  p.921
;;
;;   load("/abs/.../cadence/skill/pmu_top_symbol.il")
;;   pmuBuildTopCell("sim_yusheng" "PMU_top")

(defun pmuBuildTopCell (libName cellName "tt")
  (let (pins mi mo mio sch yi yo yb)
    (setq pins (list
        ;; controls (input)
        (list "PLL_EN"         "input")
        (list "VCO_EN"         "input")
        (list "BIAS_EN"        "input")
        (list "PLL_CTRL<3:0>"  "input")
        (list "VCO_CTRL<3:0>"  "input")
        (list "TEST_CTRL<3:0>" "input")
        ;; supplies (inputOutput)
        (list "VDD1P8" "inputOutput")
        (list "VDD1P0" "inputOutput")
        (list "VSS"    "inputOutput")
        ;; modeled outputs + bias-current sinks (output)
        (list "VDD0P8_PLL" "output")
        (list "VDD0P8_VCO" "output")
        (list "IBIAS_500N" "output")
        (list "IBIAS_1U"   "output")))
    (setq mi  (dbOpenCellViewByType "basic" "ipin"  "symbol" "" "r"))
    (setq mo  (dbOpenCellViewByType "basic" "opin"  "symbol" "" "r"))
    (setq mio (dbOpenCellViewByType "basic" "iopin" "symbol" "" "r"))
    (setq sch (dbOpenCellViewByType libName cellName "schematic" "schematic" "w"))
    ;; place pins in 3 columns by direction (schematic position is cosmetic; the
    ;; symbol generator places by direction)  -- Ref: skcompref.pdf p.244
    (setq yi 6.0) (setq yo 6.0) (setq yb 6.0)
    (foreach p pins
      (let ((nm (car p)) (dir (cadr p)))
        (cond
          ((equal dir "input")
             (schCreatePin sch mi  nm dir nil (list -2.0 yi) "R0") (setq yi (difference yi 0.5)))
          ((equal dir "output")
             (schCreatePin sch mo  nm dir nil (list  2.0 yo) "R0") (setq yo (difference yo 0.5)))
          (t
             (schCreatePin sch mio nm dir nil (list  0.0 yb) "R0") (setq yb (difference yb 0.5))))))
    (dbSave sch)
    ;; auto-generate the symbol from the schematic pin list  -- Ref: skcompref.pdf p.423
    (schViewToView libName cellName libName cellName "schematic" "symbol"
        "schSchemToPinList" "schPinListToSymbolGen")
    (printf "pmuBuildTopCell: %s/%s  schematic(%d terms) + symbol generated\n"
        libName cellName (length sch~>terminals))
    t))

;; -----------------------------------------------------------------------------
;; pmuBuildModelCell -- build the EMITTED behavioral-model cell's schematic-pin set
;; and auto-generate its symbol with INPUTS LEFT, OUTPUTS RIGHT, and the GROUND/VSS
;; pin pushed to the BOTTOM row (unlike pmuBuildTopCell, which leaves inputOutput
;; pins in the symbol generator's MIDDLE band). Used for Component D's combined PMU
;; model: input AVDD1P0 (left) / 6 outputs (right) / VSS (bottom).
;;
;;   pinspec = list of (name dir side), where dir is "input"/"output"/"inputOutput"
;;             and side is one of "left"/"right"/"bottom"/"top"/nil. side="bottom"
;;             (typically VSS) is generated as inputOutput then relocated to the
;;             bottom of the symbol body; side=nil follows the direction default
;;             (input->left, output->right).
;;
;;   pmuBuildModelCell("LDO_model_lab" "PMU_model"
;;       (list (list "AVDD1P0" "input" "left")
;;             (list "VDD0P8_DIG" "output" "right") ...
;;             (list "VSS" "inputOutput" "bottom")))
;;
;; SKILL refs (IC6.1.8 PI references, verified):
;;   schCreatePin   skcompref.pdf p.244   (d_cvId d_master t_termName t_direction
;;                                          g_offSheetP l_origin t_orientation ...)
;;   schViewToView  skcompref.pdf p.423
;;   dbMoveFig      skdfref.pdf  p.1318    (d_fig d_cellView [l_transform];
;;                                          d_cellView=nil => move within same cv)
(defun pmuBuildModelCell (libName cellName pinspec "ttl")
  (let (mi mo mio sch sym yi yo bottoms ybot ymin)
    (setq mi  (dbOpenCellViewByType "basic" "ipin"  "symbol" "" "r"))
    (setq mo  (dbOpenCellViewByType "basic" "opin"  "symbol" "" "r"))
    (setq mio (dbOpenCellViewByType "basic" "iopin" "symbol" "" "r"))
    (setq sch (dbOpenCellViewByType libName cellName "schematic" "schematic" "w"))
    ;; place schematic pins by direction (cosmetic in the schematic; the symbol
    ;; generator places by direction). Collect the names tagged side="bottom" so we
    ;; can relocate their generated symbol pins to the bottom afterward.
    ;; -- Ref: skcompref.pdf p.244
    (setq yi 6.0) (setq yo 6.0) (setq bottoms nil)
    (foreach p pinspec
      (let ((nm (nth 0 p)) (dir (nth 1 p)) (side (nth 2 p)) mstr xpos ypos)
        (cond
          ((equal dir "input")  (setq mstr mi)  (setq xpos -2.0)
             (setq ypos yi) (setq yi (difference yi 0.5)))
          ((equal dir "output") (setq mstr mo)  (setq xpos  2.0)
             (setq ypos yo) (setq yo (difference yo 0.5)))
          (t                    (setq mstr mio) (setq xpos  0.0)
             (setq ypos 0.0)))
        (schCreatePin sch mstr nm dir nil (list xpos ypos) "R0")
        (when (equal side "bottom") (setq bottoms (cons nm bottoms)))))
    (dbSave sch)
    ;; auto-generate the symbol from the schematic pin list  -- Ref: skcompref.pdf p.423
    (schViewToView libName cellName libName cellName "schematic" "symbol"
        "schSchemToPinList" "schPinListToSymbolGen")
    ;; relocate the GROUND/VSS pins to the BOTTOM of the generated symbol body. Open
    ;; the symbol, find the lowest pin-figure Y (the symbol body's bottom edge), then
    ;; move each bottom pin's figure (and its label) DOWN below that edge so it reads
    ;; as a bottom-side terminal.  -- Ref: skdfref.pdf p.1318
    (when bottoms
      (setq sym (dbOpenCellViewByType libName cellName "symbol" "symbol" "a"))
      ;; lowest existing pin Y across all symbol pins = symbol-body bottom reference
      (setq ymin 1e30)
      (foreach term sym~>terminals
        (foreach pin term~>pins
          (let ((bb pin~>fig~>bBox))
            (when (and bb (lessp (xCoord (lowerLeft bb)) 1e30))
              (when (lessp (yCoord (lowerLeft bb)) ymin)
                (setq ymin (yCoord (lowerLeft bb))))))))
      (setq ybot (difference ymin 1.0))   ;; one grid below the lowest pin
      (foreach term sym~>terminals
        (when (member term~>name bottoms)
          (foreach pin term~>pins
            (let ((fig pin~>fig))
              (when fig
                (let ((cy (yCoord (centerBox fig~>bBox))))
                  ;; move the pin fig (and its co-located label) straight down to ybot
                  (dbMoveFig fig nil (list (list 0.0 (difference ybot cy)) "R0"))))))))
      (dbSave sym))
    (printf "pmuBuildModelCell: %s/%s  schematic(%d terms) + symbol; VSS->bottom: %L\n"
        libName cellName (length sch~>terminals) bottoms)
    t))
