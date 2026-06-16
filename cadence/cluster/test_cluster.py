"""Unit/static tests for cadence.cluster -- the pure-CLI Donau+ALPS corner driver.

NO dsub / alps / spectre / live session required: every subprocess call goes through an
injectable FAKE runner returning canned dsub/djob output. Coverage (the four contract items):
  (1) alps vs spectre command strings (golden assert on the key flags: wrapper path, -format
      ps vs psfascii, -I model tree, -ade, -mt<->cpu, -x all, -EP, -o vs -raw).
  (2) the poll state machine drives on_status pending -> running -> done via a fake runner
      feeding scripted djob outputs.
  (3) dry_run returns the assembled command and executes nothing.
  (4) a failed job surfaces an error (RunCornerError, with the dpeek tail).

Run:  python -m pytest cadence/cluster/test_cluster.py -q
"""
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
# Put .../cadence on sys.path so `import cluster` resolves as a top-level package (mirrors
# the repo's bare-import convention; avoids needing the full cadence package to import).
sys.path.insert(0, str(HERE.parent))

import cluster                                                            # noqa: E402
from cluster import alps_cli, donau                                       # noqa: E402
from cluster import run_corner as rc   # the run_corner SUBMODULE (rc.run_corner is the entry)
from cluster.donau import DonauCfg, RunResult                             # noqa: E402


# ----------------------------------------------------------------- fake runner
class FakeRunner:
    """Injectable command executor that records every argv and replies from a scripted
    table keyed by the command's leading token (dsub/djob/dkill/dpeek). djob replies can be a
    LIST (consumed one per call) to script a state sequence, or a single RunResult."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []            # list of argv lists, in order
        self._idx = {}             # per-key cursor into a list reply

    def __call__(self, argv, timeout=None, check=False):
        self.calls.append(list(argv))
        key = argv[0]
        rep = self.replies.get(key)
        if rep is None:
            raise AssertionError(f"FakeRunner: no scripted reply for {key!r} ({argv})")
        if isinstance(rep, list):
            i = self._idx.get(key, 0)
            r = rep[min(i, len(rep) - 1)]       # hold the last reply once exhausted
            self._idx[key] = i + 1
        else:
            r = rep
        return RunResult(r.returncode, r.stdout, r.stderr, list(argv))

    def cmds(self, head):
        return [c for c in self.calls if c and c[0] == head]


def ok(stdout="", stderr="", rc=0):
    return RunResult(rc, stdout, stderr, [])


def _join(cmd):
    return " ".join(str(x) for x in cmd)


# -------- shared canned outputs
DSUB_OK = ok("Submit job successfully. JOBID 37238970\n")
DJOB_PENDING = ok("JobId: 37238970  State: PENDING  Queue: short\n")
DJOB_RUNNING = ok("JobId: 37238970  State: RUNNING  Node: sinct20-hs\n")
DJOB_DONE = ok("JobId: 37238970  State: DONE  Exit: 0\n")
DJOB_FAILED = ok("JobId: 37238970  State: FAILED  Exit: 1\n")
DPEEK_TAIL = ok("alps: error while loading shared libraries: libsvadv.so\n")


# =====================================================================================
# (1) command-string goldens: alps vs spectre
# =====================================================================================
def test_alps_sim_cmd_golden():
    cmd = alps_cli.build_sim_cmd(
        "alps", "input.scs", "../psf",
        "/pdk/c1x_plus_20251210", "/run/sharedData/CDS/ahdl/input.ahdlSimDB", mt=8)
    s = _join(cmd)
    # wrapper (not raw binary), absolute, .../bin/alps
    assert cmd[0] == "/software/empyrean/alps/2026.03.hf1/bin/alps"
    # positional netlist FIRST after the exe
    assert cmd[1] == "input.scs"
    # classic PSF, never psfxl
    assert "-format ps" in s and "psfxl" not in s
    assert "-o ../psf" in s                       # alps output flag is -o
    assert "-raw" not in s                        # not the spectre flag
    # model tree = the ALPS PDK subtree, appended to the root
    assert "-I /pdk/c1x_plus_20251210/alps" in s
    assert "/spectre" not in s                    # never let the spectre tree in on alps
    assert "-ahdllibdir /run/sharedData/CDS/ahdl/input.ahdlSimDB" in s
    assert "-mt 8" in s                           # threads
    assert "-ade" in s                            # ADE names + .simDone sentinel


def test_spectre_sim_cmd_golden():
    cmd = alps_cli.build_sim_cmd(
        "spectre", "input.scs", "../psf",
        "/pdk/c1x_plus_20251210", "/run/ahdl/input.ahdlSimDB", mt=8)
    s = _join(cmd)
    assert cmd[0] == "spectre"
    assert cmd[1] == "input.scs"
    assert "-format psfascii" in s and "psfxl" not in s   # classic ascii PSF, not psfxl
    assert "-raw ../psf" in s                     # spectre output flag is -raw, not -o
    assert " -o " not in (" " + s + " ")          # no -o on the spectre side
    assert "-I /pdk/c1x_plus_20251210/spectre" in s
    assert "/alps" not in s
    assert "+mt=8" in s                            # spectre thread syntax
    assert "+aps" in s                             # APS turbo
    assert "-ade" not in s                         # -ade is alps-only


def test_mt_matches_cpu_count():
    # -mt must track the requested cpu count, not the default
    cmd = alps_cli.build_sim_cmd(
        "alps", "input.scs", "../psf", "/pdk", "/ahdl", mt=16)
    assert "-mt" in cmd and cmd[cmd.index("-mt") + 1] == "16"


def test_alps_wrapper_normalisation():
    # root, .../bin and .../bin/alps all normalise to the wrapper launcher
    for w in ("/x/alps/2026.03.hf1", "/x/alps/2026.03.hf1/", "/x/alps/2026.03.hf1/bin",
              "/x/alps/2026.03.hf1/bin/alps"):
        cmd = alps_cli.build_sim_cmd("alps", "input.scs", "o", "/pdk", "/ahdl",
                                     alps_wrapper=w)
        assert cmd[0] == "/x/alps/2026.03.hf1/bin/alps", w


def test_model_tree_leaf_not_doubled():
    # if the caller already passes the engine leaf tree, do not double-append
    cmd = alps_cli.build_sim_cmd("alps", "input.scs", "o", "/pdk/alps", "/ahdl")
    s = _join(cmd)
    assert "-I /pdk/alps" in s and "/alps/alps" not in s


def test_unknown_engine_rejected():
    with pytest.raises(ValueError):
        alps_cli.build_sim_cmd("ngspice", "input.scs", "o", "/pdk", "/ahdl")


def test_no_ade_drops_flag():
    cmd = alps_cli.build_sim_cmd("alps", "input.scs", "o", "/pdk", "/ahdl", ade=False)
    assert "-ade" not in cmd


def test_ahdllibdir_optional_omitted():
    # blank/None ahdllibdir -> NO -ahdllibdir (the sim auto-compiles VA from the netlist's
    # own ahdl_include); the rest of the command is intact.
    cmd = alps_cli.build_sim_cmd("alps", "input.scs", "o", "/pdk", None)
    assert "-ahdllibdir" not in cmd
    assert "-I" in cmd and "input.scs" in cmd and "-mt" in cmd


def test_model_dir_optional_omitted():
    # blank/None model_dir -> NO -I (the netlist's own include lines are self-contained).
    cmd = alps_cli.build_sim_cmd("alps", "input.scs", "o", None, None)
    assert "-I" not in cmd and "-ahdllibdir" not in cmd
    assert "input.scs" in cmd and cmd[-1] == "-ade"
    # spectre branch too
    cmd2 = alps_cli.build_sim_cmd("spectre", "input.scs", "o")
    assert "-I" not in cmd2 and "-ahdllibdir" not in cmd2 and "+aps" in cmd2


# =====================================================================================
# dsub wrapper golden: -A/-q/-R, -x all, -EP, payload last
# =====================================================================================
def test_dsub_cmd_golden():
    cfg = DonauCfg()
    payload = ["/w/bin/alps", "input.scs", "-format", "ps", "-o", "../psf"]
    cmd = donau.build_dsub_cmd(cfg, payload, "/work/netlist", x_all=True,
                               block=False, json=True)
    s = _join(cmd)
    assert cmd[0] == "dsub"
    assert "-A ug_rfic.rfSClass" in s
    assert "-q short" in s
    assert '-R cpu=8;mem=8000' in s or ("-R" in cmd and "cpu=8;mem=8000" in cmd)
    assert "-x all" in s                          # env propagation (FlexLM license) (§9)
    assert "-EP /work/netlist" in s               # task cwd on the node
    assert "-J" in cmd                            # JSON output for JOBID parse
    # the payload is appended AFTER all dsub flags, in order, last
    assert cmd[-len(payload):] == payload


def test_dsub_block_flag():
    cmd = donau.build_dsub_cmd(DonauCfg(), ["alps"], "/d", block=True)
    assert "-I" in cmd                            # blocking submit attaches -I
    cmd2 = donau.build_dsub_cmd(DonauCfg(), ["alps"], "/d", block=False)
    assert "-I" not in cmd2


def test_dsub_payload_must_be_list():
    with pytest.raises(TypeError):
        donau.build_dsub_cmd(DonauCfg(), "alps input.scs", "/d")


def test_donaucfg_cpu_parse():
    assert DonauCfg().cpu == 8
    assert DonauCfg(resource="cpu=16;mem=32000").cpu == 16
    assert DonauCfg(resource="mem=8000").cpu is None


# =====================================================================================
# state mapping
# =====================================================================================
@pytest.mark.parametrize("raw,expected", [
    ("State: PENDING", "pending"),
    ("State: RUNNING", "running"),
    ("State: DONE", "done"),
    ("State: FAILED", "failed"),
    ("stat=run", "running"),
    ("the job was KILLED by admin", "failed"),
    ("status: succeeded", "done"),
    ("queued and waiting", "pending"),
    ("", None),
    ("garbled no-state output", None),
    # exit-CODE field (not the LSF state word EXIT): code 0 => done, nonzero => failed.
    ("Job 37238970 Exited with exit code 0", "done"),
    ("Exit: 0", "done"),
    ("exited 0", "done"),
    ("Job done. Exit: 0", "done"),                 # explicit DONE still wins, harmless
    ("Exited with exit code 137", "failed"),
    ("exit code 1", "failed"),
    ("State: EXIT", "failed"),                      # bare LSF state word EXIT = abnormal
    ("the job hit EXIT", "failed"),                 # bare token, no code => failed
])
def test_map_state(raw, expected):
    assert donau.map_state(raw) == expected


def test_parse_job_id():
    assert donau.parse_job_id("Submit job successfully. JOBID 37238970") == "37238970"
    assert donau.parse_job_id('{"id": 12345, "queue": "short"}') == "12345"
    assert donau.parse_job_id("no id here") is None


# =====================================================================================
# (2) poll state machine: pending -> running -> done drives on_status transitions
# =====================================================================================
def test_poll_transitions_pending_running_done():
    runner = FakeRunner({"djob": [DJOB_PENDING, DJOB_PENDING, DJOB_RUNNING, DJOB_DONE]})
    seen = []
    final = donau.poll("37238970", runner,
                       on_status=lambda st, raw: seen.append(st),
                       interval=0.0, sleep=lambda s: None)
    assert final == "done"
    # on_status fires ONCE per transition (the repeated PENDING is not re-reported)
    assert seen == ["pending", "running", "done"]
    # one djob call per poll tick (4 ticks to reach done)
    assert len(runner.cmds("djob")) == 4


def test_poll_holds_state_on_empty_djob():
    # a transient empty djob reply must NOT reset the state nor re-fire on_status
    runner = FakeRunner({"djob": [DJOB_RUNNING, ok(""), DJOB_RUNNING, DJOB_DONE]})
    seen = []
    final = donau.poll("1", runner, on_status=lambda st, raw: seen.append(st),
                       interval=0.0, sleep=lambda s: None)
    assert final == "done"
    assert seen == ["running", "done"]            # empty reply held 'running', no re-fire


def test_poll_failed_is_terminal():
    runner = FakeRunner({"djob": [DJOB_RUNNING, DJOB_FAILED]})
    seen = []
    final = donau.poll("1", runner, on_status=lambda st, raw: seen.append(st),
                       interval=0.0, sleep=lambda s: None)
    assert final == "failed"
    assert seen == ["running", "failed"]


def test_poll_timeout_raises():
    # never reaches terminal -> DonauError after timeout, no infinite loop
    runner = FakeRunner({"djob": DJOB_RUNNING})   # always RUNNING
    with pytest.raises(donau.DonauError):
        donau.poll("1", runner, interval=1.0, timeout=3.0, sleep=lambda s: None)


def test_submit_parses_job_id():
    runner = FakeRunner({"dsub": DSUB_OK})
    sub = donau.submit(DonauCfg(), ["alps", "input.scs"], "/d", runner)
    assert sub["job_id"] == "37238970"
    assert runner.cmds("dsub")                    # exactly one dsub call


def test_submit_nonzero_raises():
    runner = FakeRunner({"dsub": ok("perm denied", rc=1)})
    with pytest.raises(donau.DonauError):
        donau.submit(DonauCfg(), ["alps"], "/d", runner)


def test_cancel_via_dkill():
    runner = FakeRunner({"dkill": ok("job 1 killed")})
    donau.cancel("1", runner)
    assert runner.cmds("dkill") == [["dkill", "1"]]


# =====================================================================================
# (3) dry_run returns the command and executes nothing
# =====================================================================================
def test_run_corner_dry_run_returns_cmd_no_exec(tmp_path):
    netdir = tmp_path / "netlist"
    psfdir = tmp_path / "psf"
    netdir.mkdir()

    class Boom:
        def __call__(self, *a, **k):
            raise AssertionError("runner must NOT be called on dry_run")

    cmd = rc.run_corner(str(netdir), "/pdk/c1x", "/ahdl/input.ahdlSimDB", str(psfdir),
                        engine="alps", donau=DonauCfg(), runner=Boom(), dry_run=True)
    s = _join(cmd)
    assert cmd[0] == "dsub"
    assert "-A ug_rfic.rfSClass" in s and "-q short" in s
    assert "/software/empyrean/alps/2026.03.hf1/bin/alps" in s
    assert "-format ps" in s and "-ade" in s
    assert "-I /pdk/c1x/alps" in s
    # -o is RELATIVE (../psf), mirroring ADE
    assert "-o ../psf" in s
    # nothing got written, nothing executed (no exception from Boom => never called)
    assert not psfdir.exists()


def test_run_corner_dry_run_spectre(tmp_path):
    netdir = tmp_path / "netlist"; netdir.mkdir()
    cmd = rc.run_corner(str(netdir), "/pdk/c1x", "/ahdl", str(tmp_path / "psf"),
                        engine="spectre", dry_run=True)
    s = _join(cmd)
    assert "-format psfascii" in s and "-raw ../psf" in s
    assert "-I /pdk/c1x/spectre" in s and "+mt=8" in s


def test_run_corner_refuses_resource_without_cpu(tmp_path):
    # a resource string with no cpu= cannot be matched to -mt -> refuse (no silent mt=8)
    netdir = tmp_path / "netlist"; netdir.mkdir()
    with pytest.raises(rc.RunCornerError) as ei:
        rc.run_corner(str(netdir), "/pdk", "/ahdl", str(tmp_path / "psf"),
                      engine="alps", donau=DonauCfg(resource="mem=8000"), dry_run=True)
    assert "cpu=" in str(ei.value) and "-mt" in str(ei.value)


# =====================================================================================
# (4) end-to-end happy path + failure surfacing
# =====================================================================================
def _fake_psf(tmp_path, with_simdone=True):
    psfdir = tmp_path / "psf"
    psfdir.mkdir()
    (psfdir / "ac.ac").write_bytes(b"PSFversion")     # a real-ish output file
    if with_simdone:
        (psfdir / ".simDone").write_bytes(b"")
    return psfdir


def test_run_corner_happy_path_alps(tmp_path):
    netdir = tmp_path / "netlist"; netdir.mkdir()
    psfdir = _fake_psf(tmp_path, with_simdone=True)    # sim "already produced" output
    runner = FakeRunner({
        "dsub": DSUB_OK,
        "djob": [DJOB_PENDING, DJOB_RUNNING, DJOB_DONE],
    })
    seen = []
    out = rc.run_corner(str(netdir), "/pdk/c1x", "/ahdl", str(psfdir),
                        engine="alps", runner=runner, sleep=lambda s: None,
                        poll_interval=0.0, on_status=lambda st, raw: seen.append(st))
    assert pathlib.Path(out) == psfdir
    assert seen == ["pending", "running", "done"]
    assert runner.cmds("dsub") and runner.cmds("djob")


def test_run_corner_failed_job_surfaces_error_with_tail(tmp_path):
    netdir = tmp_path / "netlist"; netdir.mkdir()
    runner = FakeRunner({
        "dsub": DSUB_OK,
        "djob": [DJOB_RUNNING, DJOB_FAILED],
        "dpeek": DPEEK_TAIL,
    })
    with pytest.raises(rc.RunCornerError) as ei:
        rc.run_corner(str(netdir), "/pdk", "/ahdl", str(tmp_path / "psf"),
                      engine="alps", runner=runner, sleep=lambda s: None, poll_interval=0.0)
    msg = str(ei.value)
    assert "FAILED" in msg and "37238970" in msg
    assert "libsvadv.so" in msg                   # the dpeek tail is surfaced
    assert runner.cmds("dpeek")


def test_run_corner_missing_simdone_surfaces_error(tmp_path):
    # job reports done but the -ade sentinel never appeared -> error (incomplete ALPS run)
    netdir = tmp_path / "netlist"; netdir.mkdir()
    psfdir = _fake_psf(tmp_path, with_simdone=False)
    runner = FakeRunner({
        "dsub": DSUB_OK,
        "djob": [DJOB_DONE],
        "dpeek": ok("...some log tail..."),
    })
    with pytest.raises(rc.RunCornerError) as ei:
        rc.run_corner(str(netdir), "/pdk", "/ahdl", str(psfdir),
                      engine="alps", runner=runner, sleep=lambda s: None, poll_interval=0.0)
    assert ".simDone" in str(ei.value)


def test_run_corner_empty_psf_dir_surfaces_error(tmp_path):
    netdir = tmp_path / "netlist"; netdir.mkdir()
    psfdir = tmp_path / "psf"; psfdir.mkdir()      # exists but empty
    runner = FakeRunner({
        "dsub": DSUB_OK,
        "djob": [DJOB_DONE],
        "dpeek": ok("log tail"),
    })
    # spectre path (no simdone required) so the empty-dir check is what fires
    with pytest.raises(rc.RunCornerError) as ei:
        rc.run_corner(str(netdir), "/pdk", "/ahdl", str(psfdir),
                      engine="spectre", runner=runner, sleep=lambda s: None, poll_interval=0.0)
    assert "empty" in str(ei.value).lower()


def test_run_corner_spectre_no_simdone_required(tmp_path):
    # spectre produces no .simDone; a non-empty psf dir is enough
    netdir = tmp_path / "netlist"; netdir.mkdir()
    psfdir = tmp_path / "psf"; psfdir.mkdir()
    (psfdir / "ac.ac").write_bytes(b"PSFversion")
    runner = FakeRunner({"dsub": DSUB_OK, "djob": [DJOB_DONE]})
    out = rc.run_corner(str(netdir), "/pdk", "/ahdl", str(psfdir),
                        engine="spectre", runner=runner, sleep=lambda s: None,
                        poll_interval=0.0)
    assert pathlib.Path(out) == psfdir


# =====================================================================================
# package surface
# =====================================================================================
def test_package_exports():
    for name in ("build_sim_cmd", "DonauCfg", "build_dsub_cmd", "submit", "poll",
                 "cancel", "map_state", "run_corner", "RunCornerError"):
        assert hasattr(cluster, name), name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
