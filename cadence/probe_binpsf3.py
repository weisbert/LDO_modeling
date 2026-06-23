#!/usr/bin/env python3
"""READ-ONLY probe v3 -- pin down the WINDOWED transient PSF layout so binpsf can read it (today
it returns only 'time' -> every trans is skipped on import).

Confirmed structure (from probe run 1, arithmetic-exact):
    VALUE = [header H bytes][nwin * buffer_size][trailer]   with H = the word at VALUE+4 (0x075c),
    buffer_size == (traces+1)*window_size, each buffer = (1 sweep + traces) SIGNAL blocks of
    window_size bytes (= W doubles = points-per-window), signal-major.

This run locates a KNOWN-voltage trace (default VDD0P8_PLL ~0.8 V -- a regulated rail, very
distinctive) by VALUE, back-computes the data start + whether the SWEEP block is FIRST or LAST,
then decodes the full (time, trace) series across all buffers -- the spec + oracle for the reader.
Read-only, stdlib only, None/nan-safe. Run on the box:

    python3 cadence/probe_binpsf3.py /path/to/trz.tran [TRACE_NAME] [VLO VHI]
"""
import struct
import sys

MAJOR, MINOR, DECL, GROUP = 0x15, 0x16, 0x10, 0x11
PROP_STR, PROP_INT, PROP_DBL = 0x21, 0x22, 0x23


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0] if 0 <= o and o + 4 <= len(b) else None


def f64(b, o):
    return struct.unpack_from(">d", b, o)[0] if 0 <= o and o + 8 <= len(b) else None


def fmt(x):
    return "None" if x is None else ("nan" if x != x else f"{x:.6g}")


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
        return u32(b, lo + 4), rstr(b, lo + 8)[0]
    return None, "sweep"


def read_traces(b, a, z):
    lo, hi = minor_range(b, a, z)
    o = lo
    if u32(b, o) == GROUP:                       # 0x11 <gid> <name> <count> then the decls
        _nm, o2 = rstr(b, o + 8)
        o = o2 + 4
    traces = []
    while o + 8 <= hi and u32(b, o) == DECL:
        tid = u32(b, o + 4)
        nm, o2 = rstr(b, o + 8)
        if nm is None:
            break
        traces.append((tid, nm, u32(b, o2)))
        o = o2 + 4
    return traces


def block_vals(b, off, w):
    return [f64(b, off + 8 * j) for j in range(w)]


def main():
    if len(sys.argv) < 2:
        print("usage: python3 probe_binpsf3.py <trz.tran> [TRACE_NAME] [VLO VHI]"); sys.exit(2)
    path = sys.argv[1]
    want = sys.argv[2] if len(sys.argv) > 2 else "VDD0P8_PLL"
    vlo = float(sys.argv[3]) if len(sys.argv) > 3 else 0.55
    vhi = float(sys.argv[4]) if len(sys.argv) > 4 else 0.85
    with open(path, "rb") as f:
        b = f.read()
    print(f"=== PROBE3 {path} ({len(b)} bytes) ===")
    secs = sections(b)
    hdr = read_header(b, *secs[0])
    npts = int(hdr.get("PSF sweep points", 0))
    ntr = int(hdr.get("PSF traces", 0))
    wsz = int(hdr.get("PSF window size", 0) or 0)
    bufsz = int(hdr.get("PSF buffer size", 0) or 0)
    tmax = float(hdr.get("stop") or 1e-5)
    if wsz == 0:
        print("!! PSF window size = 0 -> NOT windowed."); return
    w = wsz // 8
    nwin = (npts + w - 1) // w
    sid, sname = read_sweep(b, *secs[2])
    traces = read_traces(b, *secs[3])
    names = [t[1] for t in traces]
    k = next((i for i, nm in enumerate(names) if nm == want or nm.replace("\\", "") == want), None)
    v0, v1 = secs[4]
    H = u32(b, v0 + 4)                            # header length (the 0x075c word)
    data = v0 + H
    print(f"npoints={npts} traces={ntr} W={w}pts/win wsz={wsz} bufsz={bufsz} "
          f"(N+1)*wsz={(ntr + 1) * wsz} nwin={nwin} last={npts - (nwin - 1) * w}")
    print(f"sweep id={sid} name={sname!r}  target {want!r} -> k={k}  tmax={tmax}")
    print(f"VALUE [{v0}..{v1}) len={v1 - v0}  header-word H={H}  data_start=v0+H={data}")
    print(f"  check: H + nwin*bufsz + trailer == len ?  H+nwin*bufsz={H + nwin * bufsz}  "
          f"len-that={(v1 - v0) - (H + nwin * bufsz)}")
    if k is None:
        print(f"!! {want!r} not in traces; first5={names[:5]} last5={names[-5:]}"); return

    # ---- locate the target's BUFFER-0 block by value: 32 doubles whose [1:] sit in [vlo,vhi] ----
    found = None
    off = data
    end0 = data + bufsz
    while off + wsz <= min(end0 + bufsz, v1):
        vals = block_vals(b, off, w)
        body = [x for x in vals[1:] if x is not None]
        if body and all(vlo <= x <= vhi for x in body):
            found = off; found_vals = vals; break
        off += wsz                                # signal blocks are wsz-aligned from data_start
    print(f"\n{want} ~[{vlo},{vhi}]V block: " +
          (f"@ {found}  (signal index (off-data)/wsz = {(found - data) // wsz})"
           if found else "NOT FOUND by value -- widen [VLO VHI]?"))
    if found:
        sidx = (found - data) // wsz
        print(f"   {want}[0..7] = {[fmt(x) for x in found_vals[:8]]}")
        print(f"   -> sweep-FIRST expects signal index {1 + k}; sweep-LAST expects {k}; "
              f"FOUND {sidx} -> {'sweep-FIRST' if sidx == 1 + k else 'sweep-LAST' if sidx == k else '??'}")

    # ============ COMPREHENSIVE DUMP -- one run, everything needed to nail the layout ============
    nbuf = (v1 - v0 - H - 16) // bufsz             # data buffers between header and 16B trailer
    print(f"\nderived buffers = (len-H-16)/bufsz = {nbuf}")

    # (1) the 1884B VALUE header: list every NON-ZERO 4-byte word (the window index / counts live
    #     here -- it's mostly zeros). Both as u32 and as the float it would be.
    print(f"\n--- VALUE header [{v0}..{v0 + H}) non-zero words (off-in-header : u32 : asF32-ish) ---")
    nz = []
    for o in range(v0, v0 + H, 4):
        wv = u32(b, o)
        if wv:
            nz.append((o - v0, wv))
    for off, wv in nz[:60]:
        print(f"   +{off:<6} 0x{wv:08x} = {wv}")
    print(f"   ({len(nz)} non-zero words total)")

    # (2) sig0 (TIME) raw 32 doubles for EVERY buffer -- reveals the per-block meta + per-window
    #     valid count (where it goes nan) + cross-window time continuity.
    def block(buf, signal):
        base = data + buf * bufsz + signal * wsz
        return [f64(b, base + 8 * j) for j in range(w)]

    def n_finite_after2(vals):                     # valid data dbls assuming 2 leading meta
        c = 0
        for x in vals[2:]:
            if x is None or x != x:                # None/nan -> stop
                break
            c += 1
        return c

    print(f"\n--- TIME (signal 0) -- all {nbuf} buffers, 32 doubles each (idx0,1 = suspected meta) ---")
    for bw in range(nbuf):
        vals = block(bw, 0)
        print(f"  buf{bw}: " + " ".join(fmt(x) for x in vals))
        print(f"        meta?=[{fmt(vals[0])},{fmt(vals[1])}]  finite-after-2 = {n_finite_after2(vals)}")

    # (3) VDD0P8_PLL (signal 1+k) -- first/last of each buffer + finite count, to confirm sweep-FIRST
    print(f"\n--- {want} (signal {1 + k}) -- per buffer: [first6] ... [last4], finite-after-2 ---")
    for bw in range(nbuf):
        vals = block(bw, 1 + k)
        print(f"  buf{bw}: {[fmt(x) for x in vals[:6]]} ... {[fmt(x) for x in vals[-4:]]}  "
              f"finite-after-2={n_finite_after2(vals)}")

    # (4) sum of per-window finite-after-2 counts vs npoints (does [2 meta][N data] tile to 210?)
    tot = sum(n_finite_after2(block(bw, 0)) for bw in range(nbuf))
    print(f"\n--- sum(finite-after-2 over buffers) = {tot}  vs  npoints = {npts}  "
          f"(match -> layout is [2 meta][N data] per block, N varies, last window short) ---")
    print("\n=== end probe3 (paste ALL of this) ===")


if __name__ == "__main__":
    main()
