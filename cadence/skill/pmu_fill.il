;; pmu_fill.il -- fill sim_yusheng/PMU_top/schematic with the behavioral guts:
;;   X_REF = vref_bias, X_PLL = ldo_core_pll, X_VCO = ldo_core_vco
;; Connectivity is established BY NET NAME: each instance/boundary pin gets a short
;; stub wire + a net-name label; same name => same net (Virtuoso connect-by-name).
;; Dummy control pins get a basic/noConn marker.
;;
;; SKILL refs: schCreateInst skcompref p.235 ; schCreateWire p.254 ;
;;             schCreateWireLabel p.256 ; dbOpenCellViewByType skdfref p.898 ; dbSave p.921
;;
;;   load(".../pmu_fill.il")  ;  pmuFillTop("sim_yusheng" "PMU_top")

(defun pmuStub (cv x y dx dy net)
  (let (w)
    (setq w (schCreateWire cv "draw" "full"
              (list (list x y) (list (plus x dx) (plus y dy))) 0.0625 0.0625 0.0))
    (schCreateWireLabel cv (car w) (list (plus x dx) (plus y dy)) net
        "lowerLeft" "R0" "stick" 0.0625 nil)
    w))

(defun pmuFillTop (libName cellName)
  (let (cv mpll mvco mref mnc)
    (setq cv   (dbOpenCellViewByType libName cellName "schematic" "schematic" "a"))
    (setq mpll (dbOpenCellViewByType libName "ldo_core_pll" "symbol" "" "r"))
    (setq mvco (dbOpenCellViewByType libName "ldo_core_vco" "symbol" "" "r"))
    (setq mref (dbOpenCellViewByType libName "vref_bias"    "symbol" "" "r"))
    (setq mnc  (dbOpenCellViewByType "basic" "noConn" "symbol" "" "r"))
    ;; --- instances ---
    (schCreateInst cv mref "X_REF" (list 10.0  0.0) "R0")
    (schCreateInst cv mpll "X_PLL" (list  5.0  8.0) "R0")
    (schCreateInst cv mvco "X_VCO" (list  5.0 -3.0) "R0")
    ;; --- X_REF pins ---
    (pmuStub cv 10.0     0.0    0.0  0.5 "VSS")
    (pmuStub cv 10.125   0.0    0.0  0.5 "VDD1P8")
    (pmuStub cv 10.25    0.0    0.0  0.5 "VDD1P0")
    (pmuStub cv 10.9375 -1.0    0.5  0.0 "VREF")
    (pmuStub cv 10.9375 -0.875  0.5  0.0 "IBIAS")
    (pmuStub cv 10.9375 -0.75   0.5  0.0 "IBIAS_500N")
    (pmuStub cv 10.9375 -0.625  0.5  0.0 "IBIAS_1U")
    ;; --- X_PLL pins (origin 5,8) ---
    (pmuStub cv  5.0     7.875 -0.5  0.0 "VREF")
    (pmuStub cv  5.0     8.0   -0.5  0.0 "IBIAS")
    (pmuStub cv  6.875   8.0    0.5  0.0 "VDD0P8_PLL")
    (pmuStub cv  5.875   8.5    0.0  0.5 "VSS")
    (pmuStub cv  6.0     8.5    0.0  0.5 "VDD1P0")
    ;; --- X_VCO pins (origin 5,-3) ---
    (pmuStub cv  5.0    -3.125 -0.5  0.0 "VREF")
    (pmuStub cv  5.0    -3.0   -0.5  0.0 "IBIAS")
    (pmuStub cv  6.875  -3.0    0.5  0.0 "VDD0P8_VCO")
    (pmuStub cv  5.875  -2.5    0.0  0.5 "VSS")
    (pmuStub cv  6.0    -2.5    0.0  0.5 "VDD1P0")
    ;; --- boundary pins ---
    (pmuStub cv  0.0     6.0    0.5  0.0 "VDD1P8")
    (pmuStub cv  0.0     5.5    0.5  0.0 "VDD1P0")
    (pmuStub cv  0.0     5.0    0.5  0.0 "VSS")
    (pmuStub cv  2.0     6.0    0.5  0.0 "VDD0P8_PLL")
    (pmuStub cv  2.0     5.5    0.5  0.0 "VDD0P8_VCO")
    (pmuStub cv  2.0     5.0    0.5  0.0 "IBIAS_500N")
    (pmuStub cv  2.0     4.5    0.5  0.0 "IBIAS_1U")
    ;; --- noConn on dummy control pins (x=-2) ---
    (foreach yv (list 6.0 5.5 5.0 4.5 4.0 3.5)
      (schCreateInst cv mnc nil (list -2.0 yv) "R0"))
    (dbSave cv)
    (printf "pmuFillTop: 3 instances + nets + 6 noConn placed\n")
    t))
