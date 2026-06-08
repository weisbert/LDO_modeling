"""glibc wheel-tag auditor -- the airgap install's #1 safety gate.

The red zone is CentOS7-class: glibc 2.17 (manylinux2014 / manylinux_2_17 baseline). On the
Windows yellow zone `pip download` will happily fetch newer wheels (manylinux_2_28/_2_31/...),
which then die on the red box with `GLIBC_2.28 not found`. This script inspects every wheel in a
directory, computes the MINIMUM glibc it can run on (across its platform tags), and FAILS the
build if any wheel needs glibc newer than the target (default 2.17). musllinux and bare
`linux_*` wheels are rejected (wrong libc / not portable); `py3-none-any` is always fine.

    python audit_wheels.py wheels/                  # audit, exit 1 if any violation
    python audit_wheels.py wheels/ --max-glibc 2.17 --arch x86_64
"""
import argparse
import pathlib
import re
import sys

# named manylinux profiles -> (glibc_major, glibc_minor)
NAMED = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
_MX = re.compile(r"^manylinux_(\d+)_(\d+)_(.+)$")        # manylinux_2_28_x86_64
_MX_NAMED = re.compile(r"^(manylinux1|manylinux2010|manylinux2014)_(.+)$")
_MUSL = re.compile(r"^musllinux_(\d+)_(\d+)_(.+)$")
INF = (999, 999)


def _platform_glibc(tag, arch):
    """One platform sub-tag -> (glibc_required, ok_arch). Returns the glibc (major,minor)
    needed to run this sub-tag, or INF if it can't run on a glibc/arch target."""
    if tag == "any":
        return (0, 0), True                              # pure python
    m = _MX.match(tag)
    if m:
        a = m.group(3)
        return (int(m.group(1)), int(m.group(2))), (a == arch)
    m = _MX_NAMED.match(tag)
    if m:
        return NAMED[m.group(1)], (m.group(2) == arch)
    if _MUSL.match(tag):
        return INF, False                                # musl libc, not glibc
    if tag.startswith("linux_"):
        return INF, False                                # bare linux: built-locally, not portable
    # win_*, macosx_*, manylinux unknown, etc. -> not a glibc-linux target
    return INF, False


def audit_wheel(path, arch="x86_64"):
    """Return dict(name, min_glibc=(M,m)|None, tags=[...], ok_for_glibc). min_glibc=None means
    no installable-on-this-arch glibc tag was found (pure-python -> (0,0))."""
    stem = pathlib.Path(path).name[:-4] if str(path).endswith(".whl") else pathlib.Path(path).name
    parts = stem.split("-")
    plat = parts[-1] if len(parts) >= 5 else ""
    subtags = plat.split(".") if plat else []
    best = None                                          # minimal glibc across arch-compatible tags
    for t in subtags:
        g, ok_arch = _platform_glibc(t, arch)
        if g == (0, 0):                                  # pure python any
            best = (0, 0); break
        if ok_arch and g != INF:
            best = g if best is None else min(best, g)
    return dict(name=pathlib.Path(path).name, tags=subtags, min_glibc=best)


def audit_dir(wheels_dir, max_glibc=(2, 17), arch="x86_64"):
    wheels = sorted(pathlib.Path(wheels_dir).glob("*.whl"))
    rows, violations = [], []
    for w in wheels:
        r = audit_wheel(w, arch=arch)
        g = r["min_glibc"]
        if g is None:
            r["verdict"] = "REJECT (no glibc/{} wheel tag)".format(arch)
            violations.append(r)
        elif g > max_glibc:
            r["verdict"] = f"REJECT (needs glibc {g[0]}.{g[1]} > {max_glibc[0]}.{max_glibc[1]})"
            violations.append(r)
        else:
            r["verdict"] = ("OK (pure-python)" if g == (0, 0)
                            else f"OK (glibc {g[0]}.{g[1]})")
        rows.append(r)
    return rows, violations


def main():
    ap = argparse.ArgumentParser(description="Audit wheels for glibc-2.17 (manylinux2014) compat")
    ap.add_argument("wheels_dir")
    ap.add_argument("--max-glibc", default="2.17")
    ap.add_argument("--arch", default="x86_64")
    a = ap.parse_args()
    mj, mn = (int(x) for x in a.max_glibc.split("."))
    rows, viol = audit_dir(a.wheels_dir, max_glibc=(mj, mn), arch=a.arch)
    if not rows:
        print(f"no wheels found in {a.wheels_dir}"); sys.exit(2)
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        print(f"  {r['name']:<{width}}  {r['verdict']}")
    print(f"\n{len(rows)} wheels, {len(viol)} violation(s); target glibc {a.max_glibc}/{a.arch}")
    if viol:
        print("AUDIT FAIL -> downpin these (last version with a manylinux_2_17/2014 wheel):")
        for r in viol:
            print(f"   - {r['name']}  ({r['verdict']})")
        sys.exit(1)
    print("AUDIT PASS -> all wheels installable on the red zone.")
    sys.exit(0)


if __name__ == "__main__":
    main()
