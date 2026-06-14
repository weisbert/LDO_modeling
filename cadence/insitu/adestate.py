"""ADE-L state inheritance for the in-situ extraction test -- the 1610/1707 fix.

`insitu_extract` is created bare by axlPutTest: it carries the schematic but NONE of the
designer's ADE setup, so a Maestro run raised
  * ASSEMBLER-1610 -- design variables flo/VDD/VDD1P0 present on the test but EMPTY, and
  * ASSEMBLER-1707 -- the ac/noise analysis objects exist but are disabled+unconfigured.
This module backfills that state on the live ADE-L session, reusing the DESIGNER'S OWN
operating point (never hardcoding values), which is the whole point of in-situ extraction:

  vars  (fixes 1610): read the designer test's design variables (asiGetDesignVarList) and
        apply the non-acm_* ones to our test (asiAddDesignVarList) -- flo/VDD/VDD1P0 inherit
        the designer's exact in-situ values.
  analyses (fixes 1707): set each analysis' sweep from the manifest (asiSetAnalysisFieldVal)
        and enable it (asiEnableAnalysis); the run loop enables ONLY the analysis a given
        one-hot group needs (asiDisableAnalysis the sibling), so ac runs don't pay for noise.

Bridge from an ADE-XL test to its ADE-L session = the documented two-step
axlGetToolSession(sess,test) -> asiGetSession(sev) (adexl p.36-37). EVERY call here was read
from the SKILL reference AND proven live on fnxSession0 before being committed to code.

SKILL refs: axlGetToolSession adexl p.36 ; asiGetSession skart p.648 ; asiGetDesignVarList
p.637 ; asiAddDesignVarList p.616 ; asiGetAnalysis p.384 ; asiSetAnalysisFieldVal p.403 ;
asiEnableAnalysis p.381 ; asiDisableAnalysis p.376 ; asiIsAnalysisEnabled p.401 ;
axlGetTests adexl p.282 ; axlGetTestToolArgs p.283 ; axlSaveSetup p.136.
"""
from skillbridge import Symbol

# sev analysis field names that the spectre ac/noise forms actually expose (discovered live
# on the designer's test: asiGetAnalysisFieldVal returns '' for a valid-but-empty field and
# nil for a non-field). We only push manifest tokens whose key is one of these.
_ANALYSIS_KEYS = ("start", "stop", "dec", "lin", "log", "step", "center", "span", "freq",
                  "oprobe", "iprobe", "sweeptype", "noisetype")


def _ts(ws, sess, test):
    """The per-test ADE-L session object (the bridge). adexl p.36 -> skart p.648."""
    sev = ws["axlGetToolSession"](sess, test)
    if not sev:
        raise RuntimeError(f"adestate: no toolSession for test {test!r} in session {sess!r}")
    ts = ws["asiGetSession"](sev)
    if not ts:
        raise RuntimeError(f"adestate: asiGetSession nil for test {test!r}")
    return ts


def inherit_vars(ws, sess, src_test, dst_test):
    """Copy the designer's design-variable values (minus acm_*) onto our test -> fixes 1610.
    Read-only on the designer's session; writes only to dst_test. Returns [[name,val],...]."""
    sdv = ws["asiGetDesignVarList"](_ts(ws, sess, src_test)) or []
    keep = [[n, v] for (n, v) in sdv if not str(n).startswith("acm_")]
    if keep:
        ws["asiAddDesignVarList"](_ts(ws, sess, dst_test), keep)   # add/update on our test
    return keep


def parse_analysis(line):
    """'ac start=10 stop=500M dec=20' -> ('ac', {'start':'10','stop':'500M','dec':'20'}).
    Keeps only key=val tokens whose key is a real sev field (drops anything else)."""
    toks = str(line).split()
    name = toks[0]
    fields = {}
    for t in toks[1:]:
        if "=" in t:
            k, v = t.split("=", 1)
            if k in _ANALYSIS_KEYS:
                fields[k] = v
    return name, fields


def config_analysis(ws, sess, test, ana_name, fields, enable=True):
    """Set an analysis' sweep fields, then enable (or disable) it -> fixes 1707 when enabled.
    fields = {'start':'10', ...}. Returns the analysis object."""
    ts = _ts(ws, sess, test)
    ana = ws["asiGetAnalysis"](ts, Symbol(ana_name))            # skart p.384
    if not ana:
        raise RuntimeError(f"adestate: test {test!r} has no {ana_name!r} analysis object")
    for k, v in fields.items():
        ws["asiSetAnalysisFieldVal"](ana, Symbol(k), str(v))    # skart p.403
    ws["asiEnableAnalysis" if enable else "asiDisableAnalysis"](ana)   # p.381 / p.376
    return ana


def enable_only_analysis(ws, sess, test, ana_name, siblings=("ac", "noise")):
    """Enable ana_name, disable the other siblings -- so a one-hot group's run computes only
    what it needs (an ac group must not drag a noise analysis, which would also need an
    oprobe). Returns the set of enabled analyses."""
    ts = _ts(ws, sess, test)
    on = set()
    for s in siblings:
        ana = ws["asiGetAnalysis"](ts, Symbol(s))
        if not ana:
            continue
        if s == ana_name:
            ws["asiEnableAnalysis"](ana); on.add(s)
        else:
            ws["asiDisableAnalysis"](ana)
    return on


def set_noise_oprobe(ws, sess, test, onet, fields=None):
    """A spectre noise analysis measures ONE output -> set its oprobe to `onet` (and re-apply
    the sweep), then enable it. Used per-output by the run loop (one noise run per v_out)."""
    f = dict(fields or {})
    f["oprobe"] = onet
    return config_analysis(ws, sess, test, "noise", f, enable=True)


def find_src_test(ws, sess, m):
    """The designer's ADE test that holds the in-situ OP: the one whose cell == manifest
    tb_cell. Override with manifest dut.ade_src_test. (insitu_extract / the extract cell are
    skipped.)  adexl p.282/283."""
    explicit = m["dut"].get("ade_src_test")
    if explicit:
        return explicit
    sdb = ws["axlGetMainSetupDB"](sess)
    names = ws["axlGetTests"](sdb)[1] or []
    want, ext = m["dut"]["tb_cell"], m["dut"].get("extract_cell")
    for n in names:
        args = dict(ws["axlGetTestToolArgs"](ws["axlGetTest"](sdb, n)) or [])
        if args.get("cell") in (ext, "Test_PMU_extract") or n == "insitu_extract":
            continue
        if args.get("cell") == want:
            return n
    raise RuntimeError(f"adestate: no designer ADE test with cell={want!r}; "
                       f"set dut.ade_src_test in the manifest")


def inherit_state(ws, m, sess, dst_test, verbose=False):
    """Full 1610+1707 fix for one manifest run: inherit the designer's OP vars and configure
    the ac (and noise) analyses on dst_test. Both analyses are left CONFIGURED; the run loop
    calls enable_only_analysis() per group. Returns a summary dict."""
    src = find_src_test(ws, sess, m)
    vars_applied = inherit_vars(ws, sess, src, dst_test)
    out = {"src_test": src, "vars": vars_applied, "analyses": {}}
    for kind in ("ac", "noise"):
        line = m["analysis"].get(kind)
        if not line:
            continue
        name, fields = parse_analysis(line)
        if kind == "noise" and "oprobe" not in fields and m["v_out"]:
            # placeholder; the run loop overrides per-output (set_noise_oprobe)
            fields["oprobe"] = next(iter(m["v_out"].values()))["net"]
        config_analysis(ws, sess, dst_test, name, fields, enable=True)
        out["analyses"][name] = fields
    if verbose:
        print(f"adestate: inherited {len(vars_applied)} vars from {src!r}, "
              f"configured {list(out['analyses'])}")
    return out
