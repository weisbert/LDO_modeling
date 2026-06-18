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


# --------------------------------------------------------------------------- CLI
# Two subcommands, both pure-CLI Donau+ALPS, no skillbridge / ADE:
#   run-corner  (default)  ONE prepared netlist -> submit/poll/verify -> the PSF dir. The
#               documented one-corner SMOKE TEST of the cluster pipeline. It does NOT generate
#               per-group one-hot netlists; it runs the netlist you point it at, end to end.
#   run-sweep              MANIFEST-DRIVEN per-group sweep: one Donau job per measurement GROUP
#               (run.groups(m)), each netlisting one acm one-hot from ONE base input.scs via the
#               OFFLINE group netlister (cluster.netlist_augment) -- no Virtuoso. Reuses the full
#               9-step orchestrator (insitu.pmu_corner.run_pmu_corner, manifest=); lands the npz.
def _add_donau_args(ap):
    """The shared Donau submit-tuple + poll knobs (both subcommands)."""
    ap.add_argument("--account", default="ug_rfic.rfSClass", help="Donau -A account.")
    ap.add_argument("--queue", default="short", help="Donau -q queue.")
    ap.add_argument("--cpu", type=int, default=8, help="Donau cpu= (== alps -mt).")
    ap.add_argument("--mem", type=int, default=8000, help="Donau mem= (MB).")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--poll-timeout", type=float, default=10800.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="assemble + print the dsub command(s), submit NOTHING.")


def _build_argparser():
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m cluster",
        description="Pure-CLI Donau+ALPS corner driver (no skillbridge / ADE needed). Two "
                    "subcommands: 'run-corner' (one prepared netlist, the smoke test) and "
                    "'run-sweep' (the manifest-driven per-group sweep via the offline netlister).")
    sub = ap.add_subparsers(dest="cmd")

    # --- run-corner: the existing one-corner smoke (kept reachable; also the DEFAULT) -------
    rcp = sub.add_parser("run-corner",
                         help="run ONE prepared netlist dir: submit -> poll -> verify -> PSF dir")
    rcp.add_argument("--netlistdir", required=True,
                     help="dir holding input.scs (read as the node cwd via dsub -EP).")
    rcp.add_argument("--out", dest="out_psf_dir", required=True,
                     help="where the PSF lands. Point at a SCRATCH dir in your workarea -- NOT the "
                          "maestro results tree (we never write the designer/maestro spine).")
    rcp.add_argument("--pdk", dest="pdk_model_dir", default=None,
                     help="PDK model ROOT *directory* (the engine subtree {alps,spectre} is "
                          "appended). e.g. $MODEL_ROOT  ->  -I $MODEL_ROOT/alps. Omit if the "
                          "netlist's own include lines are self-contained.")
    rcp.add_argument("--ahdllibdir", default=None,
                     help="compiled AHDL/VA model DB (-ahdllibdir). Omit -> the sim auto-compiles "
                          "the Verilog-A from the netlist's ahdl_include lines.")
    rcp.add_argument("--engine", default="alps", choices=("alps", "spectre"))
    rcp.add_argument("--input-scs", default=INPUT_SCS, help=f"netlist filename (default {INPUT_SCS}).")
    _add_donau_args(rcp)

    # --- run-sweep: the manifest-driven offline per-group sweep -----------------------------
    swp = sub.add_parser("run-sweep",
                         help="manifest-driven per-group sweep via the OFFLINE netlister (no Virtuoso)")
    swp.add_argument("--manifest", required=True,
                     help="manifest path or bare name (resolved via insitu.manifest.load). Its "
                          "nets MUST be resolved (no '<net:...>' placeholders).")
    swp.add_argument("--base-netlist", required=True,
                     help="dir holding the base maestro input.scs (the designer's .tran TB); the "
                          "offline netlister rewrites it into one ac/noise netlist per group.")
    swp.add_argument("--pdk", dest="pdk_model_dir", default=None,
                     help="PDK model ROOT *directory* ($MODEL_ROOT); the engine subtree {alps,"
                          "spectre} is appended to -I.")
    swp.add_argument("--ahdllibdir", default=None,
                     help="compiled AHDL/VA model DB (-ahdllibdir). Omit -> the sim auto-compiles.")
    swp.add_argument("--engine", default="alps", choices=("alps", "spectre"))
    swp.add_argument("--work-root", default=None,
                     help="$WORK_ROOT (else env WORK_ROOT / ~/ldo_workarea); the per-group netlists "
                          "+ PSF + npz land under <work-root>/ldo_modeling/<Lib>__<Cell>/<corner>.")
    swp.add_argument("--corner", default=None, help="process-corner label (else the manifest's).")
    _add_donau_args(swp)
    return ap


_SUBCOMMANDS = ("run-corner", "run-sweep")


def main(argv=None):
    import sys
    raw = list(argv) if argv is not None else sys.argv[1:]
    # default subcommand: run-corner. Inject it when the first non-help arg is NOT a known
    # subcommand, so the existing `python -m cluster --netlistdir ...` smoke keeps working
    # unchanged (argparse subparsers need the subcommand token to lead).
    if raw and raw[0] not in _SUBCOMMANDS and raw[0] not in ("-h", "--help"):
        raw = ["run-corner", *raw]
    args = _build_argparser().parse_args(raw)
    if getattr(args, "cmd", None) is None:                 # bare `python -m cluster`
        _build_argparser().parse_args(["-h"])
        return 2
    if args.cmd == "run-sweep":
        return _main_sweep(args)
    return _main_run_corner(args)


def _main_run_corner(args):
    import sys
    import shlex
    cfg = DonauCfg(account=args.account, queue=args.queue,
                   resource=f"cpu={args.cpu};mem={args.mem}")
    common = dict(engine=args.engine, donau=cfg, input_scs=args.input_scs)
    if args.dry_run:
        cmd = run_corner(args.netlistdir, args.pdk_model_dir, args.ahdllibdir,
                         args.out_psf_dir, dry_run=True, **common)
        print(shlex.join(str(x) for x in cmd))
        return 0

    def _on_status(state, raw):
        print(f"[donau] {state}", flush=True)

    try:
        psf = run_corner(args.netlistdir, args.pdk_model_dir, args.ahdllibdir, args.out_psf_dir,
                         on_status=_on_status, poll_interval=args.poll_interval,
                         poll_timeout=args.poll_timeout, **common)
    except RunCornerError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print(f"OK -- PSF dir: {psf}")
    return 0


def _main_sweep(args):
    """The run-sweep subcommand: load the manifest, build the OFFLINE group netlister over the
    base input.scs, and drive the FULL 9-step orchestrator (manifest-driven). On --dry-run print
    the per-group dsub commands; on a real run land the npz. Reuses pmu_corner.run_pmu_corner --
    no second orchestrator."""
    import sys
    import shlex
    # imports are function-local so the cluster package never hard-depends on insitu at load
    # time (insitu.pmu_corner imports cluster -> a module-level import here would be circular).
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # .../cadence
    from insitu import manifest as _manifest
    from insitu import pmu_corner as _pc
    from . import netlist_augment as _na

    m = _manifest.load(args.manifest)
    corner = args.corner or m.get("corner") or (m.get("corners", {}).get("fallback") or ["nom"])[0]
    cfg = DonauCfg(account=args.account, queue=args.queue,
                   resource=f"cpu={args.cpu};mem={args.mem}")

    # the per-group netlists land under the workarea corner dir's netlist/ subtree (NOT the
    # designer spine). corner_dir uses a minimal gui synthesized from the manifest.
    gui = _pc._gui_from_manifest(m)
    base, dirs = _pc.corner_dir(args.work_root, gui, corner)
    out_base = dirs["netlist"]

    runner = None if args.dry_run else _donau.SubprocessRunner()

    def _on_status(state, raw):
        print(f"  [donau] {state}", flush=True)

    try:
        # building the offline factory validates resolved nets + the supply source up front;
        # surface its NetlistAugmentError cleanly rather than as a traceback.
        gnl = _na.make_offline_group_netlister(args.base_netlist, m, out_base)
        res = _pc.run_pmu_corner(
            manifest=m, work_root=args.work_root, corner=corner, engine=args.engine,
            netlistdir=args.base_netlist, ahdllibdir=args.ahdllibdir,
            pdk_model_dir=args.pdk_model_dir, group_netlister=gnl, donau=cfg,
            runner=runner, on_status=_on_status, dry_run=args.dry_run,
            poll_interval=args.poll_interval, poll_timeout=args.poll_timeout)
    except (RunCornerError, _na.NetlistAugmentError, _manifest.ManifestError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1

    cmds = res.get("dsub_cmds") or []
    if args.dry_run:
        print(f"\n=== run-sweep DRY: {len(cmds)} Donau job(s) (one per measurement GROUP) ===")
        for c in cmds:
            print(shlex.join(str(x) for x in c))
        return 0
    print(f"\n=== run-sweep: {len(cmds)} group(s) ran ===")
    print(f"  psf_dir : {res.get('psf_dir')}")
    print(f"  npz     : {res.get('npz')}")
    print(f"  va      : {res.get('va')}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
