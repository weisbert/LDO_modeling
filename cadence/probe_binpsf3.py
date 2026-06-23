#!/usr/bin/env python3
"""READ-ONLY probe v3 -- reverse-engineer the WINDOWED (PSF window size != 0) transient PSF
layout so binpsf can read it (today it returns only the 'time' sweep -> every trans is skipped).

A windowed PSF stores the VALUE section in fixed-size BUFFERS, signal-major: per window of
`PSF window size` bytes (= W = wsz/8 doubles = points-per-window), each of the (1 sweep + N
traces) signals gets one W-double block; buffer_size == (N+1)*wsz. This probe AUTO-LOCATES the
data, decides whether the SWEEP block is first or last, finds the per-window stride, and decodes
the time axis + one named trace (default VDD0P8_PLL) across ALL points -- the spec + oracle binpsf
needs. Read-only, stdlib only. Run on the box:

    python3 cadence/probe_binpsf3.py /path/to/trz.tran [TRACE_NAME]
"""
import struct
import sys

MAJOR, MINOR, DECL, GROUP = 0x15, 0x16, 0x10, 0x11
PROP_STR, PROP_INT, PROP_DBL = 0x21, 0x22, 0x23


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0] if 0 <= o and o + 4 <= len(b) else None


def f64(b, o):
    return struct.unpack_from(">d", b, o)[0] if 0 <= o and o + 8 <= len(b) else None


def rstr(b, o):
    n = u32(b, o)
    if n is None or n > 4096 or o + 4 + n > len(b):
        return None, o
    return b[o + 4:o + 4 + n].decode("latin1", "replace"), o + 4 + ((n + 3) & ~3)


def sections(b):
    n = len(b)
    pos = 0 if u32(b, 0) == MAJOR else 4
    secs = []
    while pos + 8 <= n and u32(b, pos) == MAJOR:
        end = u32(b, pos + 4)
        if end <= pos or end > n:
            end = n
        secs.append((pos + 8, end))
        if end == n:
            break
        pos = end
    return secs


def minor_range(b, a, z):
    return (a + 8, u32(b, a + 4)) if u32(b, a) == MINOR else (a, z)


def read_header(b, a, z):
    o, props = a, {}
    while o < z:
        c = u32(b, o)
        if c not in (PROP_STR, PROP_INT, PROP_DBL):
            break
        o += 4
        name, o = rstr(b, o)
        if name is None:
            break
        if c == PROP_STR:
            val, o = rstr(b, o)
        elif c == PROP_INT:
            val = u32(b, o); o += 4
        else:
            val = f64(b, o); o += 8
        props[name] = val
    return props


def read_sweep(b, a, z):
    lo, hi = minor_range(b, a, z)
    if u32(b, lo) == DECL:
        sid = u32(b, lo + 4)
        nm, _ = rstr(b, lo + 8)
        return sid, nm
    return None, "sweep"


def read_traces(b, a, z):
    """Every trace decl (id, name, typeid). Skips a leading 0x11 GROUP header if present."""
    lo, hi = minor_range(b, a, z)
    o = lo
    if u32(b, o) == GROUP:                       # 0x11 <gid> <name> <count> then the decls
        _gid = u32(b, o + 4)
        _nm, o2 = rstr(b, o + 8)
        _cnt = u32(b, o2)
        o = o2 + 4
    traces = []
    while o + 8 <= hi and u32(b, o) == DECL:
        tid = u32(b, o + 4)
        nm, o2 = rstr(b, o + 8)
        if nm is None:
            break
        ty = u32(b, o2)
        traces.append((tid, nm, ty))
        o = o2 + 4
    return traces


def monotone_from_zero(b, off, w, tmax):
    """Return the W doubles at off iff they look like a window-0 TIME block: v[0]==0, strictly
    increasing, all in [0, tmax]. Else None."""
    vals = [f64(b, off + 8 * j) for j in range(w)]
    if any(v is None for v in vals):
        return None
    if abs(vals[0]) > 1e-18:
        return None
    for j in range(1, w):
        if not (vals[j] > vals[j - 1]) or vals[j] > tmax * 1.0001:
            return None
    return vals


def monotone_run(b, off, w, lo_prev, tmax):
    """Return W doubles at off iff strictly increasing, all > lo_prev (continuation of an earlier
    window) and <= tmax. Used to find window k>0's sweep block."""
    vals = [f64(b, off + 8 * j) for j in range(w)]
    if any(v is None for v in vals):
        return None
    if not (vals[0] > lo_prev):
        return None
    for j in range(1, w):
        if not (vals[j] > vals[j - 1]) or vals[j] > tmax * 1.0001:
            return None
    return vals


def main():
    if len(sys.argv) < 2:
        print("usage: python3 probe_binpsf3.py <trz.tran> [TRACE_NAME]"); sys.exit(2)
    path = sys.argv[1]
    want = sys.argv[2] if len(sys.argv) > 2 else "VDD0P8_PLL"
    with open(path, "rb") as f:
        b = f.read()
    print(f"=== PROBE3 {path} ({len(b)} bytes) ===")
    secs = sections(b)
    if len(secs) < 5:
        print(f"!! expected >=5 sections, got {len(secs)}"); return
    hdr = read_header(b, *secs[0])
    npts = int(hdr.get("PSF sweep points", 0))
    ntr = int(hdr.get("PSF traces", 0))
    wsz = int(hdr.get("PSF window size", 0) or 0)
    bufsz = int(hdr.get("PSF buffer size", 0) or 0)
    tmax = float(hdr.get("stop") or hdr.get("PSF sweep max") or 1e-5) or 1e-5
    if wsz == 0:
        print("!! PSF window size = 0 -> NOT windowed; the per-point reader should handle it."); return
    w = wsz // 8                                   # points per window per signal
    nwin = (npts + w - 1) // w
    sid, sname = read_sweep(b, *secs[2])
    print(f"npoints={npts}  traces={ntr}  window_size={wsz}B ({w} pts)  buffer_size={bufsz}  "
          f"(N+1)*wsz={(ntr + 1) * wsz} match={bufsz == (ntr + 1) * wsz}")
    print(f"windows={nwin} (last has {npts - (nwin - 1) * w})  sweep id={sid} name={sname!r}  tmax={tmax}")
    traces = read_traces(b, *secs[3])
    names = [t[1] for t in traces]
    print(f"parsed {len(traces)} trace decls (expected {ntr})")
    k = next((i for i, nm in enumerate(names) if nm == want
              or nm.replace("\\", "") == want), None)
    if k is None:
        print(f"!! trace {want!r} not found; first 5 = {names[:5]}, last 5 = {names[-5:]}")
        return
    print(f"target {want!r} -> trace decl-index k={k} (id={traces[k][0]}, type={traces[k][2]})")

    v0, v1 = secs[4]
    print(f"\nVALUE [{v0}..{v1})  len={v1 - v0}  ; nwin*buffer={nwin * bufsz}  "
          f"diff={(v1 - v0) - nwin * bufsz}")
    print(f"first 6 words of VALUE: " + " ".join(f"{u32(b, v0 + 4 * j):08x}" for j in range(6)))

    # 1) locate window-0 sweep block (scan for a v[0]==0 strictly-increasing W-run)
    sw0 = None
    o = v0
    while o + w * 8 <= v1 and o < v0 + bufsz + (1 << 18):
        if monotone_from_zero(b, o, w, tmax):
            sw0 = o; break
        o += 4
    if sw0 is None:
        print("!! could not locate the window-0 time block -- dumping 48 words from VALUE start:")
        for j in range(48):
            print(f"   +{j * 4:<6} {u32(b, v0 + 4 * j):08x}  {f64(b, v0 + 8 * (j // 2)) if j % 2 == 0 else ''}")
        return
    hdrlen = sw0 - v0
    t0 = monotone_from_zero(b, sw0, w, tmax)
    print(f"\nwindow-0 SWEEP block @ {sw0} (header before data = {hdrlen} B)")
    print(f"   time[0..7] = {[f'{x:.4g}' for x in t0[:8]]}")

    # 2) sweep FIRST vs LAST: which places {want} at a sane voltage?
    cand = {
        "sweep-FIRST (sweep, t0..tN)": sw0 + (1 + k) * wsz,
        "sweep-LAST  (t0..tN, sweep)": sw0 - (ntr - k) * wsz,
    }
    for label, base in cand.items():
        vk = [f64(b, base + 8 * j) for j in range(w)] if v0 <= base <= v1 - w * 8 else None
        head = [f"{x:.5g}" for x in vk[:8]] if vk else None
        print(f"   [{label}] {want} @ {base} = {head}")

    # 3) per-window stride: find window-1 sweep block (continues past window-0's last time)
    last_t = t0[-1]
    sw1 = None
    o = sw0 + wsz
    while o + w * 8 <= v1 and o < sw0 + 2 * bufsz + (1 << 18):
        if monotone_run(b, o, min(w, npts - w), last_t, tmax):
            sw1 = o; break
        o += 4
    if sw1 is not None:
        stride = sw1 - sw0
        t1 = [f64(b, sw1 + 8 * j) for j in range(min(w, npts - w))]
        print(f"\nwindow-1 SWEEP block @ {sw1}  -> stride = {stride}  "
              f"(== buffer_size? {stride == bufsz}; == buffer+hdr? {stride == bufsz + hdrlen})")
        print(f"   time[w..] = {[f'{x:.4g}' for x in t1[:6]]}")
    else:
        stride = bufsz
        print(f"\n!! window-1 sweep block not found; assuming stride = buffer_size = {bufsz}")

    # 4) decode the FULL (time, want) series with the winning hypothesis (auto-pick by sanity:
    #    prefer the candidate whose values stay within a plausible analog rail [-5, 5] V).
    def decode(base_at_win0):
        out = []
        for i in range(npts):
            win, j = divmod(i, w)
            off = base_at_win0 + win * stride + j * 8
            out.append(f64(b, off))
        return out

    times = []
    for i in range(npts):
        win, j = divmod(i, w)
        times.append(f64(b, sw0 + win * stride + j * 8))
    series = {}
    for label, base in cand.items():
        try:
            vals = decode(base)
            ok = all(v is not None and -5.0 <= v <= 5.0 for v in vals)
        except Exception:
            vals, ok = None, False
        series[label] = (vals, ok)

    print(f"\n--- FULL decode ({npts} pts); the SANE hypothesis (values in [-5,5] V) is the layout ---")
    for label, (vals, ok) in series.items():
        tag = "SANE  <<<" if ok else "insane"
        head = [f"{x:.5g}" for x in vals[:6]] if vals else None
        tail = [f"{x:.5g}" for x in vals[-3:]] if vals else None
        print(f"  [{tag}] {label}:  {want}[0..5]={head}  ...  [-3:]={tail}")
    print(f"\n  time[0..5]   = {[f'{x:.4g}' for x in times[:6]]}")
    print(f"  time[-3:]    = {[f'{x:.4g}' for x in times[-3:]]}")
    print("\n=== end probe3 (paste ALL of this back) ===")


if __name__ == "__main__":
    main()
