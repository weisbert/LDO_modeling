"""Donau (`dsub`) cluster scheduler layer -- submit / poll / cancel a single job.

Donau is the company-internal scheduler (LSF-bsub-flavoured fork). Command family
(ALPS_DONAU_NOTES §2a'): dsub(submit) djob(status) dkill(cancel) dpeek(tail). Help flag is
`--help`, NOT `-help` (§2a). The submit tuple for our lightweight LDO corner is
`-A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000"` (§2a''').

The submit wraps the engine payload (alps_cli.build_sim_cmd) on the node:
    dsub -A .. -q .. -R "cpu=8;mem=8000" -x all -EP <netlistdir> -I <payload ...>
  - `-x all` propagates the submit shell's env (FlexLM LM_LICENSE_FILE etc.) to the compute
    node -- REQUIRED for the CLI path (§9: without the env, even the alps wrapper can't
    license). `-EP <netlistdir>` = task working dir on the node (so the payload's relative
    `-o ../psf` resolves) (§2a''). `-I` = block, stream status, attach to session (§2a'').
  - `-J` (json) is added on submit so we can parse the JOBID deterministically.

ALL subprocess execution goes through an injectable `runner(argv, **kw) -> RunResult`
(default SubprocessRunner). Unit tests inject a fake runner that returns canned dsub/djob
output, so NO dsub is needed to test the state machine.
"""
import dataclasses
import re
import shlex
import subprocess


# ----------------------------------------------------------------- runner contract
@dataclasses.dataclass
class RunResult:
    """The result of one runner invocation. `argv` is echoed back so a dry-run / fake can
    assert exactly what would have executed."""
    returncode: int
    stdout: str = ""
    stderr: str = ""
    argv: list = dataclasses.field(default_factory=list)


class SubprocessRunner:
    """The default real runner: a thin subprocess wrapper. Only ever fires on the company
    box (a node with dsub/djob/dkill on PATH). Tests inject a fake instead."""

    def __call__(self, argv, timeout=None, check=False):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise DonauError(
                f"command failed (rc={p.returncode}): {shlex.join(argv)}\n{p.stderr}")
        return RunResult(p.returncode, p.stdout, p.stderr, list(argv))


class DonauError(RuntimeError):
    """A Donau submit/poll/cancel failed (non-zero rc, unparseable output, or job FAILED)."""


# ----------------------------------------------------------------------- config
@dataclasses.dataclass(frozen=True)
class DonauCfg:
    """The Donau submit tuple. Defaults = the validated LDO tuple (§2a''')."""
    account: str = "ug_rfic.rfSClass"      # -A : resource account / class
    queue: str = "short"                   # -q : work queue (highest prio, 3h cap, 32G)
    resource: str = "cpu=8;mem=8000"       # -R : 8 cores / 8000 MB; ';'-separated key=value

    @property
    def cpu(self):
        """The cpu= count from the resource string (so -mt can be matched to it)."""
        m = re.search(r"cpu\s*=\s*(\d+)", self.resource)
        return int(m.group(1)) if m else None


# ------------------------------------------------------------------ command build
def build_dsub_cmd(cfg, payload_cmd, netlistdir, x_all=True, block=True, json=True):
    """Assemble the full `dsub ... <payload>` argv.

    cfg          DonauCfg (account / queue / resource)
    payload_cmd  the engine invocation as an argv list (alps_cli.build_sim_cmd output)
    netlistdir   task working dir on the node (-EP) -- where input.scs lives and the
                 payload's relative -o ../psf resolves
    x_all        propagate the submit env to the node (-x all); REQUIRED for licensing (§9)
    block        attach + stream + block the session (-I); the job dies with the session.
                 When False the submit returns immediately (async; poll via djob).
    json         add -J so the JOBID is machine-parseable from stdout
    """
    if not isinstance(payload_cmd, (list, tuple)):
        raise TypeError("payload_cmd must be an argv list, not a string")
    cmd = ["dsub", "-A", cfg.account, "-q", cfg.queue, "-R", cfg.resource]
    if x_all:
        cmd += ["-x", "all"]               # carry FlexLM + EDA env to the node (§9)
    cmd += ["-EP", str(netlistdir)]        # task cwd on the node (§2a'')
    if json:
        cmd += ["-J"]                       # JSON output -> parse JOBID (§2a'')
    if block:
        cmd += ["-I"]                       # block + stream + attach to session (§2a'')
    cmd += list(payload_cmd)               # the alps/spectre run command
    return cmd


def build_djob_cmd(job_id):
    """Status query for one job: `djob <id>` (§2a')."""
    return ["djob", str(job_id)]


def build_dkill_cmd(job_id):
    """Cancel one job: `dkill <id>` (§2a')."""
    return ["dkill", str(job_id)]


def build_dpeek_cmd(job_id):
    """Tail a running/finished job's stdout: `dpeek <id>` (§2a') -- used to surface the
    failure tail on a FAILED job."""
    return ["dpeek", str(job_id)]


# --------------------------------------------------------------- state mapping
# Map Donau/CCS job states -> our 4 canonical states. Donau is bsub-flavoured; we recognise
# both Donau words (PENDING/RUNNING/DONE/FAILED, seen streaming in §9) and LSF synonyms,
# case-insensitively, so a fork's spelling doesn't silently fall through.
_STATE_MAP = {
    "pending": "pending", "pend": "pending", "queued": "pending", "waiting": "pending",
    "configuring": "pending", "submitted": "pending", "suspended": "pending", "psusp": "pending",
    "running": "running", "run": "running", "started": "running", "active": "running",
    "done": "done", "succeeded": "done", "success": "done", "completed": "done",
    "complete": "done", "finished": "done", "exit_ok": "done",
    "failed": "failed", "fail": "failed", "exit": "failed", "error": "failed",
    "killed": "failed", "cancelled": "failed", "canceled": "failed", "aborted": "failed",
    "timeout": "failed", "exited": "failed",
}


def map_state(raw):
    """Map a raw djob status token/line -> {pending,running,done,failed}, or None if no
    known state token is present (caller keeps polling -- transient/empty output)."""
    if raw is None:
        return None
    text = str(raw).lower()
    # 1) Prefer an explicit `state[: ] <word>` / `status: <word>` field if present.
    m = re.search(r"\b(?:state|status|stat)\s*[:=]?\s*([a-z_]+)", text)
    if m and m.group(1) in _STATE_MAP:
        return _STATE_MAP[m.group(1)]
    # 2) An exit-CODE field is NOT the LSF state word EXIT: `Exit: 0` / `exited 0` /
    #    `exit code 0` => clean completion (done); a non-zero code => failed. This MUST win
    #    over the bare-token scan below, which would otherwise read the word "exit" as failed
    #    and mis-report a successful run as a failure (a real djob may phrase completion this
    #    way with no separate State: word -- ALPS_DONAU_NOTES §2c leaves the format open).
    me = re.search(r"\bexit(?:ed)?\b\s*(?:code|status|:|=)?\s*(\d+)\b", text)
    if me:
        return "done" if int(me.group(1)) == 0 else "failed"
    # 3) Else scan the whole text for any known state token (a bare LSF state word EXIT, with
    #    no code, still falls here -> failed).
    for tok, mapped in _STATE_MAP.items():
        if re.search(rf"\b{re.escape(tok)}\b", text):
            return mapped
    return None


_JOBID_RE = re.compile(r"(?:JOBID|job\s*id|jobId|\"id\"\s*:)\s*\"?(\d+)", re.IGNORECASE)


def parse_job_id(stdout):
    """Pull the numeric JOBID out of dsub stdout. Handles the streamed form
    `... JOBID 37238970 ...` (§9) and a `-J` JSON `"id": 37238970`. None if not found."""
    m = _JOBID_RE.search(stdout or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------- submit / poll
def submit(cfg, payload_cmd, netlistdir, runner, x_all=True, block=False, json=True):
    """Submit one Donau job. Returns dict(job_id, cmd, result). Raises DonauError on a
    non-zero submit or an unparseable JOBID. `runner` is injected (no dsub needed in tests).

    block=False here (async submit) is the default for the poll-driven flow: submit, parse
    the JOBID, then poll() it. block=True would attach -I and stream (the session dies with
    it) -- used only when the caller wants a blocking submit instead of polling."""
    cmd = build_dsub_cmd(cfg, payload_cmd, netlistdir, x_all=x_all, block=block, json=json)
    res = runner(cmd)
    if res.returncode != 0:
        raise DonauError(
            f"dsub submit failed (rc={res.returncode}): {res.stderr or res.stdout}".strip())
    job_id = parse_job_id(res.stdout)
    if job_id is None and not block:
        raise DonauError(
            f"could not parse JOBID from dsub stdout:\n{res.stdout}\n{res.stderr}")
    return dict(job_id=job_id, cmd=cmd, result=res)


def poll(job_id, runner, on_status=None, interval=5.0, timeout=10800.0, sleep=None):
    """Poll `djob <job_id>` until the job reaches a terminal state (done|failed) or timeout.

    Calls on_status(state, raw) ONCE per state TRANSITION (pending -> running -> done), where
    state in {pending,running,done,failed} and raw is the djob stdout that produced it. The
    initial state is always reported. Returns the terminal state ('done' | 'failed').

    timeout default 10800s = 3h (the `short` queue's wall cap, §2a'''). `sleep` is injectable
    (default time.sleep) so tests drive the loop with NO real waiting; `interval` is the
    poll period. `runner` is the injected command executor."""
    import time
    _sleep = sleep or time.sleep
    on_status = on_status or (lambda state, raw: None)
    last_state = None
    waited = 0.0
    while True:
        res = runner(build_djob_cmd(job_id))
        raw = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
        # A failed djob query itself (rc!=0) is not a job-failed signal unless its output
        # actually says so -- treat an unparseable/empty reply as "keep polling" until the
        # last seen state or timeout decides. But a clearly-FAILED state IS terminal.
        state = map_state(raw)
        if state is None:
            state = last_state           # transient/empty djob output: hold prior state
        if state is not None and state != last_state:
            on_status(state, raw)        # report only real transitions (incl. the first)
            last_state = state
        if state in ("done", "failed"):
            return state
        if waited >= timeout:
            raise DonauError(
                f"job {job_id} did not reach a terminal state within {timeout:.0f}s "
                f"(last state: {last_state}). Check djob/dpeek; the cluster job may be hung.")
        _sleep(interval)
        waited += interval


def cancel(job_id, runner):
    """Cancel a job via `dkill <id>` (§2a'). Returns the RunResult; raises DonauError on a
    non-zero dkill."""
    res = runner(build_dkill_cmd(job_id))
    if res.returncode != 0:
        raise DonauError(
            f"dkill {job_id} failed (rc={res.returncode}): {res.stderr or res.stdout}".strip())
    return res


def peek_tail(job_id, runner, lines=40):
    """Best-effort tail of a job's stdout via `dpeek <id>` (§2a'), for surfacing the failure
    reason. Never raises -- returns '' if dpeek isn't available / errors."""
    try:
        res = runner(build_dpeek_cmd(job_id))
    except Exception:                      # noqa: BLE001  (diagnostic-only; never fatal)
        return ""
    text = res.stdout or res.stderr or ""
    tail = text.splitlines()[-lines:]
    return "\n".join(tail)
