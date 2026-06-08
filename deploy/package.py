"""YELLOW-ZONE packager (Windows, has network) -> offline airgap bundle for the RED zone.

Builds a self-contained tarball the red CentOS7 box installs with NO network. Two modes:

  full         cross-download Linux/glibc-2.17 wheels for the red target, AUDIT their tags
               (reject any > glibc 2.17), freeze requirements.lock, and bundle
               app/ + wheels/ + bootstrap.sh + update.sh + requirements.lock + MANIFEST.json
  incremental  app/ source only (+ update.sh + MANIFEST). NO wheels. Guarded: if the
               requirements hash changed since the last full build, refuse -> demand a full.

    python deploy/package.py full          --out dist/
    python deploy/package.py incremental   --out dist/ --last dist/MANIFEST.full.json

The wheel cross-download targets cp311 / x86_64 / manylinux2014 (+manylinux_2_17). The audit is
the airgap's #1 safety gate (deploy/audit_wheels.py); a failure prints exactly what to downpin.
"""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tarfile
import time

import audit_wheels

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
APP_DIRS = ["harness", "cadence", "gui"]      # the analytic GUI needs only these source dirs
PY_TAG, ABI, ARCH = "311", "cp311", "x86_64"
PLATFORMS = ["manylinux2014_x86_64", "manylinux_2_17_x86_64"]


def _sha256(path):
    h = hashlib.sha256()
    h.update(pathlib.Path(path).read_bytes())
    return h.hexdigest()


def _sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       text=True).strip()
    except Exception:
        return "unknown"


def stage_app(stage):
    """Copy the source dirs the GUI imports into <stage>/app (curated, not the whole repo)."""
    app = stage / "app"
    for d in APP_DIRS:
        src = ROOT / d
        if not src.exists():
            continue
        shutil.copytree(src, app / d, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "work", "o.dat", "*.log"))
    return app


def download_wheels(stage, req):
    """Cross-download the red-target wheels for the pinned requirements."""
    wheels = stage / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "-r", str(req), "--dest", str(wheels),
           "--only-binary=:all:", "--python-version", PY_TAG, "--implementation", "cp",
           "--abi", ABI]
    for p in PLATFORMS:
        cmd += ["--platform", p]
    print("  $ " + " ".join(cmd[2:]))
    subprocess.check_call(cmd)
    return wheels


def freeze_lock(wheels):
    """Build requirements.lock from the exact wheels downloaded (name==version, sorted)."""
    pins = {}
    for w in sorted(pathlib.Path(wheels).glob("*.whl")):
        parts = w.name[:-4].split("-")
        if len(parts) >= 2:
            pins[parts[0].replace("_", "-").lower()] = parts[1]
    return "\n".join(f"{n}=={v}" for n, v in sorted(pins.items())) + "\n"


def build_full(out):
    stage = out / "_stage_full"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    print("[1/6] staging app/ ..."); stage_app(stage)
    print("[2/6] cross-downloading red-target wheels ...")
    wheels = download_wheels(stage, HERE / "requirements-gui.txt")
    print("[3/6] AUDIT wheels for glibc 2.17 ...")
    rows, viol = audit_wheels.audit_dir(wheels, max_glibc=(2, 17), arch=ARCH)
    for r in rows:
        print(f"      {r['name']}  ->  {r['verdict']}")
    if viol:
        print(f"\n*** AUDIT FAIL: {len(viol)} wheel(s) need glibc > 2.17. Downpin in "
              "requirements-gui.txt and re-run. ***")
        for r in viol:
            print(f"      - {r['name']}  {r['verdict']}")
        sys.exit(1)
    print(f"      AUDIT PASS ({len(rows)} wheels, all <= glibc 2.17)")
    print("[4/6] freezing requirements.lock ...")
    lock = freeze_lock(wheels)
    (stage / "requirements.lock").write_text(lock, newline="\n")  # LF: consumed by pip on Linux
    req_hash = _sha256_text(lock)
    print("[5/6] copying installers + MANIFEST ...")
    for f in ("bootstrap.sh", "update.sh"):
        shutil.copy(HERE / f, stage / f)
    shutil.copytree(HERE, stage / "app" / "deploy",      # ship the deploy tools too (audit/update)
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    manifest = dict(
        mode="full", git_sha=_git_sha(), built_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        python=f"cp{PY_TAG}", arch=ARCH, target_glibc="2.17",
        requirements_hash=req_hash,                          # resolved lock (red-box update.sh guard)
        input_req_hash=_sha256(HERE / "requirements-gui.txt"),  # input pins (yellow incremental guard)
        wheels=[w.name for w in sorted(wheels.glob("*.whl"))],
        app_dirs=APP_DIRS,
        checksums={str(p.relative_to(stage)): _sha256(p)
                   for p in sorted(stage.rglob("*")) if p.is_file()})
    (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), newline="\n")
    print("[6/6] taring bundle ...")
    tar = out / "ldo_modeler_full.tar.gz"
    _make_tar(stage, tar)
    (out / "MANIFEST.full.json").write_text(json.dumps(manifest, indent=2), newline="\n")
    (tar.with_suffix(".gz.sha256")).write_text(_sha256(tar) + "  " + tar.name + "\n", newline="\n")  # LF: sha256sum -c on Linux
    print(f"\nDONE -> {tar}  ({tar.stat().st_size/1e6:.1f} MB)\n"
          f"       req-hash {req_hash[:12]}  |  {len(manifest['wheels'])} wheels  |  sha256 sidecar written")
    return tar


def build_incremental(out, last_manifest):
    stage = out / "_stage_incr"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    print("[1/4] staging app/ ..."); stage_app(stage)
    shutil.copytree(HERE, stage / "app" / "deploy",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # GUARD (enforced): an incremental ships NO wheels, so the requirements must be UNCHANGED since
    # the last full build. Compare the current requirements-gui.txt hash to the one recorded then;
    # abort (don't ship a silently-stale-deps bundle) if it moved.
    print("[2/4] enforcing requirements-unchanged vs last full build ...")
    last = json.loads(pathlib.Path(last_manifest).read_text())
    cur_input_hash = _sha256(HERE / "requirements-gui.txt")
    last_input_hash = last.get("input_req_hash")
    if last_input_hash is None:
        print("*** ABORT: last full MANIFEST predates input-hash tracking; rebuild with `package.py "
              "full` so the incremental guard has a baseline. ***"); sys.exit(1)
    if cur_input_hash != last_input_hash:
        print(f"*** ABORT: requirements-gui.txt changed since the last full build "
              f"({last_input_hash[:12]} -> {cur_input_hash[:12]}). Incremental ships no wheels; run "
              "`package.py full` to re-download + re-audit the wheel set. ***"); sys.exit(1)
    print(f"      requirements unchanged (input hash {cur_input_hash[:12]}); code-only update is safe")
    print("[3/4] copying update.sh + MANIFEST ...")
    shutil.copy(HERE / "update.sh", stage / "update.sh")
    manifest = dict(
        mode="incremental", git_sha=_git_sha(),
        built_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        requirements_hash=last["requirements_hash"],     # must match deployed for update.sh to proceed
        based_on_full=last.get("built_utc"), app_dirs=APP_DIRS,
        checksums={str(p.relative_to(stage)): _sha256(p)
                   for p in sorted(stage.rglob("*")) if p.is_file()})
    (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), newline="\n")
    print("[4/4] taring incremental bundle ...")
    tar = out / "ldo_modeler_incremental.tar.gz"
    _make_tar(stage, tar)
    (tar.with_suffix(".gz.sha256")).write_text(_sha256(tar) + "  " + tar.name + "\n", newline="\n")  # LF: sha256sum -c on Linux
    print(f"\nDONE -> {tar}  ({tar.stat().st_size/1e3:.0f} KB)  (no wheels; venv reused on red box)\n"
          f"       carries req-hash {manifest['requirements_hash'][:12]} -- update.sh aborts if the "
          "deployed venv's hash differs.")
    return tar


def freeze_lock_from_pins(req):
    return pathlib.Path(req).read_text()


def _make_tar(stage, tar):
    with tarfile.open(tar, "w:gz") as t:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                t.add(p, arcname=str(p.relative_to(stage)))


def main():
    ap = argparse.ArgumentParser(description="Package the offline GUI bundle for the red zone")
    ap.add_argument("mode", choices=["full", "incremental"])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--last", default=None, help="incremental: path to the last full MANIFEST.json")
    a = ap.parse_args()
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.mode == "full":
        build_full(out)
    else:
        last = a.last or str(out / "MANIFEST.full.json")
        if not pathlib.Path(last).exists():
            print(f"incremental needs a prior full MANIFEST ({last}); run `package.py full` first.")
            sys.exit(2)
        build_incremental(out, last)


if __name__ == "__main__":
    main()
