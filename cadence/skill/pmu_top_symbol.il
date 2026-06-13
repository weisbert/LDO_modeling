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
;;   dbOpenCellViewByType  skdfref.pdf  p.898
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
