#!/usr/bin/env python3
"""Shape-preserving decimation of a Cadence transient (itrans) export so it fits in a paste.

The itrans file has thousands of points; uniform sampling ALIASES sharp dips (a few evenly-spaced
points jump around and hide the real shape). This keeps the points that DEFINE the curve -- the dip
bottoms, the step edges, the settling tails -- via Ramer-Douglas-Peucker simplification plus forced
global extrema, and drops the long flat runs. The result is a small table that reconstructs the
waveform faithfully.

Pure stdlib (no numpy) so it runs anywhere -- scp it to the box and run with the system python.

INPUT  : a whitespace- OR comma-separated table. Column 0 = time; columns 1.. = signal(s)
         (Vout, and ideally the load current iload). Header/comment lines (anything not starting
         with a number) are skipped. Cadence ocnPrint / ViVA "Export CSV" output works as-is.
OUTPUT : <input>.reduced.txt (same columns, far fewer rows) AND a paste-ready block on stdout,
         prefixed with a one-line summary per signal (settled-start, settled-end, global min@time).

Usage:
    python3 decimate_trans.py itrans.txt                  # auto: all columns, <=400 rows
    python3 decimate_trans.py itrans.txt --max-points 300
    python3 decimate_trans.py itrans.txt --cols 0,1,4     # time, Vout, iload (pick columns)
    python3 decimate_trans.py itrans.txt --label tr_pll_4m
"""
import argparse
import sys


def _parse(path, cols):
    """Tolerant table read -> (header_names, rows[list[float]]). Skips non-numeric lines."""
    rows, ncol = [], None
    with open(path) as fh:
        for ln in fh:
            s = ln.strip()
            if not s:
                continue
            parts = [p for p in s.replace(",", " ").replace(";", " ").split() if p]
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                continue                                   # header / comment / units line
            if len(vals) < 2:
                continue
            if ncol is None:
                ncol = len(vals)
            if len(vals) != ncol:
                continue                                   # ragged line -> skip
            rows.append(vals)
    if not rows:
        sys.exit(f"decimate_trans: no numeric rows parsed from {path} "
                 f"(expected >=2 numeric columns per line)")
    if cols:
        idx = [int(c) for c in cols.split(",")]
        rows = [[r[i] for i in idx] for r in rows]
    return rows


def _rdp_keep(xs, ys, eps):
    """Iterative Ramer-Douglas-Peucker on normalized (xs,ys) in [0,1]; returns kept index set."""
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
        dx, dy = x1 - x0, y1 - y0
        denom = (dx * dx + dy * dy) ** 0.5 or 1e-30
        dmax, imax = -1.0, -1
        for i in range(i0 + 1, i1):
            d = abs(dy * (xs[i] - x0) - dx * (ys[i] - y0)) / denom   # perp. distance to chord
            if d > dmax:
                dmax, imax = d, i
        if dmax > eps and imax > 0:
            keep[imax] = True
            stack.append((i0, imax))
            stack.append((imax, i1))
    return {i for i in range(n) if keep[i]}


def _norm(a):
    lo, hi = min(a), max(a)
    rng = (hi - lo) or 1.0
    return [(x - lo) / rng for x in a], lo, hi


def decimate(rows, max_points):
    """Union the RDP-kept indices of EVERY signal column onto a shared time grid (so each signal's
    features survive), plus each signal's global min/max, capped to max_points by tuning epsilon."""
    t = [r[0] for r in rows]
    sigs = [[r[c] for r in rows] for c in range(1, len(rows[0]))]
    tn, _, _ = _norm(t)
    sn = [_norm(s)[0] for s in sigs]

    forced = {0, len(t) - 1}
    for s in sigs:                                         # never lose a global extreme (dip bottom)
        forced.add(s.index(min(s)))
        forced.add(s.index(max(s)))

    def kept_for(eps):
        keep = set(forced)
        for y in sn:
            keep |= _rdp_keep(tn, y, eps)
        return keep

    lo, hi = 1e-9, 1.0                                     # binary-search eps -> <= max_points
    keep = kept_for(lo)
    if len(keep) > max_points:
        for _ in range(40):
            mid = (lo * hi) ** 0.5
            k = kept_for(mid)
            if len(k) > max_points:
                lo = mid
            else:
                hi = mid
                keep = k
    idx = sorted(keep)
    return idx, t, sigs


def _fmt(x):
    return f"{x:.6g}"


def main():
    ap = argparse.ArgumentParser(description="Shape-preserving decimation of a Cadence transient.")
    ap.add_argument("input")
    ap.add_argument("--max-points", type=int, default=400)
    ap.add_argument("--cols", default="", help="comma list of column indices, e.g. 0,1,4")
    ap.add_argument("--label", default="", help="optional label for the summary header")
    a = ap.parse_args()

    rows = _parse(a.input, a.cols)
    idx, t, sigs = decimate(rows, a.max_points)

    lbl = a.label or a.input
    print(f"# decimate_trans: {lbl}  {len(rows)} -> {len(idx)} rows  "
          f"(t {_fmt(t[0])}..{_fmt(t[-1])}s)")
    for j, s in enumerate(sigs):
        imn = s.index(min(s))
        print(f"#   col{j + 1}: start={_fmt(s[0])} end={_fmt(s[-1])} "
              f"min={_fmt(min(s))}@{_fmt(t[imn])}s max={_fmt(max(s))}")
    ncol = 1 + len(sigs)
    head = "# t  " + "  ".join(f"col{j + 1}" for j in range(len(sigs)))
    lines = [head] + ["  ".join(_fmt(rows[i][c]) for c in range(ncol)) for i in idx]

    out = a.input.rsplit(".", 1)[0] + ".reduced.txt"
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"# wrote {out}  ({len(idx)} rows)\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
