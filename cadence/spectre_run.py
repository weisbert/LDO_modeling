"""Thin Spectre subprocess driver for the Target-B bring-up.

Mirrors harness/ng.py but talks to Spectre 18.1 instead of ngspice. Two hard-won
rules baked in (see memory spectre-va-compile-64bit):
  * always `spectre -64`  -> else ahdlcmi compiles -m32 and dies on gnu/stubs-32.h
  * `-format psfascii`    -> parsed by cadence/psf.py

The env mirrors the live Virtuoso IC618 process (cadence/env.sh): SPECTRE181 on
PATH ahead of IC618, CDS_LIC_FILE pointing at the node-locked license.
"""
import os
import pathlib
import shutil
import subprocess
import psf

ROOT = pathlib.Path(__file__).resolve().parents[1]
CADENCE = ROOT / "cadence"
WORK = CADENCE / os.environ.get("LDO_SPECTRE_WORK", "work")

CDSHOME = "/home/yusheng/Program/eda/cadence/IC618"
SPECTRE_HOME = "/home/yusheng/Program/eda/cadence/SPECTRE181"
LICENSE = "/home/yusheng/Program/eda/cadence/license/license.dat"


def _env():
    e = dict(os.environ)
    e["CDSHOME"] = e["CDS_INST_DIR"] = e["CDS_ROOT"] = CDSHOME
    e["CDS_LIC_FILE"] = LICENSE
    e["SPECTRE_HOME"] = SPECTRE_HOME
    pre = [f"{SPECTRE_HOME}/bin", f"{SPECTRE_HOME}/tools/bin",
           f"{CDSHOME}/bin", f"{CDSHOME}/tools/bin/64bit", f"{CDSHOME}/tools/bin",
           f"{CDSHOME}/share/oa/bin", f"{CDSHOME}/tools/dfII/bin"]
    e["PATH"] = ":".join(pre) + ":" + e.get("PATH", "")
    return e


def run(scs_text, tag, aux=(), timeout=300):
    """Write `scs_text` to <WORK>/<tag>/input.scs, run `spectre -64`, and parse
    every PSF analysis output. Returns {analysis_name: parsed_dict}. Raises with
    the spectre log tail on a non-zero/`fatal error` exit.

    aux = iterable of (src_path, dest_basename) copied into the run dir before
    launch (e.g. the `$table_model` dropout .tbl, whose name is hardcoded in the VA)."""
    wd = WORK / tag
    wd.mkdir(parents=True, exist_ok=True)
    for src, dst in aux:
        shutil.copyfile(src, wd / dst)
    (wd / "input.scs").write_text(scs_text)
    raw = wd / "raw"
    if raw.exists():
        for f in raw.iterdir():
            if f.is_file():
                f.unlink()
    r = subprocess.run(
        ["spectre", "-64", "input.scs", "-format", "psfascii", "-raw", "raw",
         "+log", "spectre.log", "-E"],
        cwd=str(wd), env=_env(), capture_output=True, text=True, timeout=timeout)
    log = (wd / "spectre.log").read_text() if (wd / "spectre.log").exists() else r.stdout
    if "fatal error" in log or r.returncode != 0:
        raise RuntimeError(f"[{tag}] spectre failed (rc={r.returncode}):\n" + log[-2500:])
    out = {}
    if raw.exists():
        for f in sorted(raw.iterdir()):
            if not f.is_file() or f.name in ("logFile",):
                continue
            name = f.name.split(".")[0]            # acZ.ac -> acZ ; trn.tran.tran -> trn
            try:
                out[name] = psf.read_psf(f)
            except Exception:
                pass
    out["_log"] = log
    return out
