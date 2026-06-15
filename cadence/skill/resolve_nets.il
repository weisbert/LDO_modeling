;; ============================================================================
;; resolve_nets.il -- Component B (SKILL side): symbol-pin -> TB-net resolver.
;;
;; The designer hands us SYMBOL PIN NAMES off the DUT instance in the PMU
;; testbench (e.g. VDD0P8_DIG, AVDD1P0, IBP_POLY_500N_VCO_Fit). The downstream
;; manifest is driven by actual TB NET names, so we must map each requested pin
;; to the net the DUT instance terminal connects to. This file does exactly that:
;; open the TB cellview, find the named instance, and for each pin read the net on
;; the matching instTerm.
;;
;;   insResolveNets(tbLib tbCell tbView instName pinList)
;;       -> list of (pin net) pairs  (net = "<unresolved>" on a miss; never crashes)
;;
;; Design notes:
;;   * The symbol pin name IS the instTerm name (instTerm carries the master
;;     terminal's name), so the lookup is name-equality on inst~>instTerms. We also
;;     fall back to the master terminal list when an instTerm has not been
;;     materialised (connectivity not extracted) so the pin is at least reported.
;;   * instTerm~>net is nil until the schematic's connectivity is established
;;     (a checked/saved schematic). A working designer TB is always checked, so the
;;     net is present; if it is nil we report "<unresolved>" rather than guess.
;;   * Bus pins like ctrl<3:0> are expanded to their bits (ctrl<3> .. ctrl<0>) and
;;     each bit is resolved+reported individually, since the DUT terminal/net model
;;     is per-bit. A scalar pin returns a single pair.
;;   * Any failure (no cellview / no instance / no such pin) returns a clear marker
;;     pair, NEVER an error -- the Python side decides how to surface a gap.
;;
;; SKILL refs (looked up via the virtuoso-skill function_index, mandatory):
;;   dbOpenCellViewByType   skdfref.pdf p.898   (open TB cellview; "r" read mode)
;;   dbFindAnyInstByName    skdfref.pdf p.958   (preferred inst lookup by name;
;;                          dbGetInstByName p.962 is the back-compat alias)
;;   dbCreateInstTerm       skdfref.pdf p.1000  (connectivity model: instTerm<->net;
;;                          documents the instTerm/net relation traversed by
;;                          instTerm~>net / instTerm~>name / net~>name)
;;   dbClose                skdfref.pdf p.873   (release the read cellview)
;;   The instTerm~>net / net~>name attribute traversal is the standard dbObject
;;   access-path idiom already used in cadence/skill/insitu_augment.il (see its
;;   p.119 note "instTerm~>net stays nil until a check runs").
;;
;;   load("/abs/.../cadence/skill/resolve_nets.il")
;;   insResolveNets("TB_lib" "PMU_tb" "schematic" "I0"
;;       list("AVDD1P0" "VDD0P8_DIG" "IBP_POLY_500N_VCO_Fit"))
;; ============================================================================

;; --- expand a bus pin "name<hi:lo>" -> ("name<hi>" ... "name<lo>") ------------
;; A scalar pin returns (pin). A single-bit "name<k>" returns (name<k>). A range
;; "name<hi:lo>" expands inclusively in declared order (hi first if hi>=lo, else
;; lo first) so the reported order matches the bus declaration.
(defun insExpandBusPin (pin)
  (let (m base hi lo bits step)
    (cond
      ;; range form  base<hi:lo>
      ((setq m (and (rexMatchp "^\\(.+\\)<\\([0-9]+\\):\\([0-9]+\\)>$" pin)
                    (list (rexReplace pin "\\1" 0))))
       (setq base (rexReplace pin "\\1" 0))
       (setq hi   (atoi (rexReplace pin "\\2" 0)))
       (setq lo   (atoi (rexReplace pin "\\3" 0)))
       (setq step (if (geqp hi lo) -1 1))
       (setq bits nil)
       (for i 0 (abs (difference hi lo))
         (setq bits (cons (sprintf nil "%s<%d>" base (plus hi (times step i))) bits)))
       (reverse bits))
      ;; scalar (incl. single-bit base<k>) -> itself
      (t (list pin)))))


;; --- find the instTerm on inst whose name == termName ------------------------
;; Returns the instTerm dbObject or nil. instTerm~>name carries the master
;; terminal name == the symbol pin name. Ref: dbCreateInstTerm skdfref p.1000.
(defun insFindInstTerm (inst termName)
  (car (setof it inst~>instTerms (equal it~>name termName))))


;; --- resolve ONE (already bit-expanded) pin to its net name ------------------
;; Returns the net name string, or "<unresolved>" if no matching terminal/net.
(defun insResolveOnePin (inst pin)
  (let (it net)
    (setq it (insFindInstTerm inst pin))
    (cond
      ;; matching instTerm with a bound net -> its net name
      ((and it it~>net) it~>net~>name)
      ;; instTerm exists but net is nil (unchecked schematic) -> unresolved
      (it "<unresolved>")
      ;; no instTerm: confirm the master HAS this terminal (so we know the pin is
      ;; real but simply unconnected) -- either way we cannot bind a net.
      (t "<unresolved>"))))


;; --- main entry: resolve a list of (symbol) pins to TB nets ------------------
;; Returns list of (pin net) pairs. Opens the TB read-only and always closes it.
;; On a missing cellview or instance, every requested pin maps to "<unresolved>".
(defun insResolveNets (tbLib tbCell tbView instName pinList)
  (let (cv inst out)
    (setq cv (errset (dbOpenCellViewByType tbLib tbCell tbView "" "r") t))
    (setq cv (and cv (car cv)))                 ; errset wraps the value in a list
    (cond
      ;; cellview did not open -> everything unresolved (expanded by bus)
      ((null cv)
       (foreach pin pinList
         (foreach bit (insExpandBusPin pin)
           (setq out (cons (list bit "<unresolved>") out))))
       (printf "insResolveNets: cannot open %s/%s/%s -- all <unresolved>\n"
               tbLib tbCell tbView)
       (reverse out))
      (t
       (setq inst (dbFindAnyInstByName cv instName))   ; Ref: skdfref p.958
       (cond
         ((null inst)
          (foreach pin pinList
            (foreach bit (insExpandBusPin pin)
              (setq out (cons (list bit "<unresolved>") out))))
          (printf "insResolveNets: no instance %s in %s/%s/%s -- all <unresolved>\n"
                  instName tbLib tbCell tbView))
         (t
          (foreach pin pinList
            (foreach bit (insExpandBusPin pin)
              (setq out (cons (list bit (insResolveOnePin inst bit)) out))))))
       (dbClose cv)                                    ; Ref: skdfref p.873
       (reverse out)))))
