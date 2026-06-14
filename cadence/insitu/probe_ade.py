"""Read-ONLY live probe for the ADE-XL session (Mechanism A debug doctor).

Answers the synthesis live-checks in ONE pass, touching nothing (no test creation, no run,
no var/analysis writes). Run it after the designer's session is up and idle:

    cd cadence && python -m insitu.probe_ade               # active ADE-XL window's session
    cd cadence && python -m insitu.probe_ade fnxSession0   # force a session name

What it reports (each line maps to a live-check in the fix plan):
  [0] channel liveness  -- if it hangs, a modal dialog is freezing the SKILL evaluator
  [1] the REAL session name (axlGetWindowSession) vs the assumed one
  [2] setupDB handle shape -- MUST be an int, not t (the insituSdb plumbing bug)
  [3] every test: lib/cell/view/sim/state/path + enabled  -- clone prereq (state/path)
  [4] global vars + values (flo/VDD/VDD1P0?), sweep-encoded?  -- fixes-1610 inputs
  [5] axlGetAllVarsDisabled  -- the global-vars gate
  [6] the ADE-L bridge (axlGetToolSession->asiGetSession) per test, then the REAL field
      names+values of the 'ac and 'noise analyses, DISCOVERED not hardcoded -- fixes-1707

SKILL refs (virtuoso-skill index): axlGetWindowSession adexl p.38; axlGetMainSetupDB p.31;
axlGetTests p.282; axlGetTest p.281; axlGetTestToolArgs p.283; axlGetVars p.175;
axlGetVar p.173; axlGetVarValue p.177; axlGetAllVarsDisabled p.180; axlGetToolSession p.36;
asiGetSession skart p.648; asiGetAnalysis p.384; asiGetAnalysisFieldVal p.387.
Nothing here is destructive.
"""
import sys

# spectre sev analysis field-name CANDIDATES. asiGetAnalysisFieldVal returns nil for a field
# that does not exist (skart p.387), so probing a generous set and keeping the non-nil hits
# discovers the REAL field names without hardcoding the simulator's form layout.
_AC_FIELDS = ["from", "to", "dec", "lin", "log", "sweeptype", "start", "stop", "step",
             "center", "span", "pts", "points", "values", "freq"]
_NOISE_FIELDS = _AC_FIELDS + ["oprobe", "iprobe", "p1", "n1", "pnoise", "noisetype",
                              "output", "input", "noiseout", "refsource"]


def _g(ws, fn, *a):
    """Call a SKILL fn, never raise (errset-style). Returns value or an '<ERR ...>' marker."""
    try:
        return ws[fn](*a)
    except Exception as e:                                     # noqa: BLE001
        return f"<ERR {fn}: {type(e).__name__}: {str(e)[:100]}>"


def _err(v):
    return isinstance(v, str) and v.startswith("<ERR")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    want_sess = argv[0] if len(argv) > 0 else None

    from skillbridge import Workspace, Symbol
    ws = Workspace.open()
    print("== insitu ADE probe (READ-ONLY) ==")

    # [0] liveness -- if this hangs, a modal dialog is freezing the SKILL evaluator
    print("[0] channel plus(2,3) =>", _g(ws, "plus", 2, 3), " (expect 5; a hang => close Virtuoso dialogs)")

    # [1] the REAL session name (adexl p.38) vs whatever we assumed
    real = _g(ws, "axlGetWindowSession")
    print(f"[1] axlGetWindowSession() => {real!r}   (assumed/CLI default: {want_sess or 'fnxSession0'!r})")
    sess = want_sess or (real if isinstance(real, str) else "fnxSession0")

    # [2] setupDB handle -- MUST be an int (adexl p.31); bool t means we called it wrong
    sdb = _g(ws, "axlGetMainSetupDB", sess)
    ok_sdb = isinstance(sdb, int) and not isinstance(sdb, bool)
    print(f"[2] axlGetMainSetupDB({sess!r}) => {sdb!r} type={type(sdb).__name__}  "
          f"{'OK(int handle)' if ok_sdb else 'BAD -> session name likely wrong'}")
    if not ok_sdb and isinstance(real, str) and real != sess:
        sess2, sdb2 = real, _g(ws, "axlGetMainSetupDB", real)
        print(f"    retry real session {sess2!r} => sdb={sdb2!r}")
        if isinstance(sdb2, int) and not isinstance(sdb2, bool):
            sess, sdb, ok_sdb = sess2, sdb2, True
    if not ok_sdb:
        print("    cannot continue without a valid setupDB handle; stopping.")
        return 1

    # [3] tests: lib/cell/view/sim/state/path + enabled (adexl p.281/283)
    tl = _g(ws, "axlGetTests", sdb)
    names = tl[1] if isinstance(tl, (list, tuple)) and len(tl) > 1 else None
    print(f"[3] tests => {names}")
    for n in (names or []):
        th = _g(ws, "axlGetTest", sdb, n)
        args = _g(ws, "axlGetTestToolArgs", th)
        en = _g(ws, "axlGetEnabled", th)
        d = {k: v for k, v in (args if isinstance(args, list) else [])} if not _err(args) else {}
        print(f"    TEST {n!r:24} enabled={en}  state={d.get('state')!r} path={d.get('path')!r}")
        print(f"        toolArgs={args}")

    # [4] global vars + values (adexl p.175/177) -- do the 1610 names live here?
    vt = _g(ws, "axlGetVars", sdb)
    vnames = vt[1] if isinstance(vt, (list, tuple)) and len(vt) > 1 else None
    print(f"[4] global vars => {vnames}")
    for vn in (vnames or []):
        vh = _g(ws, "axlGetVar", sdb, vn)
        val = _g(ws, "axlGetVarValue", vh)
        swept = isinstance(val, str) and len(val.split()) > 1
        print(f"    {vn:20} = {val!r}{'   <-- SWEEP-ENCODED (multi-point!)' if swept else ''}")
    for need in ("flo", "VDD", "VDD1P0"):
        here = bool(vnames) and need in vnames
        print(f"    1610-name {need!r}: {'PRESENT as global' if here else 'ABSENT here (per-test local? read off ADE-L session)'}")

    # [5] global-vars gate (adexl p.180): t => globals NOT included in the run
    print(f"[5] axlGetAllVarsDisabled => {_g(ws,'axlGetAllVarsDisabled',sdb)!r}  (t = globals OFF -> need axlSetAllVarsDisabled 0)")

    # [6] the ADE-L bridge + REAL analysis field discovery (adexl p.37; skart p.384/387)
    print("[6] ADE-L bridge + analysis fields (the fixes-1707 inputs):")
    for n in (names or []):
        sev = _g(ws, "axlGetToolSession", sess, n)             # adexl p.36
        ts = _g(ws, "asiGetSession", sev)                      # skart p.648
        simn = _g(ws, "asiGetSimName", ts)
        print(f"    TEST {n!r}: toolSession={sev!r} asiSession={ts!r} sim={simn!r}")
        if _err(ts) or not ts:
            continue
        for ana_name, cands in (("ac", _AC_FIELDS), ("noise", _NOISE_FIELDS)):
            ana = _g(ws, "asiGetAnalysis", ts, Symbol(ana_name))   # skart p.384
            if _err(ana) or not ana:
                print(f"        {ana_name}: {ana!r}")
                continue
            hits = {}
            for fld in cands:
                v = _g(ws, "asiGetAnalysisFieldVal", ana, Symbol(fld))  # skart p.387
                if v not in (None, False, [], "") and not _err(v):
                    hits[fld] = v
            print(f"        {ana_name} REAL fields => {hits}")
    print("== probe done ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
