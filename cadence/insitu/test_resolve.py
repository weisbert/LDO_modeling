"""Offline self-test for component B (symbol-pin -> TB-net resolver).

NO live Virtuoso / skillbridge server is required: every test here is static or uses
an injected fake ws, so it runs anywhere (CI, dev box). The two contract checks the
brief mandates:
  1. cadence/skill/resolve_nets.il exists and is paren-balanced (string/comment aware).
  2. resolve_nets(..., session=None) raises ResolveUnavailable when there is no live
     Virtuoso bridge.
Plus a few behavioural checks of the pure-Python normalisation using a fake ws, so the
(pin net)-pairs -> {pin: net} mapping and the "<unresolved>" pass-through are covered
without a simulator.

Run:  cd .../LDO_modeling && python -m pytest cadence/insitu/test_resolve.py -q
"""
import sys
import pathlib

import pytest

# Make the cadence/ siblings importable by bare name (mirrors the package convention),
# so `from insitu.resolve import ...` works when pytest collects this file directly.
_CADENCE = pathlib.Path(__file__).resolve().parents[1]
if str(_CADENCE) not in sys.path:
    sys.path.insert(0, str(_CADENCE))

from insitu import resolve as R               # noqa: E402


# --------------------------------------------------------------------------- 1) .il
def _balanced(src):
    """True iff parens balance, ignoring ;-comments and "..." strings (with \\ escapes).
    A tiny SKILL-aware paren scanner -- enough to catch an unbalanced edit."""
    depth = 0
    in_str = False
    esc = False
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == ";":                         # comment to end of line
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not in_str


def test_il_exists():
    assert R.RESOLVE_IL.is_file(), f"missing SKILL helper {R.RESOLVE_IL}"


def test_il_paren_balanced():
    src = R.RESOLVE_IL.read_text()
    assert _balanced(src), "resolve_nets.il parentheses are not balanced"


def test_il_defines_entry_point():
    """The pinned SKILL entry point insResolveNets must be defined in the file, along
    with the helpers the Python side relies on existing (bus expansion)."""
    src = R.RESOLVE_IL.read_text()
    assert "defun insResolveNets" in src
    assert "defun insExpandBusPin" in src
    # the inline SKILL citations the brief mandates
    assert "dbFindAnyInstByName" in src
    assert "dbOpenCellViewByType" in src
    assert "; Ref:" in src or ";; SKILL refs" in src


# ----------------------------------------------------- 2) offline => ResolveUnavailable
def test_resolve_raises_without_session(monkeypatch):
    """With session=None and skillbridge unavailable, resolve_nets must raise
    ResolveUnavailable (not crash, not fabricate). We force the 'no skillbridge'
    condition deterministically by making the import fail, so this holds whether or
    not a server happens to be listening in the environment."""
    real_import = __import__

    def _no_skillbridge(name, *a, **k):
        if name == "skillbridge" or name.startswith("skillbridge."):
            raise ImportError("skillbridge blocked for offline test")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_skillbridge)
    with pytest.raises(R.ResolveUnavailable):
        R.resolve_nets("L", "C", "schematic", "I0", ["AVDD1P0"], session=None)


def test_open_ws_helper_raises_offline(monkeypatch):
    """The _open_ws helper itself maps an absent/unreachable bridge to
    ResolveUnavailable (covers the connect-failure branch too)."""
    real_import = __import__

    def _no_skillbridge(name, *a, **k):
        if name == "skillbridge" or name.startswith("skillbridge."):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_skillbridge)
    with pytest.raises(R.ResolveUnavailable):
        R._open_ws()


def test_open_ws_times_out_when_server_absent(monkeypatch):
    """skillbridge INSTALLED but no CIW server: Workspace.open() blocks -> _open_ws must
    fail FAST with ResolveUnavailable (bounded by its worker-thread deadline), not hang.
    This is the real dev-box failure mode the import-absent tests above do NOT cover."""
    import time
    skb = pytest.importorskip("skillbridge")

    def _hang(*a, **k):
        time.sleep(30)                       # far longer than our short test deadline

    monkeypatch.setattr(skb.Workspace, "open", staticmethod(_hang))
    t0 = time.time()
    with pytest.raises(R.ResolveUnavailable):
        R._open_ws(timeout=0.5)
    assert time.time() - t0 < 5.0, "must fail fast, not block on the hung connect"


def test_open_ws_maps_connect_error(monkeypatch):
    """skillbridge installed but the connect RAISES (refused) -> ResolveUnavailable."""
    skb = pytest.importorskip("skillbridge")

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(skb.Workspace, "open", staticmethod(_boom))
    with pytest.raises(R.ResolveUnavailable):
        R._open_ws(timeout=2.0)


# ------------------------------------------------- normalisation with an injected ws
class _FakeWS:
    """Minimal stand-in for a skillbridge Workspace: ws["fn"](...) dispatch. Records
    the load() path and returns canned insResolveNets pairs -- no Virtuoso needed."""

    def __init__(self, pairs):
        self._pairs = pairs
        self.loaded = []
        self.calls = []

    def __getitem__(self, fn):
        def _call(*args):
            if fn == "load":
                self.loaded.append(args[0])
                return True
            if fn == "insResolveNets":
                self.calls.append(args)
                return self._pairs
            raise KeyError(fn)
        return _call


def test_resolve_maps_pairs_to_dict():
    """A live ws is injected via session=; the (pin net) pairs SKILL returns become a
    {pin: net} dict. Also confirms the .il is sourced (load called) and arguments are
    forwarded verbatim."""
    ws = _FakeWS([["AVDD1P0", "avdd"], ["VDD0P8_DIG", "vdig"]])
    out = R.resolve_nets("Lib", "tb", "schematic", "I0",
                         ["AVDD1P0", "VDD0P8_DIG"], session=ws)
    assert out == {"AVDD1P0": "avdd", "VDD0P8_DIG": "vdig"}
    assert ws.loaded and ws.loaded[0].endswith("resolve_nets.il")
    # the call forwards (lib, cell, view, inst, pins-as-list)
    assert ws.calls[0][:4] == ("Lib", "tb", "schematic", "I0")
    assert ws.calls[0][4] == ["AVDD1P0", "VDD0P8_DIG"]


def test_resolve_passes_through_unresolved():
    """A pin the SKILL side could not bind comes back as the UNRESOLVED marker and is
    passed through unchanged -- a gap is visible, never guessed or dropped."""
    ws = _FakeWS([["AVDD1P0", "avdd"], ["MYSTERY", R.UNRESOLVED]])
    out = R.resolve_nets("Lib", "tb", "schematic", "I0",
                         ["AVDD1P0", "MYSTERY"], session=ws)
    assert out["MYSTERY"] == R.UNRESOLVED
    assert out["AVDD1P0"] == "avdd"


def test_resolve_handles_expanded_bus_pairs():
    """The SKILL side expands bus pins to bits and returns one pair per bit; the dict
    therefore carries one entry per resolved bit (the Python side does not re-collapse
    them -- it mirrors what SKILL reports)."""
    ws = _FakeWS([["ctrl<3>", "n3"], ["ctrl<2>", "n2"],
                  ["ctrl<1>", "n1"], ["ctrl<0>", "n0"]])
    out = R.resolve_nets("Lib", "tb", "schematic", "I0", ["ctrl<3:0>"], session=ws)
    assert out == {"ctrl<3>": "n3", "ctrl<2>": "n2",
                   "ctrl<1>": "n1", "ctrl<0>": "n0"}


def test_resolve_tolerates_empty_and_malformed_pairs():
    """Defensive normalisation: a None / empty / short pair from the wire must not
    crash; only well-formed 2-element pairs land in the dict."""
    ws = _FakeWS([["A", "na"], ["short"], [], ["B", "nb", "extra"]])
    out = R.resolve_nets("Lib", "tb", "schematic", "I0", ["A", "B"], session=ws)
    assert out == {"A": "na", "B": "nb"}     # short/empty dropped; extra ignored


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
