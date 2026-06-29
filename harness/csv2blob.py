#!/usr/bin/env python3
"""csv2blob: Cadence transient CSV  <->  one small pasteable [ITRACE1] block.

Purpose: get a measured load current OFF a no-file-transfer box and INTO local
current-replay by COPY-PASTE. Local replay of the LDO .va needs the current at
~1-2 GHz fidelity -- the rail's V-dip is driven by ~GHz current features, so
averaging the switching out KILLS the dip (proven locally: a 2 ns boxcar on the
current took the replayed V from 2 mV to 10 mV error). That fidelity needs ~800
shape-preserving (RDP) points; plain text is ~20 KB, but gzip+base64 shrinks it to
~5 KB -- the same size as the report's [MPD1-GZ] digest, small enough to paste.

Validated end-to-end (csv -> blob -> decode -> Spectre replay): 800-pt block
reproduces the full-resolution replayed VDD to 0.7 mV RMS.

Pure stdlib (no numpy) -- scp/`bash apply` to the box, run with system python3.

ENCODE (on the box):
  python3 csv2blob.py iload.csv                      # all cols, 800 pts -> iload.blob.txt
  python3 csv2blob.py iload.csv --cols 0,1           # time + current only (smallest)
  python3 csv2blob.py iload.csv --cols 0,1,2 --label pll
INPUT : whitespace/comma CSV. col0=time; col1..=signal(s). Non-numeric header/units
        lines skipped. ocnPrint / ViVA "Export CSV" works as-is.
OUTPUT: <input>.blob.txt + stdout: a human summary + [ITRACE1]..[ITRACE1-END] block.
        Paste the WHOLE block back.

DECODE (anywhere, to verify / reuse):
  python3 csv2blob.py --decode blob.txt -o trace.txt     # -> plain "t s1 s2.." table
"""
import argparse, base64, gzip, sys


# --------------------------------------------------------------------- parse/decimate
def _parse(path, cols):
    rows, ncol = [], None
    for ln in open(path):
        s = ln.strip()
        if not s:
            continue
        parts = [p for p in s.replace(",", " ").replace(";", " ").split() if p]
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            continue
        if len(vals) < 2:
            continue
        if ncol is None:
            ncol = len(vals)
        if len(vals) != ncol:
            continue
        rows.append(vals)
    if not rows:
        sys.exit(f"csv2blob: no numeric rows in {path}")
    if cols:
        idx = [int(c) for c in cols.split(",")]
        rows = [[r[i] for i in idx] for r in rows]
    return rows


def _rdp_keep(xs, ys, eps):
    n = len(xs)
    if n <= 2:
        return set(range(n))
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        x0, y0, x1, y1 = xs[i0], ys[i0], xs[i1], ys[i1]
        dx = (x1 - x0) or 1e-30
        dmax, imax = -1.0, -1
        for i in range(i0 + 1, i1):
            d = abs(ys[i] - (y0 + (y1 - y0) * (xs[i] - x0) / dx))
            if d > dmax:
                dmax, imax = d, i
        if dmax > eps and imax > 0:
            keep[imax] = True
            stack.append((i0, imax)); stack.append((imax, i1))
    return {i for i in range(n) if keep[i]}


def _norm(a):
    lo, hi = min(a), max(a)
    return [(x - lo) / ((hi - lo) or 1.0) for x in a]


def _prethin(t, sigs, tol):
    n = len(t)
    thr = [0.3 * tol * ((max(s) - min(s)) or 1.0) for s in sigs]
    keep, last = [0], 0
    for i in range(1, n - 1):
        if any(abs(sigs[j][i] - sigs[j][last]) > thr[j] for j in range(len(sigs))):
            if i - 1 > keep[-1]:
                keep.append(i - 1)
            keep.append(i); last = i
    keep.append(n - 1)
    return keep


def decimate(rows, max_points, tol=1e-3):
    """Shape-preserving (RDP + forced global extrema, union across signals). Proven the
    RIGHT method for the GHz-switching current: keeps the dip the V response depends on."""
    t = [r[0] for r in rows]
    sigs = [[r[c] for r in rows] for c in range(1, len(rows[0]))]
    cand = _prethin(t, sigs, tol)
    ct = [t[i] for i in cand]
    csigs = [[s[i] for i in cand] for s in sigs]
    tn = _norm(ct); sn = [_norm(s) for s in csigs]
    forced = {0, len(cand) - 1}
    for s in csigs:
        forced.add(s.index(min(s))); forced.add(s.index(max(s)))

    def kept_for(eps):
        keep = set(forced)
        for y in sn:
            keep |= _rdp_keep(tn, y, eps)
        return keep
    lo, hi = max(tol, 1e-9), 1.0
    keep = kept_for(lo)
    if len(keep) > max_points:
        for _ in range(40):
            mid = (lo * hi) ** 0.5
            k = kept_for(mid)
            if len(k) > max_points:
                lo = mid
            else:
                hi = mid; keep = k
    return sorted(cand[p] for p in keep), t, sigs


# --------------------------------------------------------------------- encode/decode
MARK, END = "[ITRACE1]", "[ITRACE1-END]"


def encode(rows, idx, ncol):
    body = "\n".join(" ".join(f"{rows[i][c]:.6e}" for c in range(ncol)) for i in idx)
    return base64.b64encode(gzip.compress(body.encode(), 9)).decode()


def decode(text):
    """[ITRACE1] block (or whole report) -> list of rows [[t, s1, ...], ...]."""
    lines = text.splitlines()
    i0 = next(i for i, l in enumerate(lines) if l.strip() == MARK)
    i1 = next(i for i, l in enumerate(lines) if l.strip() == END)
    blob = "".join(l.strip() for l in lines[i0 + 1:i1])
    body = gzip.decompress(base64.b64decode(blob)).decode()
    return [[float(x) for x in ln.split()] for ln in body.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--max-points", type=int, default=800,
                    help="shape-preserving point budget (default 800 -> ~5KB, <1mV replay)")
    ap.add_argument("--cols", default="", help="column indices to keep, e.g. 0,1 (time,current)")
    ap.add_argument("--label", default="", help="label stored in the block header")
    ap.add_argument("--decode", action="store_true", help="DECODE a .blob.txt back to a table")
    ap.add_argument("-o", "--out", default="", help="output path (decode mode)")
    a = ap.parse_args()

    if a.decode:
        rows = decode(open(a.input).read())
        out = "\n".join(" ".join(f"{v:.6e}" for v in r) for r in rows) + "\n"
        if a.out:
            open(a.out, "w").write(out); sys.stderr.write(f"# wrote {a.out} ({len(rows)} rows)\n")
        else:
            sys.stdout.write(out)
        return

    rows = _parse(a.input, a.cols)
    idx, t, sigs = decimate(rows, a.max_points)
    ncol = 1 + len(sigs)
    blob = encode(rows, idx, ncol)
    lbl = a.label or a.input
    summ = [f"# {MARK} {lbl}  {len(rows)}->{len(idx)} rows  {ncol} cols  "
            f"t {t[0]:.4g}..{t[-1]:.4g}s  ({len(blob)/1024:.1f}KB blob)"]
    for j, s in enumerate(sigs):
        imn = s.index(min(s))
        summ.append(f"#   col{j+1}: start={s[0]:.6g} end={s[-1]:.6g} "
                    f"min={min(s):.6g}@{t[imn]:.4g}s max={max(s):.6g}")
    wrapped = [blob[i:i + 120] for i in range(0, len(blob), 120)]
    out = "\n".join(summ + [MARK] + wrapped + [END]) + "\n"
    fn = a.input.rsplit(".", 1)[0] + ".blob.txt"
    open(fn, "w").write(out)
    sys.stderr.write(f"# wrote {fn}\n")
    print(out)


if __name__ == "__main__":
    main()
