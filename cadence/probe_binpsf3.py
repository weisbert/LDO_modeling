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

    # ---- AUTO-FIND (header_skip h, points-per-window p): the combo whose sweep (signal 0) decodes
    #      to a CLEAN monotone time 0 -> ~tmax wins. Each 256B signal block may hold [h meta dbls]
    #      [p data dbls] with h+p == w(=32). p must tile: ceil(npts/p) <= nbuf(=nwin). ----
    def decode(h, p, signal):
        out = []
        for i in range(npts):
            bf, j = divmod(i, p)
            out.append(f64(b, data + bf * bufsz + signal * wsz + (h + j) * 8))
        return out

    def time_ok(t):
        if not t or t[0] is None or abs(t[0]) > 1e-15:
            return False
        for i in range(1, npts):
            if t[i] is None or t[i] < t[i - 1]:
                return False
        return 0.5 * tmax <= t[-1] <= tmax * 1.0001

    print("\n--- AUTO-FIND (header_skip h, pts/window p): clean monotone time 0->~tmax wins ---")
    winner = None
    for p in range(w, 27, -1):                     # 32,31,30,29,28
        if -(-npts // p) > nwin:                   # ceil(npts/p) must fit the buffer count
            continue
        for h in range(0, w - p + 1):              # h + p <= w
            t = decode(h, p, 0)                    # sweep = signal 0 (sweep-FIRST, confirmed)
            ok = time_ok(t)
            if ok and winner is None:
                winner = (h, p)
            if ok or (h, p) in ((0, w), (2, 30)):
                print(f"  h={h} p={p}: {'CLEAN <<<' if ok else 'no'}  "
                      f"time[0..4]={[fmt(x) for x in t[:5]]} time[-3:]={[fmt(x) for x in t[-3:]]}")

    if winner is None:
        print("\n!! no (h,p) gave a clean time -- dumping sig0 block-0 raw (32 dbls):")
        print("   " + " ".join(fmt(f64(b, data + j * 8)) for j in range(w)))
        return
    h, p = winner
    print(f"\n==> LAYOUT: signal-major, sweep=signal 0; each {wsz}B block = [{h} meta][{p} data]; "
          f"{nwin} buffers @ data_start + w*{bufsz}; {nwin}*{p}={nwin * p} >= {npts}")
    t = decode(h, p, 0)
    vk = decode(h, p, 1 + k)                        # trace k = signal 1+k
    print(f"  time[0..6] = {[fmt(x) for x in t[:7]]}")
    print(f"  time[-6:]  = {[fmt(x) for x in t[-6:]]}   (stop={tmax:g})")
    print(f"  {want}[0..6] = {[fmt(x) for x in vk[:7]]}")
    print(f"  {want}[-6:]  = {[fmt(x) for x in vk[-6:]]}")
    # window-boundary continuity check (points p-1, p, p+1 across the first two windows)
    print(f"  window0/1 seam time[{p-1},{p},{p+1}] = {[fmt(t[p-1]), fmt(t[p]), fmt(t[p+1])]}")
    print("\n=== end probe3 (paste ALL of this) ===")


if __name__ == "__main__":
    main()
