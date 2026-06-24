"""Unit tests for the temperature-sweep SYNTAX SET (manifest.expand_temp_set): explicit floats
AND Cadence start:step:stop ranges (INCLUSIVE of an on-grid stop), comma-mixed. Pure + headless
(no Qt, no simulator). The GUI temps fields + build_manifest all route through this one grammar."""
import sys
import pathlib

CADENCE = pathlib.Path(__file__).resolve().parents[1]
if str(CADENCE) not in sys.path:
    sys.path.insert(0, str(CADENCE))

from insitu import manifest as M           # noqa: E402


def test_explicit_list_backward_compat():
    # a plain comma list (no colon) behaves like the old loop, only now sorted + deduped
    assert M.expand_temp_set("-40, 55, 125") == [-40.0, 55.0, 125.0]
    assert M.expand_temp_set("125, -40, 55") == [-40.0, 55.0, 125.0]   # unsorted -> sorted
    assert M.expand_temp_set("55, 55, -40") == [-40.0, 55.0]           # dedup


def test_range_inclusive_on_grid_plus_explicit():
    out = M.expand_temp_set("-40:10:120, 125")
    assert out[0] == -40.0 and out[-1] == 125.0
    assert 120.0 in out                        # on-grid stop included
    assert len(out) == 18                       # 17-pt range (incl 120) + explicit 125


def test_range_offgrid_stop_is_omitted():
    out = M.expand_temp_set("-40:10:125")       # 125 is NOT on the grid -> last on-grid is 120
    assert out[-1] == 120.0
    assert 125.0 not in out                     # type '...120, 125' to include it explicitly


def test_mixed_range_and_explicit_dedup_sort():
    # range -40:65:120 -> [-40,25,90] (next 155 > 120); plus two explicit 55 -> dedup+sort
    assert M.expand_temp_set("55, -40:65:120, 55") == [-40.0, 25.0, 55.0, 90.0]


def test_degenerate_and_malformed_never_raise():
    assert M.expand_temp_set("") == []
    assert M.expand_temp_set(None) == []
    assert M.expand_temp_set("abc") == []          # non-numeric token skipped
    assert M.expand_temp_set("1:2") == []          # bare start:stop (2-token) unsupported -> skip
    assert M.expand_temp_set("1:2:3:4") == []      # 4-token -> skip
    assert M.expand_temp_set("10:0:50") == [10.0]  # zero step -> single point (no infinite loop)
    assert M.expand_temp_set("0:-10:20") == [0.0]  # wrong-sign step -> single point


def test_accepts_list_and_is_idempotent():
    # the GUI pre-expands to list[float]; build_manifest re-expands -> must be a no-op
    assert M.expand_temp_set([-40, 55, 125]) == [-40.0, 55.0, 125.0]
    once = M.expand_temp_set("-40:10:120, 125")
    assert M.expand_temp_set(once) == once          # idempotent on its own output
    # a list may even carry range-string tokens (CLI convenience)
    assert M.expand_temp_set(["-40:65:120", 55]) == [-40.0, 25.0, 55.0, 90.0]


def test_temps_manifest_consumes_explicit_list():
    """coverage.temps MUST stay an explicit list of numbers (manifest._validate_coverage rejects
    a string); expansion happens at the GUI/build boundary, so the stored list passes validate."""
    out = M.expand_temp_set("-40:55:120, 125")      # -> [-40, 15, 70, 120, 125]
    m = {"name": "t", "dut": {"lib": "L", "cell": "C", "tb_lib": "TB", "tb_cell": "TBC"},
         "supplies": {"s": {"net": "VS", "dc": 1.0}}, "v_out": {}, "bias": {},
         "i_out": {"c": {"net": "VI", "dc": 0.5}}, "coverage": {"tier": "T4", "temps": out}}
    m = M._fill_defaults(m)
    assert M.validate(m) is True
    assert M.temps(m) == out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
