"""coverage_items_to_tier_enable <-> coverage_enabled round-trip -- the serialization the
redesigned coverage UI (flat per-item checkboxes + tier preset) depends on. Locks: (1) EVERY one
of the 2^8 checkbox combinations re-reads back identically through coverage_enabled (the manifest
schema is unchanged, so downstream is untouched); (2) a combination that equals a tier ladder
serializes to JUST that tier (byte-identical to the old tier-only UI -> existing manifests are
unchanged); (3) inoise stays opt-in (never introduced by a tier/preset)."""
import itertools
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from insitu import manifest as M                            # noqa: E402

ITEMS = M.COVERAGE_ITEMS


def _enabled_set(tier, enable):
    """The set coverage_enabled() reports for a synthetic coverage dict."""
    m = {"coverage": {"tier": tier, "enable": dict(enable)}}
    return {it for it in ITEMS if M.coverage_enabled(m, it)}


@pytest.mark.parametrize("r", range(len(ITEMS) + 1))
def test_every_checkbox_combination_round_trips(r):
    """For EVERY subset of COVERAGE_ITEMS, (tier, enable) reproduces it exactly via coverage_enabled."""
    for combo in itertools.combinations(ITEMS, r):
        checked = set(combo)
        tier, enable = M.coverage_items_to_tier_enable(checked)
        assert tier in M.TIERS
        got = _enabled_set(tier, enable)
        assert got == checked, f"{checked} -> (tier={tier}, enable={enable}) read back {got}"


def test_each_tier_ladder_serializes_to_just_that_tier():
    """A checkbox set that equals a tier's cumulative ladder -> (that tier, {}) -- no enable dict,
    byte-identical to the old tier-only UI so existing manifests are not rewritten."""
    for t in M.TIERS:
        checked = M.tier_cumulative(t)
        tier, enable = M.coverage_items_to_tier_enable(checked)
        assert (tier, enable) == (t, {}), f"{t} ladder -> ({tier}, {enable})"


def test_inoise_is_never_introduced_by_a_tier_or_preset():
    """inoise is in no tier, so tier_cumulative never includes it and a preset never checks it
    (opt-in). It only ever appears as an explicit True delta when the user checks it."""
    for t in M.TIERS:
        assert "inoise" not in M.tier_cumulative(t)
    tier, enable = M.coverage_items_to_tier_enable({"ac", "noise", "inoise"})
    assert enable.get("inoise") is True
    tier, enable = M.coverage_items_to_tier_enable({"ac", "noise"})
    assert "inoise" not in enable


def test_t4_default_minus_inoise_is_byte_clean_full():
    """The shipped default (tier T4, no enable) reads as 'all items but inoise'; re-serializing that
    exact set must give back ({tier:'T4'}, no enable) -- the backward-compat anchor."""
    full = {"coverage": {"tier": "T4"}}
    checked = {it for it in ITEMS if M.coverage_enabled(full, it)}
    assert checked == set(ITEMS) - {"inoise"}
    assert M.coverage_items_to_tier_enable(checked) == ("T4", {})


def test_expressible_combinations_old_ui_could_not():
    """'T0 + temperature only' (no slew/iv/dropout) -- impossible with the old additive tier combo
    -- now serializes compactly and reads back exactly."""
    checked = {"ac", "noise", "temp"}
    tier, enable = M.coverage_items_to_tier_enable(checked)
    assert tier == "T0" and enable == {"temp": True}
    assert _enabled_set(tier, enable) == checked


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
