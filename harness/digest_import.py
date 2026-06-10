"""Rebuild a fit-ready reference npz from a pasted [7] GT DIGEST block.

The air-gap return channel: the red-zone report's section [7] carries log-resampled
GT curves as text. This parser turns that paste back into results/ref/<name>.npz so
the real part can be fitted/iterated LOCALLY (no npz crosses the gap).

Digest columns: f[Hz], |Z|[ohm], Zph[deg], PSRRatt[dB], Hph[deg], Sv[V/rtHz]
('-' = quantity not swept at that frequency). dc_loadreg is synthesized from the
per-corner LF Re(Zout) (good enough for fitting; the digest has no DC sweeps).

Usage:
    python harness/digest_import.py results/ref/myldo_digest.txt [--name myldo_digest]
                                    [--vref 1.05]
"""
import argparse
import pathlib
import re
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
REFDIR = ROOT / "results" / "ref"


def parse_digest(text):
    """-> dict corner -> ndarray[N,6] (nan for '-'), in file order."""
    corners = {}
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"#\s*corner\s+(\S+)", line)
        if m:
            cur = m.group(1)
            corners[cur] = []
            continue
        if not line or line.startswith("#") or cur is None:
            continue
        toks = [t.strip() for t in line.split(",")]
        if len(toks) < 6:
            continue
        try:
            row = [float("nan") if t == "-" else float(t) for t in toks[:6]]
        except ValueError:
            continue
        corners[cur].append(row)
    return {il: np.array(rows, float) for il, rows in corners.items() if rows}


def build_ref(corners, name, vref=1.05):
    loads = list(corners.keys())
    ref = {"loads": np.array(loads),
           "meta_cout": np.array(np.nan), "meta_esr": np.array(np.nan),
           "spur_F": np.array([]), "spur_twin0": np.array(0.0),
           "spur_binhz": np.array(15625.0)}
    rdc = {}
    for il, a in corners.items():
        f = a[:, 0]
        Z = a[:, 1] * np.exp(1j * np.radians(a[:, 2]))
        ref[f"z_{il}"] = np.c_[f, Z.real, Z.imag]
        ok = np.isfinite(a[:, 3]) & np.isfinite(a[:, 4])
        H = 10.0 ** (-a[ok, 3] / 20.0) * np.exp(1j * np.radians(a[ok, 4]))
        ref[f"p_{il}"] = np.c_[f[ok], H.real, H.imag]
        nk = np.isfinite(a[:, 5])
        ref[f"noise_{il}"] = np.c_[f[nk], a[nk, 5]]
        rdc[il] = float(Z.real[0])              # LF Re(Zout) ~ DC output resistance
    # synthesized DC curves (the digest carries no DC sweeps): linear load-reg from
    # the mean LF Re(Z); flat line-reg; dropout = load-reg extended (placeholder)
    rmean = float(np.mean(list(rdc.values())))
    imax = max(float(il.replace("u", "e-6").replace("m", "e-3")) for il in loads)
    idc = np.linspace(imax / 400, 4 * imax, 80)
    ref["dc_loadreg"] = np.c_[idc, vref - rmean * idc]
    vdc = np.linspace(vref + 0.05, vref + 0.4, 40)
    ref["dc_linereg"] = np.c_[vdc, vref + 0 * vdc]
    iddc = np.linspace(imax / 400, 10 * imax, 80)
    ref["dc_dropout"] = np.c_[iddc, np.maximum(vref - rmean * iddc, 0.2 * vref)]
    out = REFDIR / f"{name}.npz"
    np.savez(out, **ref)
    return out, loads


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("digest", help="text file containing the pasted [7] GT DIGEST block")
    ap.add_argument("--name", default=None, help="output ref name (default: file stem)")
    ap.add_argument("--vref", type=float, default=1.05)
    a = ap.parse_args()
    p = pathlib.Path(a.digest)
    corners = parse_digest(p.read_text(encoding="utf-8"))
    if not corners:
        sys.exit("no '# corner' blocks parsed -- is this a [7] GT DIGEST paste?")
    name = a.name or p.stem
    out, loads = build_ref(corners, name, vref=a.vref)
    print(f"wrote {out}   corners={loads} "
          f"({', '.join(str(len(corners[il])) + 'pts' for il in loads)})")


if __name__ == "__main__":
    main()
