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


;; --- enable ONLY our extraction test -----------------------------------------
(defun insituEnableOnly (sess testName)
  ;; Ref: axlGetTests adexl p.282 ; axlGetTest p.281 ; axlSetEnabled p.156
  (let (sdb names h)
    (setq sdb (insituSdb sess))
    (setq names (cadr (axlGetTests sdb)))
    (foreach n names
      (setq h (axlGetTest sdb n))
      (when h (axlSetEnabled h (equal n testName))))
    t))


;; --- snapshot / restore the designer's test-enable state (don't mutate the session) ---
(defun insituSnapshotEnabled (sess)
  ;; Returns ((name enabled) ...) so the run loop can restore the designer's ADE exactly.
  ;; Ref: axlGetTests p.282 ; axlGetTest p.281 ; axlGetEnabled (companion of axlSetEnabled
  ;; p.156; used by simkit pvtRunner).
  (let (sdb out h)
    (setq sdb (insituSdb sess))
    (foreach n (cadr (axlGetTests sdb))
      (setq h (axlGetTest sdb n))
      (when h (setq out (cons (list n (axlGetEnabled h)) out))))
    (reverse out)))

(defun insituRestoreEnabled (sess snap)
  (let (sdb h)
    (setq sdb (insituSdb sess))
    (foreach pair snap
      (setq h (axlGetTest sdb (car pair)))
      (when h (axlSetEnabled h (cadr pair))))
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
  ;; Returns (completed total) points -- DONE when completed>=total (RESTS at (N,N),
  ;; NOT (0,0)). nil if no run has registered yet. NB: this SESSION-AGGREGATE form can be
  ;; poisoned ("Cannot find a setup database entry for handle 0") after many renames, and
  ;; sums points across ALL histories -- prefer the per-history insituHistStatus below.
  (car (errset (axlGetRunStatus sess) nil)))


;; --- per-history run status (robust) -----------------------------------------
;; The session-aggregate axlGetRunStatus(sess) errors once a stale history handle exists
;; (rename-heavy runs) and double-counts across histories. Query the run's OWN history:
(defun insituCurHist (sess)
  ;; Current history NAME (string) or nil. Ref: axlGetCurrentHistory adexl p.417 ;
  ;; axlGetHistoryName adexl p.427.
  (let (h)
    (setq h (car (errset (axlGetCurrentHistory sess) nil)))
    (if h (car (errset (axlGetHistoryName h) nil)) nil)))

(defun insituHistStatus (sess hist)
  ;; (completed total) for ONE history -- DONE when completed>=total. Ref: axlGetRunStatus
  ;; adexl p.386/387 (?historyName arg).
  (if hist (car (errset (axlGetRunStatus sess ?historyName hist) nil)) nil))

(defun insituRename (sess histName)
  ;; BEST-EFFORT rename of the current (idle) history to histName for readable Maestro
  ;; names. Only call after idle (a non-idle rename -> ASSEMBLER-2423). A re-run reuses the
  ;; same tag, so histName may already exist -> axlSetHistoryName errors; that is NOT fatal
  ;; (the PSF is under the actual history dir), so we swallow it and KEEP the current name.
  ;; ALWAYS returns the history's actual resulting name. Ref: axlGetCurrentHistory p.417 ;
  ;; axlSetHistoryName p.435 ; axlGetHistoryName p.427.
  (let (h)
    (setq h (car (errset (axlGetCurrentHistory sess) nil)))
    (unless h (error "insituRename: axlGetCurrentHistory nil"))
    (errset (axlSetHistoryName h histName) nil)        ; swallow name-collision / 2423
    (car (errset (axlGetHistoryName h) nil))))          ; actual name (renamed or original)
