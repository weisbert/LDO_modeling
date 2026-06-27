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
        dx = (x1 - x0) or 1e-30
        dmax, imax = -1.0, -1
        for i in range(i0 + 1, i1):
            chord = y0 + (y1 - y0) * (xs[i] - x0) / dx      # the linear interpolant at xs[i]
            d = abs(ys[i] - chord)                          # VERTICAL gap = the reconstruction error
            if d > dmax:                                    # (perp. distance under-resolves steep flanks)
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


def _prethin(t, sigs, tol):
    """O(n) pre-thin so RDP (O(n^2) worst-case) never chokes on a huge adaptive-timestep export
    (1e5..1e7 pts). Keep a sample whenever ANY signal has moved more than ~tol*range since the
    LAST kept sample -- this preserves shoulders, edges AND slow ramps (cumulative move triggers
    a keep) while dropping flat-region noise below tol and long flat runs. Unlike min/max binning
    it never erases the shoulder (the last flat point before a drop), which is what makes RDP's
    chord cut straight into the dip. Endpoints always kept; RDP then bounds error + trims count."""
    n = len(t)
    rng = [(max(s) - min(s)) or 1.0 for s in sigs]
    thr = [0.3 * tol * r for r in rng]
    keep = [0]
    last = 0
    for i in range(1, n - 1):
        if any(abs(sigs[j][i] - sigs[j][last]) > thr[j] for j in range(len(sigs))):
            if i - 1 > keep[-1]:
                keep.append(i - 1)                         # the SHOULDER: last sample before the move
            keep.append(i)                                 # (else RDP's chord cuts from the prior flat
            last = i                                       #  point straight into the dip -> big error)
    keep.append(n - 1)
    return keep


def decimate(rows, max_points, tol=1e-3):
    """Two-stage, scales to millions of points: (1) O(n) min/max pre-bin to a few thousand
    candidates that retain every extreme; (2) RDP (+ forced global extrema) on the candidates,
    unioned across signals onto a shared time grid, with an epsilon FLOOR at `tol` (a fraction of
    each signal's range) so sub-tol NOISE is ignored -- otherwise the budget gets spent capturing
    flat-region noise wiggles instead of the real dip (default tol=1e-3 = 0.1% of range). Epsilon
    is raised above the floor only when the genuine features need more than max_points."""
    t = [r[0] for r in rows]
    sigs = [[r[c] for r in rows] for c in range(1, len(rows[0]))]

    cand = _prethin(t, sigs, tol)                              # stage 1: huge -> O(n), keep features
    ct = [t[i] for i in cand]
    csigs = [[s[i] for i in cand] for s in sigs]
    tn, _, _ = _norm(ct)
    sn = [_norm(s)[0] for s in csigs]

    forced = {0, len(cand) - 1}
    for s in csigs:                                        # never lose a global extreme (dip bottom)
        forced.add(s.index(min(s)))
        forced.add(s.index(max(s)))

    def kept_for(eps):                                     # positions WITHIN the candidate set
        keep = set(forced)
        for y in sn:
            keep |= _rdp_keep(tn, y, eps)
        return keep

    lo, hi = max(tol, 1e-9), 1.0                           # eps FLOOR = tol -> noise below tol ignored
    keep = kept_for(lo)                                    # tolerance-limited (fewest, all meaningful)
    if len(keep) > max_points:                             # real features exceed budget -> raise eps
        for _ in range(40):
            mid = (lo * hi) ** 0.5
            k = kept_for(mid)
            if len(k) > max_points:
                lo = mid
            else:
                hi = mid
                keep = k
    idx = sorted(cand[p] for p in keep)                   # map candidate positions -> original idx
    return idx, t, sigs


def _fmt(x):
    return f"{x:.6g}"


def main():
    ap = argparse.ArgumentParser(description="Shape-preserving decimation of a Cadence transient.")
    ap.add_argument("input")
    ap.add_argument("--max-points", type=int, default=400)
    ap.add_argument("--tol", type=float, default=1e-3,
                    help="ignore deviations below this FRACTION of each signal's range (default "
                         "1e-3 = 0.1%%); raise to drop more noise, lower to keep finer wiggles")
    ap.add_argument("--cols", default="", help="comma list of column indices, e.g. 0,1,4")
    ap.add_argument("--label", default="", help="optional label for the summary header")
    a = ap.parse_args()

    rows = _parse(a.input, a.cols)
    idx, t, sigs = decimate(rows, a.max_points, a.tol)

    lbl = a.label or a.input
    ratio = len(rows) / max(1, len(idx))
    print(f"# decimate_trans: {lbl}  {len(rows)} -> {len(idx)} rows  ({ratio:.0f}x smaller)  "
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
