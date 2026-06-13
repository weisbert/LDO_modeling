;; ============================================================================
;; insitu_run.il -- Mechanism A (P3): run-drive for the in-situ extraction.
;;
;; Reuses simkit's PROVEN axlRunAllTests sequence (Submit -> poll -> Rename) -- the run
;; TRIGGER that "is equivalent to manual click" and therefore inherits whatever the
;; company's Maestro session is configured to do (simulator = ALPS, submit = cluster via
;; Job Setup). So the same call that runs local Spectre here submits to the cluster Monday
;; with NO code change (see memory simkit-mechanism-a-reference / pvtRunner.il).
;;
;; We do NOT import simkit -- we copy the small, proven sequence into our own file.
;;
;; Per measurement GROUP the Python driver sets exactly one acm_* design variable to 1
;; (axlPutVar) then runs; AC superposition makes every saved port in that one run an
;; identifiable transfer (7 runs -> all 14 measurements).
;;
;; SKILL refs (virtuoso-skill index, mandatory):
;;   axlGetWindowSession adexl p.31  axlGetMainSetupDB adexl p.30  axlPutTest adexl p.133
;;   axlSetTestToolArgs  adexl p.286 axlPutVar         adexl p.178 axlGetTests adexl p.282
;;   axlSetEnabled       adexl p.156 axlGetTest        adexl p.281 axlRunAllTests adexl p.393
;;   axlGetRunStatus     adexl p.386 axlGetCurrentHistory adexl p.417 axlSetHistoryName p.435
;;
;;   load("cadence/skill/insitu_run.il")
;; ============================================================================

;; --- resolve the setup database for a session name ---------------------------
(defun insituSdb (sess)
  ;; Ref: axlGetMainSetupDB adexl p.30
  (let (sdb)
    (setq sdb (axlGetMainSetupDB sess))
    (unless sdb (error "insituSdb: no setupDB for session %L" sess))
    sdb))


;; --- find-or-create the extraction test, pointed at <lib>/<cell>/schematic ---
(defun insituEnsureTest (sess testName libName cellName @optional (sim "spectre"))
  ;; Ref: axlPutTest adexl p.133 ; axlSetTestToolArgs adexl p.286/287 (the documented
  ;; from-scratch ADE-XL test creation flow).
  (let (sdb th)
    (setq sdb (insituSdb sess))
    (setq th (axlPutTest sdb testName "ADE"))
    (unless th (error "insituEnsureTest: axlPutTest %s failed" testName))
    (axlSetTestToolArgs th (list (list "lib" libName)
                                 (list "cell" cellName)
                                 (list "view" "schematic")
                                 (list "sim" sim)))
    th))


;; --- enable ONLY our extraction test (snapshot/restore is the caller's job) ---
(defun insituEnableOnly (sess testName)
  ;; Ref: axlGetTests adexl p.282 ; axlGetTest p.281 ; axlSetEnabled p.156
  (let (sdb names h)
    (setq sdb (insituSdb sess))
    (setq names (cadr (axlGetTests sdb)))
    (foreach n names
      (setq h (axlGetTest sdb n))
      (when h (axlSetEnabled h (equal n testName))))
    t))


;; --- set one design variable (the acm one-hot) -------------------------------
(defun insituPutVar (sess name value)
  ;; Ref: axlPutVar adexl p.178 (sets a global design variable on the setupDB)
  (axlPutVar (insituSdb sess) name (if (stringp value) value (lsprintf "%L" value))))


;; --- the proven Submit -> poll -> Rename run sequence (from pvtRunner) --------
(defun insituSubmit (sess)
  ;; Fire-and-forget; sims run in the background (DECISIONS #54). Ref: axlRunAllTests p.393
  (let (rc)
    (setq rc (errset (axlRunAllTests sess "") nil))
    (unless rc (error "insituSubmit: axlRunAllTests failed (session %L)" sess))
    t))

(defun insituStatus (sess)
  ;; Pure read; safe during the async tail. Ref: axlGetRunStatus adexl p.386.
  ;; Returns [code sub] (idle = (0 0)) or nil.
  (car (errset (axlGetRunStatus sess) nil)))

(defun insituRename (sess histName)
  ;; DESTRUCTIVE -- only after idle (else ASSEMBLER-2423). Ref: axlGetCurrentHistory
  ;; p.417 ; axlSetHistoryName p.435.
  (let (h)
    (setq h (car (errset (axlGetCurrentHistory sess) nil)))
    (unless h (error "insituRename: axlGetCurrentHistory nil"))
    (axlSetHistoryName h histName)
    (car (errset (axlGetHistoryName h) nil))))
