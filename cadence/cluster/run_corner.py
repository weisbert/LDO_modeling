"""run_corner -- the one-corner CLI driver: submit -> poll -> verify -> return the PSF dir.

The top of Component A. Given a prepared netlist dir (input.scs + a compiled -ahdllibdir),
it assembles the engine command (alps_cli), submits it through Donau (donau), polls to
completion, verifies the classic-PSF output landed (+ the `.simDone` sentinel under -ade),
and returns the PSF directory for the caller to feed to binpsf.py / importmp -- the npz
firewall is untouched (no PSF parsing happens here).

This is the cluster analogue of cadence/insitu/run.py's ADE backend: same destination (a
PSF dir per corner), different launcher (pure dsub+alps instead of axlRunAllTests). Off the
company box there is no dsub/alps, so `dry_run=True` returns the assembled command without
executing, and the real path takes an injectable `runner` (tests inject a fake).

Storage convention (SHARED CONTRACT): our workarea is
  $WORK_ROOT/ldo_modeling/<Lib>__<Cell>/<corner>/{netlist,psf,npz,model}
The caller passes `netlistdir` (the .../netlist dir) and `out_psf_dir` (the sibling
.../psf). We NEVER write into the designer's spine $WORK_ROOT/simulation/<Lib>/<Cell>/.
"""
import pathlib

from . import alps_cli
from . import donau as _donau
from .donau import DonauCfg


class RunCornerError(RuntimeError):
    """A corner run failed: submit error, job FAILED (with the dpeek tail), or the expected
    PSF output / .simDone sentinel never appeared."""


# Default netlist name ADE/our emit writes (positional first arg to the engine).
INPUT_SCS = "input.scs"
SIMDONE = ".simDone"          # the 0-byte sentinel -ade drops in -o when the run finishes


def run_corner(netlistdir, pdk_model_dir, ahdllibdir, out_psf_dir,
               engine="alps", donau=None, on_status=None, runner=None,
               dry_run=False, input_scs=INPUT_SCS, alps_wrapper=alps_cli.ALPS_WRAPPER_DEFAULT,
               poll_interval=5.0, poll_timeout=10800.0, sleep=None, require_simdone=None):
    """Run ONE process corner via the pure-CLI Donau+ALPS path. Returns the PSF dir (str).

    netlistdir     dir holding input.scs (the task cwd on the node, -EP); -o ../psf resolves
                   relative to it. The payload's out-dir is computed relative to this.
    pdk_model_dir  PDK model ROOT (contains {alps,spectre} subtrees); the engine's subtree
                   is selected automatically (§1d).
    ahdllibdir     compiled AHDL/VA model DB (-ahdllibdir).
    out_psf_dir    where the PSF lands (the sibling .../psf). The engine -o/-raw is passed as
                   the path RELATIVE to netlistdir when possible (matches ADE's `-o ../psf`),
                   so the node writes to the right place; the returned dir is the absolute
                   out_psf_dir for the caller.
    engine         'alps' (default) | 'spectre'.
    donau          DonauCfg (pinned-interface name); threads (-mt) are matched to its cpu=
                   count. Defaults to DonauCfg() (the validated LDO tuple).
    on_status      callback(state, raw) fired on each state transition
                   (pending -> running -> done|failed). state in those 4 words.
    runner         injected command executor (default real subprocess) -- so tests need no
                   dsub/alps present.
    dry_run        assemble and RETURN the dsub command (a list) WITHOUT executing anything.
    require_simdone  verify the .simDone sentinel exists post-run. Default: True for alps
                   with -ade (it drops the sentinel), False for spectre (no sentinel)."""
    cfg = donau or DonauCfg()
    runner = runner or _donau.SubprocessRunner()
    # -mt MUST equal the Donau cpu= allocation (§1c). If the resource string carries no cpu=
    # count we CANNOT match it -> refuse rather than silently default to 8 on a (default cpu=1)
    # allocation, which would oversubscribe the node.
    mt = cfg.cpu
    if mt is None:
        raise RunCornerError(
            f"DonauCfg.resource={cfg.resource!r} has no cpu= count, so -mt cannot be matched "
            f"to the Donau allocation (oversubscription risk). Use resource='cpu=N;mem=M'.")
    if require_simdone is None:
        require_simdone = (engine == "alps")

    netdir = pathlib.Path(netlistdir)
    out_abs = pathlib.Path(out_psf_dir)
    # Pass -o RELATIVE to the node cwd (-EP netlistdir) when out is reachable from it, exactly
    # like ADE's `-o ../psf`; else fall back to the absolute path. The node resolves it; we
    # keep out_abs for our own post-run verification + return value.
    out_arg = _relpath(out_abs, netdir)

    payload = alps_cli.build_sim_cmd(
        engine, input_scs, out_arg, pdk_model_dir, ahdllibdir, mt=mt,
        alps_wrapper=alps_wrapper, ade=(engine == "alps"))
    dsub_cmd = _donau.build_dsub_cmd(cfg, payload, netdir, x_all=True, block=False, json=True)

    if dry_run:
        return dsub_cmd

    # 1. submit (async) -> JOBID
    sub = _donau.submit(cfg, payload, netdir, runner, x_all=True, block=False, json=True)
    job_id = sub["job_id"]
    # 2. poll to a terminal state, reporting each transition
    try:
        final = _donau.poll(job_id, runner, on_status=on_status,
                           interval=poll_interval, timeout=poll_timeout, sleep=sleep)
    except _donau.DonauError as e:
        tail = _donau.peek_tail(job_id, runner)
        raise RunCornerError(f"corner run (job {job_id}) poll failed: {e}"
                             + (f"\n--- dpeek tail ---\n{tail}" if tail else "")) from e
    if final == "failed":
        tail = _donau.peek_tail(job_id, runner)
        raise RunCornerError(
            f"corner run FAILED (job {job_id}, engine={engine})."
            + (f"\n--- dpeek tail ---\n{tail}" if tail else
               "\n(no dpeek output available)"))
    # 3. verify the PSF landed (+ sentinel) before handing the dir back to the npz path
    _verify_psf(out_abs, require_simdone=require_simdone, job_id=job_id, runner=runner)
    return str(out_abs)


def _verify_psf(out_dir, require_simdone, job_id, runner):
    """Confirm the engine actually produced output in out_dir. Surfaces the dpeek tail on a
    missing-output failure (a 'done' job that wrote nothing -> usually a netlist/model error
    the sim log explains)."""
    d = pathlib.Path(out_dir)
    if not d.is_dir():
        tail = _donau.peek_tail(job_id, runner)
        raise RunCornerError(
            f"job {job_id} reported done but PSF dir {d} does not exist."
            + (f"\n--- dpeek tail ---\n{tail}" if tail else ""))
    if require_simdone and not (d / SIMDONE).exists():
        tail = _donau.peek_tail(job_id, runner)
        raise RunCornerError(
            f"job {job_id} done but no {SIMDONE} sentinel in {d} -- the ALPS run did not "
            f"complete cleanly (or -ade was not passed)."
            + (f"\n--- dpeek tail ---\n{tail}" if tail else ""))
    # a non-empty PSF dir is the minimal sign of real output (binpsf reads the files later)
    if not any(d.iterdir()):
        tail = _donau.peek_tail(job_id, runner)
        raise RunCornerError(
            f"job {job_id} done but PSF dir {d} is empty (no results written)."
            + (f"\n--- dpeek tail ---\n{tail}" if tail else ""))


def _relpath(target, start):
    """target relative to start if target is under/near start, else the absolute target.
    Mirrors ADE's `-o ../psf` (sibling of netlist/). Falls back to absolute when no relative
    path is sensible (different roots)."""
    import os
    try:
        rel = os.path.relpath(str(target), str(start))
    except ValueError:                     # different drives (Windows only); never on POSIX
        return str(pathlib.Path(target).resolve())
    # Only the sibling/under form (ADE's `-o ../psf`) is safe to hand the node; a deep `../../`
    # chain to an unrelated root is fragile -> pass an absolute path instead.
    if rel.startswith(".." + os.sep + ".."):
        return str(pathlib.Path(target).resolve())
    return rel
