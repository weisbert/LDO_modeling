"""Verilog-A -> OSDI toolchain wrapper (compile a .va to a .osdi ngspice can load).

Mirrors ng.py's philosophy: locate the toolchain via env vars first, then known repo/
machine locations, so the modeling tool itself stays toolchain-agnostic. Used by
validate_trans_va.py to PROVE the emitted stimulus .va actually compiles and runs (the
hard "don't just write the .va" constraint), and reusable for the deferred Target-B
.va HB-check.

Toolchain pieces (Windows / MSVC-ABI target, which is OpenVAF's only Windows target):
  - openvaf.exe                     : the VA front-end + codegen ($OPENVAF | tools/openvaf | PATH)
  - an MSVC-compatible `link.exe`    : OpenVAF shells out to `link`; conda-forge `lld`'s
                                       lld-link.exe works when invoked as `link` (LLD picks the
                                       COFF flavor from a `link` suffix). Must sit beside its
                                       LLVM DLLs ($OSDI_LINKER_DIR | tools/openvaf/bin | a conda env).
  - MSVC CRT + Windows SDK import libs on $LIB (msvcrt/kernel32/ucrt ...). xwin splats these
                                       from Microsoft's CDN with no Visual Studio install
                                       ($OSDI_LIB | tools/xwin/splat).
On Linux the OpenVAF build is self-contained (bundles its own linker) -> link dir / LIB are
simply omitted (return None), and `openvaf foo.va` just works.

Env overrides (portable; set any of these to point at your own toolchain):
  OPENVAF, OSDI_LINKER_DIR, OSDI_LIB
"""
import os
import glob
import shutil
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
_IS_WIN = os.name == "nt"
_EXE = ".exe" if _IS_WIN else ""


def find_openvaf():
    """Locate the openvaf executable. Raises FileNotFoundError if none is found."""
    p = os.environ.get("OPENVAF")
    if p and pathlib.Path(p).exists():
        return str(p)
    cand = ROOT / "tools" / "openvaf" / f"openvaf{_EXE}"
    if cand.exists():
        return str(cand)
    w = shutil.which("openvaf")
    if w:
        return w
    raise FileNotFoundError(
        "openvaf not found. Set $OPENVAF, drop it in tools/openvaf/, or put it on PATH.")


def _ensure_link_exe(dirpath):
    """Return dirpath if it contains an MSVC-style `link.exe`; if it only has lld-link.exe,
    copy it to link.exe (LLD dispatches the COFF/WinLink flavor from the `link` suffix and
    finds its DLLs in its own directory). Returns None if neither is present."""
    d = pathlib.Path(dirpath)
    if (d / "link.exe").exists():
        return str(d)
    lld = d / "lld-link.exe"
    if lld.exists():
        try:
            shutil.copy2(lld, d / "link.exe")
            return str(d)
        except OSError:
            return None
    return None


def find_linker_dir():
    """Directory holding an MSVC-compatible `link.exe` (+ its DLLs), or None on platforms
    where OpenVAF links itself (Linux) / when not needed."""
    if not _IS_WIN:
        return None
    d = os.environ.get("OSDI_LINKER_DIR")
    if d:
        got = _ensure_link_exe(d)
        if got:
            return got
    repo_bin = ROOT / "tools" / "openvaf" / "bin"
    if repo_bin.exists():
        got = _ensure_link_exe(repo_bin)
        if got:
            return got
    # conda envs (the conda-forge `lld` package ships lld-link.exe in <env>/Library/bin)
    bases = [os.environ.get("CONDA_PREFIX", ""),
             str(pathlib.Path.home() / "anaconda3"),
             str(pathlib.Path.home() / "miniconda3"),
             "C:\\ProgramData\\anaconda3", "C:\\ProgramData\\miniconda3"]
    for base in bases:
        if not base:
            continue
        for hit in glob.glob(str(pathlib.Path(base) / "envs" / "*" / "Library" / "bin" / "lld-link.exe")):
            got = _ensure_link_exe(pathlib.Path(hit).parent)
            if got:
                return got
        # the env itself may BE a CONDA_PREFIX with lld in Library/bin
        for hit in glob.glob(str(pathlib.Path(base) / "Library" / "bin" / "lld-link.exe")):
            got = _ensure_link_exe(pathlib.Path(hit).parent)
            if got:
                return got
    return None


def find_lib_dirs():
    """os.pathsep-joined MSVC CRT + Windows SDK import-lib dirs for $LIB, or None when not
    needed (non-Windows, or already set in the environment by a VS dev prompt)."""
    if not _IS_WIN:
        return None
    v = os.environ.get("OSDI_LIB")
    if v:
        return v
    sp = ROOT / "tools" / "xwin" / "splat"
    parts = [sp / "crt" / "lib" / "x86_64",
             sp / "sdk" / "lib" / "um" / "x86_64",
             sp / "sdk" / "lib" / "ucrt" / "x86_64"]
    parts = [str(p) for p in parts if p.exists()]
    return os.pathsep.join(parts) if parts else None


def toolchain_status():
    """Diagnostic dict: which pieces are resolvable (for the report / a fast preflight)."""
    try:
        ov = find_openvaf()
    except FileNotFoundError:
        ov = None
    return dict(openvaf=ov, linker_dir=find_linker_dir(), lib=find_lib_dirs(), is_win=_IS_WIN)


def compile_va(va_path, osdi_out=None, timeout=300):
    """Compile a Verilog-A file to a .osdi shared object ngspice can `pre_osdi`-load.
    Returns the .osdi path. Raises RuntimeError with the openvaf output on failure."""
    va_path = pathlib.Path(va_path).resolve()       # absolute -> works regardless of caller cwd
    osdi_out = pathlib.Path(osdi_out).resolve() if osdi_out else va_path.with_suffix(".osdi")
    env = os.environ.copy()
    ld = find_linker_dir()
    if ld:
        env["PATH"] = ld + os.pathsep + env.get("PATH", "")
    lib = find_lib_dirs()
    if lib:
        env["LIB"] = lib + (os.pathsep + env["LIB"] if env.get("LIB") else "")
    if osdi_out.exists():
        try:
            osdi_out.unlink()
        except OSError:
            pass
    cmd = [find_openvaf(), str(va_path), "-o", str(osdi_out)]
    r = subprocess.run(cmd, env=env, cwd=str(va_path.parent),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace",   # zh-CN Windows: don't GBK-crash on output
                       timeout=timeout)
    if not (osdi_out.exists() and r.returncode == 0):
        raise RuntimeError(
            f"openvaf failed for {va_path.name} (rc={r.returncode}).\n"
            f"  cmd : {' '.join(cmd)}\n"
            f"  link: {ld}\n  LIB : {lib}\n"
            f"STDOUT:\n{(r.stdout or '')[-1800:]}\nSTDERR:\n{(r.stderr or '')[-1800:]}")
    return str(osdi_out)


if __name__ == "__main__":
    import json
    print("toolchain:", json.dumps(toolchain_status(), indent=2))
