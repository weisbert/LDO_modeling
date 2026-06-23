#!/usr/bin/env python3
"""READ-ONLY probe v2 for a GROUPED (PSF groups=1) noise PSF -- nails the facts probe v1
left open, so binpsf can be extended confidently:

  * the FULL ordered trace-id sequence (parsed, not capped) + the `out` trace's id/type
  * each value-entry's WIDTH measured directly from the bytes (so we learn struct widths
    WITHOUT parsing the struct grammar) -- in particular `out`'s width
  * a point-boundary + stride validation (does every sweep point really hold sweep + all
    traces, constant stride?) and npoints*stride == VALUE-section length
  * the actual (freq, out[..]) series for ALL sweep points, decoded as doubles -- so we can
    eyeball that the output-noise PSD is sane (and reuse it to validate the eventual fit)

Standalone, stdlib only, never writes. Run on the box:

    python3 cadence/probe_binpsf2.py /path/to/psf/g_n_pll/nz.noise | tee /tmp/probe2.txt
"""
import struct
import sys

MAJOR, MINOR, DECL = 0x15, 0x16, 0x10
PROP_STR, PROP_INT, PROP_DBL = 0x21, 0x22, 0x23


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0] if o + 4 <= len(b) else None


def f64(b, o):
    return struct.unpack_from(">d", b, o)[0] if o + 8 <= len(b) else None


def read_str(b, o):
    n = u32(b, o)
    if n is None or n > 4096 or o + 4 + n > len(b):
        return None, o
    s = b[o + 4:o + 4 + n].decode("latin1", "replace")
    return s, o + 4 + ((n + 3) & ~3)


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
        name, o = read_str(b, o)
        if name is None:
            break
        if code == PROP_STR:
            val, o = read_str(b, o)
        elif code == PROP_INT:
            val = u32(b, o); o += 4
        else:
            val = f64(b, o); o += 8
        props[name] = val
    return props


def read_sweep(b, a, z):
    """SWEEP section -> (sweep_id, sweep_name)."""
    lo, hi = minor_range(b, a, z)
    if u32(b, lo) == DECL:
        sid = u32(b, lo + 4)
        name, _ = read_str(b, lo + 8)
        return sid, name
    return None, "sweep"


def read_all_traces(b, a, z):
    """Full TRACE minor range -> [(id, name, typeid)] (every decl, not capped)."""
    lo, hi = minor_range(b, a, z)
    o, traces = lo, []
    while o + 8 <= hi and u32(b, o) == DECL:
        tid = u32(b, o + 4)
        name, o2 = read_str(b, o + 8)
        if name is None:
            break
        typeid = u32(b, o2)
        traces.append((tid, name, typeid))
        o = o2 + 4
    return traces, lo, hi, o


def scan_to_marker(b, o, limit, next_id):
    """Step in whole-double (8-byte) strides to the next `0x10 <next_id>`; return that offset."""
    p = o
    while p + 8 <= limit:
        if u32(b, p) == DECL and u32(b, p + 4) == next_id:
            return p
        p += 8
    return limit


def main():
    if len(sys.argv) < 2:
        print("usage: python3 probe_binpsf2.py <nz.noise>")
        sys.exit(2)
    path = sys.argv[1]
    with open(path, "rb") as f:
        b = f.read()
    print(f"=== PROBE2 {path}  ({len(b)} bytes) ===")

    secs = sections(b)
    if len(secs) < 5:
        print(f"!! expected >=5 sections, found {len(secs)} -- aborting"); return
    hdr = read_header(b, *secs[0])
    npoints = int(hdr.get("PSF sweep points", 0))
    hdr_traces = int(hdr.get("PSF traces", 0))
    print(f"PSF groups={hdr.get('PSF groups')}  sweep points={npoints}  "
          f"header PSF traces={hdr_traces}")

    sweep_id, sweep_name = read_sweep(b, *secs[2])
    print(f"sweep: id={sweep_id} name={sweep_name!r}")

    traces, tlo, thi, tend = read_all_traces(b, *secs[3])
    print(f"\nTRACE decls parsed: {len(traces)}  (minor range [{tlo}..{thi}), parse stopped @ {tend})")
    if traces:
        for tid, nm, ty in traces[:3]:
            print(f"  first  id={tid} type={ty} name={nm!r}")
        for tid, nm, ty in traces[-3:]:
            print(f"  last   id={tid} type={ty} name={nm!r}")
    # locate 'out'
    out_idx = next((i for i, t in enumerate(traces) if t[1] == "out"), None)
    if out_idx is None:
        print("!! no trace named 'out' in the decls -- importmp needs it. listing types of last 6:")
        for t in traces[-6:]:
            print("   ", t)
        return
    out_id, _, out_type = traces[out_idx]
    print(f"\n'out' trace: id={out_id} type={out_type} decl-index={out_idx} of {len(traces)}")

    # ---- walk point 1 from VALUE start, measuring each entry width by scan-to-next-marker ----
    v0, v1 = secs[4]
    seq = [(sweep_id, sweep_name)] + [(t[0], t[1]) for t in traces]
    print(f"\nVALUE [{v0}..{v1})  len={v1 - v0}  ; per-point entries expected={len(seq)}")
    o = v0
    widths, out_off, off_acc = {}, None, 0
    bad = None
    from collections import Counter
    whist = Counter()
    for k, (eid, nm) in enumerate(seq):
        if u32(b, o) != DECL or u32(b, o + 4) != eid:
            bad = (k, eid, nm, o, u32(b, o), u32(b, o + 4)); break
        payload = o + 8
        nxt = seq[k + 1][0] if k + 1 < len(seq) else sweep_id   # last entry -> next point's sweep
        end = scan_to_marker(b, payload, v1, nxt)
        w = (end - payload) // 8
        widths[eid] = w
        whist[w] += 1
        if eid == out_id:
            out_off = off_acc
            out_vals0 = [f64(b, payload + 8 * j) for j in range(w)]
        off_acc += 8 + w * 8
        o = end
    if bad:
        k, eid, nm, oo, gotm, gotid = bad
        print(f"!! point-1 walk broke at entry {k} (expected id={eid} {nm!r}) @ {oo}: "
              f"got marker=0x{gotm:08x} id={gotid}. Dumping 8 words:")
        print("   " + " ".join(f"{u32(b, oo + 4 * j):08x}" for j in range(8)))
        return
    stride = o - v0
    print(f"point-1 consumed {len(seq)} entries; stride={stride} B; off_acc={off_acc} "
          f"(== stride? {off_acc == stride})")
    print(f"width histogram (doubles per entry -> count): {dict(sorted(whist.items()))}")
    print(f"'out' width={widths[out_id]} doubles ; out_off-within-point={out_off} B ; "
          f"point-0 out doubles={out_vals0}")

    # ---- validations ----
    nextw, nextid = u32(b, o), u32(b, o + 4)
    print(f"\nVALIDATE next-point marker @ {o}: 0x{nextw:08x} id={nextid} "
          f"(expect 0x10 id={sweep_id})  -> {'OK' if (nextw == DECL and nextid == sweep_id) else 'MISMATCH'}")
    exp = npoints * stride
    print(f"VALIDATE npoints*stride={exp} vs VALUE len={v1 - v0}  -> "
          f"{'OK' if exp == (v1 - v0) else f'DIFF {exp - (v1 - v0)}'}")

    # ---- read freq + out for ALL points directly via stride + out_off ----
    print(f"\n--- (freq, out[total]) for all {npoints} points (out shown as all its doubles) ---")
    for i in range(npoints):
        base = v0 + i * stride
        freq = f64(b, base + 8)                          # sweep entry: 0x10 sweepid <double>
        ob = base + out_off + 8                          # out entry payload
        ods = [f64(b, ob + 8 * j) for j in range(widths[out_id])]
        print(f"  [{i:3d}] freq={freq:.6g}   out={ods}")
    print("\n=== end probe2 ===")


if __name__ == "__main__":
    main()
