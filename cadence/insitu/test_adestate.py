"""Offline self-test for insitu.adestate.parse_analysis -- the ADE-live sweep-line parser.

NO live Virtuoso / skillbridge needed: parse_analysis and _split_brackets are pure string
helpers (skillbridge is lazy-imported only inside the live-ADE functions). Covers the Block-C
bracket-awareness: a Cadence-style `values=[a b c]` specific-points list (spaces inside the
brackets) must survive as ONE field instead of being shattered by a plain whitespace split, and
'values' must be an accepted sev key.

Run:  cd .../LDO_modeling && python -m pytest cadence/insitu/test_adestate.py -q
"""
import sys
import pathlib

_CADENCE = pathlib.Path(__file__).resolve().parents[1]
if str(_CADENCE) not in sys.path:
    sys.path.insert(0, str(_CADENCE))

from insitu import adestate as A                                            # noqa: E402


def test_parse_plain_ac():
    name, f = A.parse_analysis("ac start=10 stop=500M dec=20")
    assert name == "ac"
    assert f == {"start": "10", "stop": "500M", "dec": "20"}


def test_parse_drops_unknown_keys():
    # a key not in _ANALYSIS_KEYS is dropped (only real sev fields are pushed live)
    name, f = A.parse_analysis("ac start=10 stop=1G nonsense=7 dec=5")
    assert name == "ac"
    assert "nonsense" not in f and f["dec"] == "5"


def test_values_is_an_accepted_key():
    assert "values" in A._ANALYSIS_KEYS


def test_split_brackets_keeps_value_list_together():
    toks = A._split_brackets("ac start=10 stop=1G values=[304M 1.2G 2.5G] dec=20")
    assert "values=[304M 1.2G 2.5G]" in toks                # the bracketed list is ONE token
    assert toks[0] == "ac"


def test_parse_values_with_spaces():
    name, f = A.parse_analysis("ac start=10 stop=1G dec=20 values=[304M 1.2G 2.5G]")
    assert name == "ac"
    assert f["dec"] == "20"
    assert f["values"] == "[304M 1.2G 2.5G]"               # captured whole, not shattered


def test_parse_values_comma_form():
    name, f = A.parse_analysis("ac start=10 stop=1G dec=20 values=[1,2,3]")
    assert f["values"] == "[1,2,3]"


def test_noise_line_name_preserved():
    name, f = A.parse_analysis("noise start=10 stop=100M dec=20 values=[5k 50k]")
    assert name == "noise" and f["values"] == "[5k 50k]"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
