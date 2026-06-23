"""Self-test for cadence/binpsf.py -- the binary-PSF reader for the ade/cluster path.

Self-contained: two committed fixtures (testdata/binpsf/) captured from ONE ade AC run --
the BINARY PSF that Maestro wrote, and the SAME netlist re-emitted with `-format psfascii`.
No spectre / live session needed at test time.

What it proves:
  1. binpsf.read_binpsf parses the binary fixture: freq 10Hz->500MHz, 51 pts, 35 traces,
     complex AC values, bus-pin names un-escaped to match psfascii.
  2. The binary reader agrees with the trusted psfascii reader (cadence/psf.py) to within
     psfascii's OWN precision. psfascii rounds to ~6 significant figures (it literally
     writes `(0.125000 -9.81748e-10)`), so the floor is ~1e-5 relative -- the binary
     reader is the more accurate of the two (full double precision).
  3. psf.read_psf auto-dispatches on the bytes: binary->binpsf, text->ascii grammar.

Run:  python cadence/test_binpsf.py     (exit 0 = pass)
"""
import pathlib
import struct
import sys
import tempfile

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import binpsf                                            # noqa: E402
import psf                                               # noqa: E402

BIN = HERE / "testdata" / "binpsf" / "ac_ade_binary.ac"
ASC = HERE / "testdata" / "binpsf" / "ac_psfascii.ac"
NBIN = HERE / "testdata" / "binpsf" / "noise_binary.noise"
NASC = HERE / "testdata" / "binpsf" / "noise_psfascii.noise"

# psfascii prints ~6 significant figures; relative to a port's own magnitude that is a
# ~1e-5 floor. We assert the binary reader matches the ascii reference within 3e-5.
ASCII_PRECISION = 3e-5


# --------------------------------------------------------------- synthetic grouped PSF fixture
# The real grouped (PSF groups=1) noise PSF is the 192MB g_n_pll/nz.noise that cannot be a repo
# fixture, so we hand-build a SMALL binary PSF in the exact same byte layout the reader expects
# (big-endian, 4-byte aligned). It carries a real `out` scalar + per-instance STRUCT traces of
# WIDTH 3/4 (mirroring the real file's 3/4/7/10 widths) that the reader must DROP, plus the freq
# sweep -- so reading it back exercises: the relaxed groups>=1 guard, the constant-stride fast
# path, struct-drop, and the scalar-out read. Format mirrors binpsf.py's module docstring.
_MAJOR, _MINOR, _DECL = 0x15, 0x16, 0x10
_PROP_INT = 0x22
_DT_REAL, _DT_STRUCT = 0x0b, 0x10


def _s(txt):
    """A length-prefixed latin1 string padded up to a 4-byte boundary (PSF `str`)."""
    bb = txt.encode("latin1")
    return struct.pack(">I", len(bb)) + bb + b"\x00" * (((len(bb) + 3) & ~3) - len(bb))


def _make_grouped_fixture(path, freqs, outs, widths=None):
    """Write a minimal grouped (PSF groups=1) binary PSF: sweep 'freq' + 3 traces (two STRUCT
    instances that must drop, one real 'out' scalar). One point =
    [0x10 sweepId <freq>] [0x10 i1 <w1 d>] [0x10 i2 <w2 d>] [0x10 outId <out>].
    `widths` (per point: a list of (w1, w2)) defaults to a CONSTANT (3, 4) -> constant stride; pass
    differing per-point widths to forge a VARIABLE-stride file (the reader must fall back to the
    per-entry walk and still read freq+out correctly)."""
    assert len(freqs) == len(outs) and len(freqs) >= 1
    npoints = len(freqs)
    if widths is None:
        widths = [(3, 4)] * npoints
    assert len(widths) == npoints
    SWEEP_ID, I1_ID, I2_ID, OUT_ID = 1, 2, 3, 4
    T_REAL, T_STRUCT = 100, 102                          # type ids (struct last -> no false find)

    # HEADER body: the property list (groups=1 is the whole point of the fixture)
    hb = (struct.pack(">I", _PROP_INT) + _s("PSF groups") + struct.pack(">I", 1)
          + struct.pack(">I", _PROP_INT) + _s("PSF sweep points") + struct.pack(">I", npoints)
          + struct.pack(">I", _PROP_INT) + _s("PSF traces") + struct.pack(">I", 3))

    # TYPE decls: 0x10 <typeid> <name> <flag=0> <datatype>. _type_datatypes skips name+flag.
    type_decls = (struct.pack(">I", _DECL) + struct.pack(">I", T_REAL) + _s("real")
                  + struct.pack(">I", 0) + struct.pack(">I", _DT_REAL)
                  + struct.pack(">I", _DECL) + struct.pack(">I", T_STRUCT) + _s("noiseStruct")
                  + struct.pack(">I", 0) + struct.pack(">I", _DT_STRUCT))
    # SWEEP decl: 0x10 <sweepId> <name>  (the VALUE sweep id is read from VALUE, this names it)
    sweep_decls = struct.pack(">I", _DECL) + struct.pack(">I", SWEEP_ID) + _s("freq")
    # TRACE decls: 0x10 <traceid> <name> <typeid>. out LAST, like the real file's decl order.
    trace_decls = b"".join(struct.pack(">I", _DECL) + struct.pack(">I", tid) + _s(nm)
                           + struct.pack(">I", ty) for tid, nm, ty in
                           ((I1_ID, "inst1", T_STRUCT), (I2_ID, "inst2", T_STRUCT),
                            (OUT_ID, "out", T_REAL)))

    # VALUE body: npoints points, each the fixed id sequence with this point's struct widths.
    value_body = b""
    for (f, ov), (w1, w2) in zip(zip(freqs, outs), widths):
        value_body += struct.pack(">I", _DECL) + struct.pack(">I", SWEEP_ID) + struct.pack(">d", f)
        value_body += (struct.pack(">I", _DECL) + struct.pack(">I", I1_ID)
                       + struct.pack(f">{w1}d", *range(1, w1 + 1)))
        value_body += (struct.pack(">I", _DECL) + struct.pack(">I", I2_ID)
                       + struct.pack(f">{w2}d", *range(1, w2 + 1)))
        value_body += struct.pack(">I", _DECL) + struct.pack(">I", OUT_ID) + struct.pack(">d", ov)

    # frame: each minor section (TYPE/SWEEP/TRACE) opens with 0x16 <minorEnd>; HEADER/VALUE don't.
    MINOR_HDR = struct.pack(">I", _MINOR) + struct.pack(">I", 0)     # minorEnd patched below
    bodies = [hb, MINOR_HDR + type_decls, MINOR_HDR + sweep_decls, MINOR_HDR + trace_decls,
              value_body]
    starts, pos = [], 0
    for body in bodies:
        starts.append(pos)
        pos += 8 + len(body)                            # 8 = the 0x15 <end> section header
    total = pos
    out = bytearray()
    for i, body in enumerate(bodies):
        end = starts[i + 1] if i + 1 < len(bodies) else total
        out += struct.pack(">II", _MAJOR, end) + body
    # patch each minor section's minorEnd (body offset 4) to its section end = next section start.
    for i in (1, 2, 3):
        struct.pack_into(">I", out, starts[i] + 8 + 4, starts[i + 1])
    pathlib.Path(path).write_bytes(out)


def _grouped_check():
    """Build + read back the synthetic grouped PSF: groups=1 guard relaxed, struct traces dropped,
    only the freq sweep + the real scalar 'out' returned, values exact (full double precision)."""
    freqs = [10.0, 100.0, 1e3, 1e4, 1e5]
    outs = [8.638555e-05, 4.0e-06, 9.1e-07, 5.5e-08, 4.581145e-09]
    with tempfile.NamedTemporaryFile(suffix=".noise", delete=False) as tf:
        fpath = tf.name
    try:
        _make_grouped_fixture(fpath, freqs, outs)
        d = binpsf.read_binpsf(fpath)                   # must NOT raise (guard relaxed)
        assert d["_sweep"] == "freq", d["_sweep"]
        assert "out" in d, f"grouped: scalar 'out' missing ({list(d)})"
        assert "inst1" not in d and "inst2" not in d, "grouped: STRUCT traces must be dropped"
        assert set(k for k in d if k != "_sweep") == {"freq", "out"}, list(d)
        fb, ob = np.asarray(d["freq"]).real, np.asarray(d["out"]).real
        assert ob.dtype == float and fb.size == len(freqs), (ob.dtype, fb.size)
        assert np.allclose(fb, freqs, rtol=0, atol=0), (fb, freqs)
        assert np.allclose(ob, outs, rtol=0, atol=0), (ob, outs)   # exact: same doubles we wrote
        # force the WALK fallback (npoints<=1 -> no constant-stride measure): a 1-point grouped
        # build must still drop the structs and return freq+out exactly via the per-entry walk.
        _make_grouped_fixture(fpath, freqs[:1], outs[:1])
        d1 = binpsf.read_binpsf(fpath)
        assert set(k for k in d1 if k != "_sweep") == {"freq", "out"}, list(d1)
        assert d1["freq"][0] == freqs[0] and d1["out"][0] == outs[0], (d1["freq"], d1["out"])
        # VARIABLE-stride file: one point's struct is WIDER, so the per-point stride is NOT constant.
        # The fast path's invariant must REJECT it (a fixed stride would silently misread freq/out)
        # and fall back to the per-entry walk, which reads freq+out EXACTLY despite the width change.
        vf = [10.0, 100.0, 1000.0]; vo = [1e-5, 2e-6, 3e-7]
        _make_grouped_fixture(fpath, vf, vo, widths=[(3, 4), (7, 4), (3, 4)])
        dv = binpsf.read_binpsf(fpath)
        assert set(k for k in dv if k != "_sweep") == {"freq", "out"}, list(dv)
        assert np.allclose(np.asarray(dv["freq"]).real, vf, rtol=0, atol=0), (dv["freq"], vf)
        assert np.allclose(np.asarray(dv["out"]).real, vo, rtol=0, atol=0), (dv["out"], vo)
    finally:
        pathlib.Path(fpath).unlink(missing_ok=True)
    return len(freqs)


def _worst_rel(a, b):
    """Max over common signals of max|a-b| / max|a| (magnitude-aware relative error)."""
    common = sorted(set(k for k in a if k != "_sweep") & set(k for k in b if k != "_sweep"))
    worst, wkey = 0.0, None
    for k in common:
        va = np.asarray(a[k]).astype(complex)
        vb = np.asarray(b[k]).astype(complex)
        assert va.shape == vb.shape, f"shape mismatch {k}: {va.shape} vs {vb.shape}"
        scale = max(float(np.max(np.abs(va))), 1e-30)
        err = float(np.max(np.abs(va - vb))) / scale
        if err > worst:
            worst, wkey = err, k
    return worst, wkey, len(common)


def main():
    assert BIN.exists() and ASC.exists(), f"fixtures missing under {BIN.parent}"

    # 1. structural invariants on the binary parse
    d = binpsf.read_binpsf(BIN)
    assert d["_sweep"] == "freq", d["_sweep"]
    f = np.asarray(d["freq"]).real
    assert f.size == 51, f"expected 51 sweep points, got {f.size}"
    assert abs(f[0] - 10.0) < 1e-9 and abs(f[-1] - 500e6) < 1.0, (f[0], f[-1])
    traces = [k for k in d if k not in ("_sweep", "freq")]
    assert len(traces) == 34, f"expected 34 traces, got {len(traces)}"
    assert "PLL_CTRL<0>" in d, "bus-pin name not un-escaped"          # not PLL_CTRL\<0\>
    vdd = np.asarray(d["VDD0P8_PLL"])                                  # the injected-node Zout
    assert vdd.dtype == complex and np.count_nonzero(vdd) == 51, "AC node should be complex/nonzero"
    assert abs(vdd[0].real - 0.125) < 1e-3, vdd[0]                     # DC Zout ~ 0.125 ohm

    # 2. cross-validate vs psfascii within psfascii's own precision
    a = psf.read_psf(ASC)
    worst, wkey, ncommon = _worst_rel(a, d)
    assert ncommon == 35, f"expected 35 common signals (incl freq), got {ncommon}"
    assert worst <= ASCII_PRECISION, f"binary vs ascii worst={worst:.2e} @ {wkey} > {ASCII_PRECISION:.0e}"

    # 3. psf.read_psf dispatches by content (binary file -> binpsf path)
    via_dispatch = psf.read_psf(str(BIN))
    w2, k2, _ = _worst_rel(via_dispatch, d)
    assert w2 == 0.0, f"psf.read_psf binary dispatch differs from binpsf ({w2:.2e} @ {k2})"

    # 4. NOISE PSF: real-valued + per-instance STRUCT traces (the path AC can't exercise).
    #    The struct traces vary in width per instance and are dropped; only the scalar total
    #    `out` (what importmp's noise derive reads) must come through, matching psfascii.
    nb = binpsf.read_binpsf(NBIN)
    na = psf.read_psf(NASC)
    assert nb["_sweep"] == "freq" and "out" in nb, f"noise: missing 'out' ({list(nb)})"
    fb = np.asarray(nb["freq"]).real
    assert fb.size == na["freq"].size and fb.size > 1, ("noise sweep len", fb.size)
    assert np.allclose(fb, np.asarray(na["freq"]).real, rtol=1e-9), "noise freq grid mismatch"
    ob, oa = np.asarray(nb["out"]).real, np.asarray(na["out"]).real
    assert ob.dtype == float and ob.size == fb.size, ("noise 'out' shape", ob.shape)
    nrel = float(np.max(np.abs(ob - oa))) / max(float(np.max(np.abs(oa))), 1e-30)
    assert nrel <= ASCII_PRECISION, f"noise 'out' vs ascii worst={nrel:.2e} > {ASCII_PRECISION:.0e}"

    # 5. GROUPED (PSF groups=1) PSF: the real nz.noise layout (192MB on the box) hand-built small.
    #    The relaxed guard must accept it, the constant-stride fast path must drop the per-instance
    #    STRUCT traces and return the freq sweep + the scalar 'out' exactly.
    ngrp = _grouped_check()

    print(f"PASS  binpsf AC: 51 pts x 34 traces, 10Hz->500MHz, vs psfascii worst={worst:.2e} @ {wkey} "
          f"(psfascii 6-sigfig floor); auto-dispatch exact. "
          f"NOISE: {fb.size} pts, scalar 'out' matches psfascii ({nrel:.2e}), struct traces dropped. "
          f"GROUPED (groups=1): {ngrp} pts, freq+out exact, structs dropped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
