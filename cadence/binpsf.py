"""Standalone reader for Cadence **binary** PSF (BINPSF), the format ADE/Maestro and
the cluster write (ALPS/Spectre). The dev fixture uses `spectre -format psfascii`
(parsed by cadence/psf.py); the production ade backend and the cluster emit BINARY PSF,
which psfascii's text grammar cannot read. This module is the binary dual: it returns
the SAME dict shape as `psf.read_psf` so the importmp firewall is byte-format agnostic.

Self-contained (pure struct/numpy, no live session, no external psf lib) -- the npz
firewall stays standalone, and the same reader handles the cluster's binary output.

Format (reverse-engineered + cross-validated against `spectre -format psfascii` on a
real ade AC run -- see test_binpsf.py). Big-endian throughout:

  file      := [leading word] section* toc
  section   := 0x15(MAJOR=21) endOffset:u32  body            # body spans [.. endOffset)
  HEADER    := property*                                      # name/value pairs
  property  := 0x21 str str   (string) | 0x22 str u32 (int) | 0x23 str f64 (double)
  TYPE/TRACE:= 0x16(MINOR=22) endOffset:u32  item*            # then a name->id index tail
  item      := 0x10(DECL=16)  id:u32  name:str  ...           # type/trace declaration
  VALUE     := entry*                                         # NON-grouped sweep layout
  entry     := 0x10(DECL=16)  id:u32  value                   # value: 1 f64 (real) or
                                                              #        2 f64 (complex re,im)
  str       := len:u32  bytes (padded up to a 4-byte boundary)

A TYPE declares a physical quantity with a datatype code: 0x0b(11)=real double,
0x0c(12)=complex (re,im). A TRACE declares a saved signal and references a TYPE by id, so
each value's width (1 vs 2 doubles) is data-driven -- AC traces are complex, the noise
`out` trace is real, and both read correctly without hardcoding the analysis.
"""
import struct
import numpy as np

_MAJOR, _MINOR, _DECL = 0x15, 0x16, 0x10           # section / sub-section / declaration
_PROP_STR, _PROP_INT, _PROP_DBL = 0x21, 0x22, 0x23
_DT_REAL, _DT_COMPLEX = 0x0b, 0x0c                 # datatype codes -> 1 / 2 doubles
_NDOUBLES = {_DT_REAL: 1, _DT_COMPLEX: 2}


class _Cur:
    """A big-endian cursor over the PSF byte buffer."""
    __slots__ = ("b", "o")

    def __init__(self, b, o=0):
        self.b, self.o = b, o

    def u32(self, at=None):
        o = self.o if at is None else at
        return struct.unpack_from(">I", self.b, o)[0]

    def f64(self, at):
        return struct.unpack_from(">d", self.b, at)[0]

    def take_u32(self):
        v = self.u32(); self.o += 4; return v

    def take_str(self):
        n = self.take_u32()
        s = self.b[self.o:self.o + n].decode("latin1")
        self.o += (n + 3) & ~3                       # pad up to 4-byte boundary
        return s.replace("\\<", "<").replace("\\>", ">")  # bus pins: psfascii is un-escaped


def _is_binary(path):
    """True if `path` is a binary PSF (vs psfascii). psfascii is plain text starting
    with `HEADER`; BINPSF starts with a small-int word and carries NUL bytes."""
    with open(path, "rb") as f:
        head = f.read(64)
    return b"\x00" in head and not head.lstrip().startswith(b"HEADER")


# ----------------------------------------------------------------- section locator
def _sections(b):
    """Walk the top-level major sections -> list of (start, end) body ranges, in file
    order: HEADER, TYPE, SWEEP, TRACE, VALUE. The file may begin with one leading word
    before the first 0x15 marker (a format tag); we skip it if present."""
    n = len(b)
    pos = 0 if struct.unpack_from(">I", b, 0)[0] == _MAJOR else 4
    secs = []
    while pos + 8 <= n and struct.unpack_from(">I", b, pos)[0] == _MAJOR:
        end = struct.unpack_from(">I", b, pos + 4)[0]
        if end <= pos or end > n:                    # the VALUE end points at the TOC
            end = n
        secs.append((pos + 8, end))
        pos = end
    return secs


def _skip_minor(c, body_start):
    """If a section body opens with a 0x16 minor-section header (TYPE/TRACE/SWEEP do),
    step past its 2-word header; otherwise stay put (HEADER/VALUE have no minor header)."""
    c.o = body_start
    if c.u32() == _MINOR:
        c.o += 8                                      # 0x16 + endOffset
    return c


# --------------------------------------------------------------------- per-section
def _read_header(c, a, z):
    """Parse the HEADER property list -> {name: value} (str/int/float)."""
    c.o = a
    props = {}
    while c.o < z:
        code = c.u32()
        if code not in (_PROP_STR, _PROP_INT, _PROP_DBL):
            break
        c.o += 4
        name = c.take_str()
        if code == _PROP_STR:
            props[name] = c.take_str()
        elif code == _PROP_INT:
            props[name] = c.take_u32()
        else:
            props[name] = c.f64(c.o); c.o += 8
    return props


def _read_types(c, a, z):
    """TYPE section -> {typeid: ndoubles}. Each item: 0x10 id name <datatype ...>.
    Only the datatype code (real/complex) is needed; struct/group types are tolerated
    (they aren't referenced by scalar traces) and default to complex."""
    _skip_minor(c, a)
    types = {}
    while c.o < z and c.u32() == _DECL:
        c.o += 4
        tid = c.take_u32()
        c.take_str()                                  # type name (unused)
        dt = c.u32(c.o + 4)                            # datatype sits one word after name
        types[tid] = _NDOUBLES.get(dt, 2)
        nxt = c.o + 8                                  # advance to the next 0x10 declaration
        while nxt < z and struct.unpack_from(">I", c.b, nxt)[0] != _DECL:
            nxt += 4
        c.o = nxt
    return types


def _read_traces(c, a, z, types):
    """TRACE section -> ordered [(traceid, name, ndoubles)]. Each item: 0x10 id name
    typeid; the typeid -> datatype width comes from `types` (default complex)."""
    _skip_minor(c, a)
    traces = []
    while c.o < z and c.u32() == _DECL:
        c.o += 4
        tid = c.take_u32()
        name = c.take_str()
        typeid = c.take_u32()
        traces.append((tid, name, types.get(typeid, 2)))
    return traces


def _read_sweep_name(c, a, z):
    """SWEEP section -> the sweep variable name (e.g. 'freq'); first declared item."""
    _skip_minor(c, a)
    if c.o < z and c.u32() == _DECL:
        c.o += 4
        c.take_u32()                                  # sweep id (the VALUE entry id is used)
        return c.take_str()
    return "sweep"


def _read_values(c, a, toc, sweep_name, traces):
    """VALUE section -> {name: ndarray}. Non-grouped sweep layout: a flat run of
    `0x10 id value` entries; the sweep var (real) opens each point, then one entry per
    saved trace. Width per id is data-driven (1 vs 2 doubles)."""
    width = {tid: nd for tid, _, nd in traces}
    name_of = {tid: nm for tid, nm, _ in traces}
    sweep_id = c.u32(a + 4)                            # id of the first entry == sweep var
    cols = {sweep_name: []}
    order = [sweep_name]
    for _, nm, _nd in traces:
        cols[nm] = []
        order.append(nm)
    c.o = a
    while c.o < toc and c.u32() == _DECL:
        c.o += 4
        eid = c.take_u32()
        if eid == sweep_id:
            cols[sweep_name].append(c.f64(c.o)); c.o += 8
        else:
            nd = width.get(eid, 2)
            if nd == 1:
                cols[name_of[eid]].append(c.f64(c.o)); c.o += 8
            else:
                cols[name_of[eid]].append(complex(c.f64(c.o), c.f64(c.o + 8))); c.o += 16
    out = {nm: np.asarray(cols[nm]) for nm in order}
    out["_sweep"] = sweep_name
    return out


# --------------------------------------------------------------------------- API
def read_binpsf(path):
    """Read a binary PSF file -> {name: ndarray} (the sweep column + every saved trace,
    complex where Spectre stored a pair, real where it stored a scalar), plus
    `_sweep` = the sweep variable name. Mirrors cadence/psf.py:read_psf exactly so the
    importmp read path is identical for ascii and binary inputs."""
    with open(path, "rb") as f:
        b = f.read()
    c = _Cur(b)
    secs = _sections(b)
    if len(secs) < 5:
        raise ValueError(f"{path}: expected >=5 PSF sections, found {len(secs)} "
                         f"(not a recognised binary PSF?)")
    (h0, h1), (t0, t1), (s0, s1), (r0, r1), (v0, v1) = secs[:5]
    hdr = _read_header(c, h0, h1)
    if int(hdr.get("PSF groups", 0)):                 # per-instance group data (e.g. noise
        raise NotImplementedError(                     # contributions) -- not needed for the
            f"{path}: grouped PSF (PSF groups={hdr['PSF groups']}) not supported; "    # contract
            "extraction saves scalar node/branch traces only")
    types = _read_types(c, t0, t1)
    traces = _read_traces(c, r0, r1, types)
    sweep_name = _read_sweep_name(c, s0, s1)
    return _read_values(c, v0, v1, sweep_name, traces)


if __name__ == "__main__":
    import sys
    d = read_binpsf(sys.argv[1])
    print("sweep:", d["_sweep"])
    for k, v in d.items():
        if k == "_sweep":
            continue
        nz = np.count_nonzero(v) if v.size else 0
        print(f"  {k:16s} {v.dtype} n={v.size} nz={nz}  "
              f"[{v.flat[0]:.4g} .. {v.flat[-1]:.4g}]" if v.size else f"  {k}: empty")
