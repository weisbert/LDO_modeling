#!/usr/bin/env python3
"""READ-ONLY probe for a binary PSF file -- structure dump for reverse-engineering the
*grouped* (PSF groups>=1) layout that binpsf.read_binpsf does not yet handle.

It opens the file read-only, never writes, and prints only a few KB of structured text
(header properties, the top-level section table, and word-aligned hexdumps of the small
sections + the HEADS/TAILS of the big ones). PSF is 4-byte-word aligned and big-endian
throughout, so a word dump with an ASCII gutter shows markers, string lengths, names and
doubles directly.

Run on the box (where the 200 MB nz.noise lives):

    python3 cadence/probe_binpsf.py /path/to/psf/g_n_pll/nz.noise

Paste the whole stdout back. Pure stdlib (no numpy) -- runs under any python3.
"""
import struct
import sys

MAJOR, MINOR, DECL = 0x15, 0x16, 0x10
PROP_STR, PROP_INT, PROP_DBL = 0x21, 0x22, 0x23
GROUP_GUESS = 0x11                       # guessed group-decl marker (DECL+1) -- verified by output


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0] if o + 4 <= len(b) else None


def f64(b, o):
    return struct.unpack_from(">d", b, o)[0] if o + 8 <= len(b) else None


def read_str(b, o):
    """(string, next_offset, raw_len) with 4-byte padding. Guards against bogus lengths."""
    n = u32(b, o)
    if n is None or n > 4096 or o + 4 + n > len(b):
        return None, o, n
    s = b[o + 4:o + 4 + n].decode("latin1", "replace")
    return s, o + 4 + ((n + 3) & ~3), n


def looks_str(b, o):
    """Heuristic: a plausible length-prefixed ASCII string at o (used only for annotation)."""
    n = u32(b, o)
    if n is None or not (1 <= n <= 64) or o + 4 + n > len(b):
        return None
    raw = b[o + 4:o + 4 + n]
    if all(32 <= x < 127 for x in raw):
        return raw.decode("latin1")
    return None


def sections(b):
    """Replicate binpsf._sections: top-level 0x15 major sections -> [(start,end)]."""
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
    if u32(b, a) == MINOR:
        return a + 8, u32(b, a + 4)
    return a, z


def read_header(b, a, z):
    o, props = a, {}
    while o < z:
        code = u32(b, o)
        if code not in (PROP_STR, PROP_INT, PROP_DBL):
            break
        o += 4
        name, o, _ = read_str(b, o)
        if name is None:
            break
        if code == PROP_STR:
            val, o, _ = read_str(b, o)
        elif code == PROP_INT:
            val = u32(b, o); o += 4
        else:
            val = f64(b, o); o += 8
        props[name] = val
    return props


def worddump(b, start, length, label):
    end = min(start + length, len(b)) & ~3
    print(f"\n--- {label}: words [{start} .. {end})  (region len {end - start} B, file {len(b)} B) ---")
    o = start
    while o < end:
        words, chars = [], ""
        for k in range(4):
            p = o + 4 * k
            if p + 4 <= end:
                words.append(f"{u32(b, p):08x}")
                chars += "".join(chr(x) if 32 <= x < 127 else "." for x in b[p:p + 4])
            else:
                words.append("        ")
        ann = looks_str(b, o)                       # annotate a string that STARTS on this line
        tail = f"   str?-> {ann!r}" if ann else ""
        print(f"{o:9d} +{o - start:<6d} {' '.join(words)}  |{chars}|{tail}")
        o += 16


def walk_traces(b, a, z):
    """Structured attempt over the TRACE minor range: dispatch on the leading marker so we
    SEE whether/how a group decl (non-0x10) is encoded. Bounded; stops at the first unknown."""
    lo, hi = minor_range(b, a, z)
    print(f"\n--- TRACE structured walk: minor range [{lo} .. {hi}) ---")
    o, n = lo, 0
    while o + 8 <= hi and n < 60:
        marker = u32(b, o)
        if marker == DECL:                          # flat trace decl: 0x10 id name typeid
            tid = u32(b, o + 4)
            name, o2, _ = read_str(b, o + 8)
            typeid = u32(b, o2)
            print(f"  @{o:<8d} 0x10 TRACE  id={tid:<4d} type={typeid:<4d} name={name!r}")
            o = o2 + 4
        elif marker == GROUP_GUESS:                 # guessed group: 0x11 id name count ...
            gid = u32(b, o + 4)
            name, o2, _ = read_str(b, o + 8)
            count = u32(b, o2)
            print(f"  @{o:<8d} 0x11 GROUP? id={gid:<4d} name={name!r} nextword(count?)={count} "
                  f"thenword={u32(b, o2 + 4)}")
            o = o2 + 4                              # continue -- children should follow
        else:
            print(f"  @{o:<8d} UNKNOWN marker 0x{marker:08x} -- stopping structured walk "
                  f"(hexdump below covers it)")
            break
        n += 1
    if n >= 60:
        print("  ... (stopped at 60 decls)")


def value_marker_scan(b, a, z, want=24, scan_cap=262144):
    """List the first `want` DECL ids found at word alignment in the VALUE section, to infer
    the per-point id sequence (or window structure). Scans at most `scan_cap` bytes so a huge
    (200 MB) windowed VALUE section with no early 0x10 markers can't run away."""
    limit = min(z, a + scan_cap)
    print(f"\n--- VALUE first {want} '0x10 <id>' markers (word-aligned, scan<= {limit - a} B) ---")
    o, seen = a, 0
    rows = []
    while o + 8 <= limit and seen < want:
        if u32(b, o) == DECL:
            rows.append((o, u32(b, o + 4)))
            seen += 1
        o += 4
    if rows:
        print("  " + ", ".join(f"@{off}:id={i}" for off, i in rows))
    else:
        print(f"  (no 0x10 markers in first {limit - a} B -- VALUE is likely windowed/grouped, "
              f"not point-by-point)")


def main():
    if len(sys.argv) < 2:
        print("usage: python3 probe_binpsf.py <file.psf|nz.noise>")
        sys.exit(2)
    path = sys.argv[1]
    with open(path, "rb") as f:
        b = f.read()
    print(f"=== PROBE {path}  ({len(b)} bytes) ===")
    print(f"leading word = 0x{u32(b, 0):08x}  (0x15 = starts at section; else 4-byte format tag)")

    secs = sections(b)
    print(f"\n--- top-level sections (expected order HEADER,TYPE,SWEEP,TRACE,VALUE) : {len(secs)} found ---")
    labels = ["HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "EXTRA6", "EXTRA7"]
    for i, (s, e) in enumerate(secs):
        lab = labels[i] if i < len(labels) else f"SEC{i}"
        print(f"  [{i}] {lab:7s} body [{s:9d} .. {e:9d})  len={e - s}")

    if not secs:
        print("!! no 0x15 sections found -- not a binary PSF, or unexpected framing.")
        worddump(b, 0, 256, "FILE HEAD")
        return

    # HEADER
    h0, h1 = secs[0]
    props = read_header(b, h0, h1)
    print("\n--- HEADER properties ---")
    for k, v in props.items():
        print(f"  {k:24s} = {v!r}")
    for key in ("PSF groups", "PSF sweep points", "PSF traces", "PSF window size",
                "PSF buffer size", "PSF sweeps"):
        if key in props:
            print(f"  >> {key} = {props[key]}")

    # TYPE head
    if len(secs) >= 2:
        t0, t1 = secs[1]
        worddump(b, t0, min(640, t1 - t0), "TYPE head")

    # SWEEP whole (small)
    if len(secs) >= 3:
        s0, s1 = secs[2]
        worddump(b, s0, min(256, s1 - s0), "SWEEP whole")

    # TRACE: structured walk + head + tail (the group decl lives here)
    if len(secs) >= 4:
        r0, r1 = secs[3]
        walk_traces(b, r0, r1)
        worddump(b, r0, min(1280, r1 - r0), "TRACE head")
        if r1 - r0 > 1280:
            worddump(b, max(r0, r1 - 384), 384, "TRACE tail")

    # VALUE: marker scan + head (how a grouped point is laid out)
    if len(secs) >= 5:
        v0, v1 = secs[4]
        value_marker_scan(b, v0, v1)
        worddump(b, v0, min(1280, v1 - v0), "VALUE head")

    # TOC / file tail
    worddump(b, max(0, len(b) - 256), 256, "FILE TAIL (TOC)")
    print("\n=== end probe ===")


if __name__ == "__main__":
    main()
