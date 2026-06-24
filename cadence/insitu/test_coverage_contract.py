"""STAGE 1a -- the coverage 'measurement contract' foundation.

These tests pin the contract that the later coverage stages (netlist_augment / importmp /
pmu_corner) consume, and -- the critical one -- PROVE backward-compatibility: a shipped
coverage-free manifest yields the byte-identical T0 measurement matrix + groups it always did.

NO sim / Virtuoso / dsub: pure dict-in / dict-out over manifest.measurements + run.groups.

Run:  python -m pytest cadence/insitu/test_coverage_contract.py -q
"""
import json
import pathlib
import re
import sys
import tempfile

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # .../cadence on sys.path (bare-import convention)

from insitu import manifest as M                                            # noqa: E402
from insitu import run as RUN                                               # noqa: E402

WUR = HERE / "manifests" / "wur_pmu_top.json"
STANDIN = HERE / "manifests" / "pmu_top.json"

_TMP = []


def _write_tmp(obj):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(obj, f)
    f.close()
    _TMP.append(f.name)
    return f.name


def _resolved_wur_dict():
    """The shipped wur manifest with '<net:X>' -> 'X' (resolved), as a plain dict."""
    return json.loads(re.sub(r"<net:([^>]+)>", r"\1", WUR.read_text()))


def _resolved_wur():
    return M.load(_write_tmp(_resolved_wur_dict()))


# =====================================================================================
# (1) BACKWARD-COMPAT -- the critical regression: a coverage-free manifest is unchanged.
# =====================================================================================
def test_wur_backward_compat_14_points_8_groups():
    m = _resolved_wur()
    pts = M.measurements(m)
    grps = RUN.groups(m)
    # the documented wur matrix: 14 measurement points -> 8 groups
    assert len(pts) == 14, [p["tag"] for p in pts]
    assert len(grps) == 8
    # coverage DEFAULTED to the full ladder, yet ZERO extra points (no declared loads/iv/...)
    assert m["coverage"]["tier"] == "T4"
    # the existing T0 tags are byte-identical + FIRST (no reorder, no new point kinds)
    tags = [p["tag"] for p in pts]
    assert tags == [
        "z_pll", "c_pll_vco", "n_pll", "z_vco", "c_vco_pll", "n_vco",
        "p_pll_avdd1p0", "p_vco_avdd1p0",
        "y_i500n_lpf", "pi_i500n_lpf_avdd1p0",
        "y_i3p6u_vco", "pi_i3p6u_vco_avdd1p0",
        "y_i1p5u_ptat", "pi_i1p5u_ptat_avdd1p0",
    ]
    # the documented group tags survive verbatim
    gtags = {g["tag"] for g in grps}
    for t in ("g_v_out_pll", "g_v_out_vco", "g_n_pll", "g_n_vco", "g_supply_avdd1p0",
              "g_i_out_i500n_lpf", "g_i_out_i3p6u_vco", "g_i_out_i1p5u_ptat"):
        assert t in gtags
    # no coverage-kind tag leaked in
    assert not any(g["tag"].startswith(("g_iv_", "g_dc_", "g_tr_", "g_z2_")) for g in grps)


def test_standin_backward_compat_unchanged():
    m = M.load(str(STANDIN))
    pts = M.measurements(m)
    grps = RUN.groups(m)
    # the stand-in 2-rail / 2-sink topology: its established matrix is preserved verbatim
    tags = [p["tag"] for p in pts]
    assert tags == [
        "z_pll", "c_pll_vco", "n_pll", "z_vco", "c_vco_pll", "n_vco",
        "p_pll_1p0", "p_pll_1p8", "p_vco_1p0", "p_vco_1p8",
        "y_i500n", "pi_i500n_1p0", "y_i1u", "pi_i1u_1p0",
    ]
    assert m["coverage"]["tier"] == "T4"                  # defaulted, still no extra points
    assert not any(g["tag"].startswith(("g_iv_", "g_dc_", "g_tr_", "g_z2_")) for g in grps)


def test_measurements_byte_identical_with_and_without_coverage_key():
    """The strongest backward-compat statement: stripping the coverage section entirely (the
    pre-1a shape) yields the IDENTICAL measurement dicts that the defaulted manifest does."""
    m_def = _resolved_wur()                               # coverage defaulted by load()
    raw = _resolved_wur_dict()
    raw.pop("coverage", None)                             # simulate a pre-1a manifest in memory
    pts_def = M.measurements(m_def)
    pts_raw = M.measurements(raw)                         # no coverage key at all
    assert pts_def == pts_raw
    assert [g["tag"] for g in RUN.groups(m_def)] == [g["tag"] for g in RUN.groups(raw)]


# =====================================================================================
# (2) coverage_enabled tier ladder
# =====================================================================================
def test_tier_ladder_T0_only_ac_noise():
    m = _resolved_wur()
    m["coverage"]["tier"] = "T0"
    assert M.coverage_enabled(m, "ac") and M.coverage_enabled(m, "noise")
    for item in ("slew", "iv", "dropout", "load_schedule", "temp"):
        assert not M.coverage_enabled(m, item), item


def test_tier_ladder_T2_turns_iv_dropout_on():
    m = _resolved_wur()
    m["coverage"]["tier"] = "T2"
    assert M.coverage_enabled(m, "ac") and M.coverage_enabled(m, "noise")
    assert M.coverage_enabled(m, "slew")                  # T1 included (additive)
    assert M.coverage_enabled(m, "iv") and M.coverage_enabled(m, "dropout")
    assert not M.coverage_enabled(m, "load_schedule")     # T3 not yet
    assert not M.coverage_enabled(m, "temp")              # T4 not yet


def test_enable_override_flips_single_item():
    m = _resolved_wur()
    m["coverage"]["tier"] = "T0"                          # iv off by tier
    m["coverage"]["enable"] = {"iv": True}                # explicit override turns just iv on
    assert M.coverage_enabled(m, "iv")
    assert not M.coverage_enabled(m, "dropout")           # the override is item-scoped
    # the override can also turn an otherwise-on item OFF
    m["coverage"]["tier"] = "T4"
    m["coverage"]["enable"] = {"temp": False}
    assert not M.coverage_enabled(m, "temp")
    assert M.coverage_enabled(m, "iv")                    # T4 still has the rest


def test_unknown_tier_defensive_degrade_on_unvalidated_dict():
    # DEFENSIVE path only: a LOADED manifest can never carry a junk tier (validate() rejects it --
    # see test_validate_rejects_bad_tier). This exercises coverage_enabled() directly on an in-memory
    # dict that bypassed validate(): a junk tier degrades to full rather than raising IndexError.
    m = _resolved_wur()
    m["coverage"]["tier"] = "T9_bogus"                    # bypasses validate() -> defensive full
    # every TIER-LADDER item degrades to on; 'inoise' is OPT-IN only (no tier introduces it,
    # because the oprobe current-noise netlist is box-validate-pending) so it stays OFF until
    # coverage.enable.inoise is set -- assert that opt-in contract explicitly.
    for item in M.COVERAGE_ITEMS:
        if item == "inoise":
            assert not M.coverage_enabled(m, item), "inoise must be opt-in, never tier-auto-on"
            continue
        assert M.coverage_enabled(m, item), item


# =====================================================================================
# (3) NEW KINDS APPEAR when params are declared (iv / dc / tr / z2)
# =====================================================================================
def _wur_with_coverage(**cov):
    d = _resolved_wur_dict()
    d.setdefault("coverage", {}).update(cov)
    return M.load(_write_tmp(d))


def test_iv_point_appears_with_declared_sweep():
    sweep = {"type": "lin", "start": 0.0, "stop": 1.2, "n": 13}
    m = _wur_with_coverage(iv={"i500n_lpf": {"sweep": sweep}})
    pt = next(p for p in M.measurements(m) if p["tag"] == "iv_i500n_lpf")
    assert pt["analysis"] == "dc" and pt["derive"] == "iv"
    assert pt["hot"] == [("i_out", "i500n_lpf")]
    probe = M._probe_name(m, "i500n_lpf")                 # Vbias_500n_lpf
    assert pt["reads"] == [("i", probe)] and pt["save"] == [("i", probe)]
    assert pt["sweep"] == sweep and pt["key"] == "iv_i500n_lpf"
    # only the declared sink gets an iv point (the others were not declared)
    iv_tags = {p["tag"] for p in M.measurements(m) if p["tag"].startswith("iv_")}
    assert iv_tags == {"iv_i500n_lpf"}


def test_dropout_point_from_dropout_then_loads_fallback():
    # explicit coverage.dropout[o].sweep
    sweep = {"type": "log", "start": 50e-6, "stop": 2e-3, "n": 8}
    m = _wur_with_coverage(dropout={"pll": {"sweep": sweep}})
    pt = next(p for p in M.measurements(m) if p["tag"] == "dc_pll")
    assert pt["analysis"] == "dc" and pt["derive"] == "dropout"
    assert pt["hot"] == [("v_out", "pll")]
    onet = m["v_out"]["pll"]["net"]
    assert pt["reads"] == [("v", onet)] and pt["save"] == [("v", onet)]
    assert pt["sweep"] == sweep
    # the dropout sweep also FALLS BACK to coverage.loads[o].sweep when dropout is absent
    m2 = _wur_with_coverage(loads={"vco": {"sweep": sweep}})
    pt2 = next(p for p in M.measurements(m2) if p["tag"] == "dc_vco")
    assert pt2["sweep"] == sweep
    assert not any(p["tag"] == "dc_pll" for p in M.measurements(m2))   # pll not declared


def test_transient_points_one_per_step():
    steps = [{"from": 500e-6, "to": 1e-3, "label": "small"},
             {"from": 0.0, "to": 2e-3}]                   # no label -> derived from/to
    m = _wur_with_coverage(
        transient={"pll": {"steps": steps, "edge": 1e-9, "tstop": 5e-6, "tstep": 1e-9}})
    pts = [p for p in M.measurements(m) if p["tag"].startswith("tr_pll")]
    assert {p["tag"] for p in pts} == {"tr_pll_small", "tr_pll_0_0.002"}
    p0 = next(p for p in pts if p["tag"] == "tr_pll_small")
    assert p0["analysis"] == "tran" and p0["derive"] == "trans"
    assert p0["hot"] == [("v_out", "pll")]
    onet = m["v_out"]["pll"]["net"]
    assert p0["reads"] == [("v", onet)] and p0["save"] == [("v", onet)]
    assert p0["step"] == steps[0]
    assert p0["edge"] == 1e-9 and p0["tstop"] == 5e-6 and p0["tstep"] == 1e-9


def test_lin_gate_points_appear_per_output():
    m = _wur_with_coverage(lin_gate=True)
    z2 = [p for p in M.measurements(m) if p["tag"].startswith("z2_")]
    assert {p["tag"] for p in z2} == {"z2_pll", "z2_vco"}
    p = next(p for p in z2 if p["tag"] == "z2_pll")
    assert p["analysis"] == "ac" and p["derive"] == "z" and p["amp"] == 2.0
    assert p["hot"] == [("v_out", "pll")]


def test_tier_gates_block_kinds_even_when_params_declared():
    # at T0 the iv/dropout/slew kinds are gated OFF even with params present
    sweep = {"type": "lin", "start": 0.0, "stop": 1.0, "n": 5}
    d = _resolved_wur_dict()
    d["coverage"] = {"tier": "T0",
                     "iv": {"i500n_lpf": {"sweep": sweep}},
                     "dropout": {"pll": {"sweep": sweep}},
                     "transient": {"pll": {"steps": [{"from": 0, "to": 1e-3}]}}}
    m = M.load(_write_tmp(d))
    tags = {p["tag"] for p in M.measurements(m)}
    assert not any(t.startswith(("iv_", "dc_", "tr_")) for t in tags)
    assert len(M.measurements(m)) == 14                   # still just the T0 core


# =====================================================================================
# (4) grouping -- each new point is its own group; ac2 never merges 1x ac
# =====================================================================================
def test_each_new_kind_is_its_own_group():
    sweep = {"type": "lin", "start": 0.0, "stop": 1.2, "n": 5}
    m = _wur_with_coverage(
        iv={"i500n_lpf": {"sweep": sweep}},
        dropout={"pll": {"sweep": sweep}},
        transient={"pll": {"steps": [{"from": 0, "to": 1e-3, "label": "s"}]}},
        lin_gate=True)
    grps = {g["tag"]: g for g in RUN.groups(m)}
    # the new groups each carry exactly ONE member, tagged "g_"+pt.tag
    for tag, member in (("g_iv_i500n_lpf", "iv_i500n_lpf"), ("g_dc_pll", "dc_pll"),
                        ("g_tr_pll_s", "tr_pll_s"), ("g_z2_pll", "z2_pll"),
                        ("g_z2_vco", "z2_vco")):
        assert tag in grps, tag
        g = grps[tag]
        assert len(g["members"]) == 1 and g["members"][0]["tag"] == member


def test_ac2_does_not_merge_with_1x_ac():
    m = _wur_with_coverage(lin_gate=True)
    grps = {g["tag"]: g for g in RUN.groups(m)}
    # the 1x Zout group still merges z_pll + c_pll_vco; the 2x point is a SEPARATE group
    one_x = grps["g_v_out_pll"]
    assert one_x["analysis"] == "ac"
    assert {pm["tag"] for pm in one_x["members"]} == {"z_pll", "c_pll_vco"}
    assert not any(pm.get("amp") for pm in one_x["members"])     # no 2x point folded in
    two_x = grps["g_z2_pll"]
    assert two_x["members"][0]["amp"] == 2.0
    assert two_x is not one_x


def test_group_tags_collision_free():
    sweep = {"type": "lin", "start": 0.0, "stop": 1.0, "n": 5}
    m = _wur_with_coverage(
        iv={"i500n_lpf": {"sweep": sweep}, "i3p6u_vco": {"sweep": sweep}},
        dropout={"pll": {"sweep": sweep}, "vco": {"sweep": sweep}},
        transient={"pll": {"steps": [{"from": 0, "to": 1e-3, "label": "a"},
                                     {"from": 0, "to": 2e-3, "label": "b"}]}},
        lin_gate=True)
    tags = [g["tag"] for g in RUN.groups(m)]
    assert len(tags) == len(set(tags)), [t for t in tags if tags.count(t) > 1]


def test_group_carries_sweep_step_amp_for_netlister():
    sweep = {"type": "log", "start": 1e-6, "stop": 1e-3, "n": 6}
    step = {"from": 0, "to": 1e-3, "label": "s"}
    m = _wur_with_coverage(
        iv={"i500n_lpf": {"sweep": sweep}},
        transient={"pll": {"steps": [step], "edge": 1e-9, "tstop": 2e-6, "tstep": 1e-9}},
        lin_gate=True)
    grps = {g["tag"]: g for g in RUN.groups(m)}
    assert grps["g_iv_i500n_lpf"]["sweep"] == sweep        # 1b's netlister reads it off the group
    tg = grps["g_tr_pll_s"]
    assert tg["step"] == step and tg["edge"] == 1e-9 and tg["tstop"] == 2e-6 and tg["tstep"] == 1e-9
    assert grps["g_z2_pll"]["amp"] == 2.0
    # a plain ac/noise group carries none of these keys (kept identical shape)
    assert not any(k in grps["g_v_out_pll"] for k in RUN._CARRY_KEYS)


# =====================================================================================
# (5) accessors: load_points / temps / _expand_sweep / slew_en_default + validate()
# =====================================================================================
def test_expand_sweep_lin_and_log():
    lin = M._expand_sweep({"type": "lin", "start": 0.0, "stop": 10.0, "n": 5})
    assert lin == [0.0, 2.5, 5.0, 7.5, 10.0]
    log = M._expand_sweep({"type": "log", "start": 1.0, "stop": 1000.0, "n": 4})
    assert all(abs(a - b) < 1e-9 for a, b in zip(log, [1.0, 10.0, 100.0, 1000.0]))
    assert M._expand_sweep({"type": "lin", "start": 3.0, "stop": 9.0, "n": 1}) == [3.0]
    assert M._expand_sweep(None) == []
    # ascending by construction (endpoints exact)
    assert log[0] == 1.0 and abs(log[-1] - 1000.0) < 1e-9


def test_load_points_merge_dedupe_sort():
    m = _wur_with_coverage(loads={"pll": {
        "sweep": {"type": "log", "start": 50e-6, "stop": 2e-3, "n": 4},
        "points": [170e-6],                               # already on the log grid-ish
        "nominal": 500e-6, "holdout": 300e-6}})
    pts = M.load_points(m, "pll")
    assert pts == sorted(pts)                              # ascending
    assert len(pts) == len(set(pts))                      # deduped
    assert 500e-6 in pts and 300e-6 in pts and 170e-6 in pts  # nominal/holdout/points folded
    assert 50e-6 in pts and abs(pts[-1] - 2e-3) < 1e-12   # sweep endpoints present
    # undeclared output -> empty (the single-OP behaviour of today)
    assert M.load_points(m, "vco") == []


def test_load_points_empty_when_undeclared():
    m = _resolved_wur()
    assert M.load_points(m, "pll") == [] and M.load_points(m, "vco") == []


def test_temps_and_slew_en_default():
    assert M.temps(_resolved_wur()) == []                 # default single (session) temp
    m = _wur_with_coverage(temps=[-40, 55, 125])
    assert M.temps(m) == [-40, 55, 125]
    assert M.slew_en_default(_resolved_wur()) == 0
    assert M.slew_en_default(_wur_with_coverage(slew_en=1)) == 1


# ----------------------------------------------------------------- validate() rejections
def test_validate_rejects_bad_tier():
    d = _resolved_wur_dict()
    d["coverage"] = {"tier": "T7"}
    with pytest.raises(M.ManifestError) as ei:
        M.load(_write_tmp(d))
    assert "tier" in str(ei.value)


def test_validate_rejects_bad_sweep():
    d = _resolved_wur_dict()
    d["coverage"] = {"iv": {"i500n_lpf": {"sweep": {"type": "lin", "start": 0, "stop": 1}}}}  # no n
    with pytest.raises(M.ManifestError) as ei:
        M.load(_write_tmp(d))
    assert "n" in str(ei.value)
    d2 = _resolved_wur_dict()
    d2["coverage"] = {"iv": {"i500n_lpf": {"sweep": {"type": "quad", "start": 0, "stop": 1, "n": 3}}}}
    with pytest.raises(M.ManifestError) as ei2:
        M.load(_write_tmp(d2))
    assert "lin" in str(ei2.value) and "log" in str(ei2.value)


def test_validate_rejects_loads_key_not_in_v_out():
    d = _resolved_wur_dict()
    d["coverage"] = {"loads": {"NOT_A_RAIL": {"sweep": {"type": "lin", "start": 0, "stop": 1, "n": 3}}}}
    with pytest.raises(M.ManifestError) as ei:
        M.load(_write_tmp(d))
    msg = str(ei.value)
    assert "NOT_A_RAIL" in msg and "v_out" in msg


def test_validate_rejects_iv_key_not_in_i_out():
    d = _resolved_wur_dict()
    d["coverage"] = {"iv": {"pll": {"sweep": {"type": "lin", "start": 0, "stop": 1, "n": 3}}}}  # pll is v_out
    with pytest.raises(M.ManifestError) as ei:
        M.load(_write_tmp(d))
    assert "pll" in str(ei.value) and "i_out" in str(ei.value)


def test_validate_rejects_bad_enable():
    d = _resolved_wur_dict()
    d["coverage"] = {"enable": {"bogus_item": True}}
    with pytest.raises(M.ManifestError) as ei:
        M.load(_write_tmp(d))
    assert "bogus_item" in str(ei.value)
    d2 = _resolved_wur_dict()
    d2["coverage"] = {"enable": {"iv": "yes"}}            # non-bool
    with pytest.raises(M.ManifestError) as ei2:
        M.load(_write_tmp(d2))
    assert "bool" in str(ei2.value)


def test_validate_rejects_non_numeric_temps():
    d = _resolved_wur_dict()
    d["coverage"] = {"temps": [-40, "hot", 125]}
    with pytest.raises(M.ManifestError) as ei:
        M.load(_write_tmp(d))
    assert "temps" in str(ei.value)


def test_validate_accepts_absent_coverage():
    d = _resolved_wur_dict()
    d.pop("coverage", None)
    m = M.load(_write_tmp(d))                             # no raise; defaults fill it
    assert m["coverage"]["tier"] == "T4"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_inoise_optin_measurements_and_netlist_oprobe_form():
    """coverage.enable.inoise adds ONE current-noise point per sink (probe-form), and NONE when
    off (opt-in). The netlist emits the `oprobe=<probe>` current-noise statement, not the
    (net ground) voltage-noise form -- box-validate-pending but text-pinned here."""
    from insitu import run as R
    from cluster import netlist_augment as NA
    m = _resolved_wur()
    m["coverage"]["enable"] = {}                          # opt-in: OFF by default
    assert not [p for p in M.measurements(m) if p["derive"] == "noise_i"]
    m["coverage"]["enable"] = {"inoise": True}            # ON
    ni = [p for p in M.measurements(m) if p["derive"] == "noise_i"]
    assert len(ni) == len(m["i_out"]) and all(p.get("oprobe_src") for p in ni)
    g = next(g for g in R.groups(m) if g.get("oprobe_src"))
    line = NA._analysis_line(m, g)
    assert "oprobe=" in line and " noise " in f" {line} " and "(" not in line
