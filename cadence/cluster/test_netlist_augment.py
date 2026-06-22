"""Unit tests for cluster.netlist_augment -- the OFFLINE (no-Virtuoso) group netlister.

NO sim / Virtuoso / dsub needed: a small synthetic base input.scs (a .tran TB with a supply
vsource on the resolved supply net + the DUT) + a RESOLVED copy of the wur_pmu_top manifest
('<net:X>' -> 'X') drive every assertion. Coverage:
  (1) per group: the base .tran analysis is STRIPPED (commented) + REPLACED (ac vs noise with
      its output port), the parameters one-hot is correct (exactly this group's hot vars =1),
      the Iext / probe / supply-mag lines are present, the save set is the union (incl <probe>:p).
  (2) the resolved-net GUARD trips on the shipped placeholder manifest.
  (3) supply auto-detect: finds the lone vsource on the net; errors (actionable) on ambiguous
      (two vsources on the net) and on missing (none); an explicit tb_src overrides auto-detect.

Run:  python -m pytest cadence/cluster/test_netlist_augment.py -q
"""
import json
import pathlib
import re
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # .../cadence on sys.path (bare-import convention)

from cluster import netlist_augment as NA                                  # noqa: E402
from insitu import manifest as M                                           # noqa: E402
from insitu import run as RUN                                              # noqa: E402

WUR = HERE.parent / "insitu" / "manifests" / "wur_pmu_top.json"


# ----------------------------------------------------------------- fixtures
def _resolved_manifest():
    """A RESOLVED copy of the shipped wur_pmu_top manifest: '<net:X>' -> 'X'."""
    raw = WUR.read_text()
    raw = re.sub(r"<net:([^>]+)>", r"\1", raw)
    d = json.loads(raw)
    return M.load(_write_tmp(d))


def _resolved_coverage_manifest():
    """A RESOLVED wur manifest with COVERAGE params injected so the new dc/tran/ac2 groups appear:
    an iv sweep on the i500n_lpf sink, a dropout sweep on the pll v_out, a transient step on pll,
    and the 2x lin-gate self-check. A coverage-free manifest (above) makes NONE of these."""
    raw = re.sub(r"<net:([^>]+)>", r"\1", WUR.read_text())
    d = json.loads(raw)
    d["coverage"] = {
        "tier": "T4",
        "iv": {"i500n_lpf": {"sweep": {"type": "lin", "start": 0.0, "stop": 1.0, "n": 11}}},
        "dropout": {"pll": {"sweep": {"type": "log", "start": 1e-4, "stop": 3e-3, "n": 8}}},
        "transient": {"pll": {"steps": [{"from": 5e-4, "to": 2e-3, "label": "step1"}],
                              "edge": 1e-9, "tstop": 1e-5, "tstep": 1e-9}},
        "lin_gate": True,
    }
    return M.load(_write_tmp(d))


_TMP = []


def _write_tmp(obj):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(obj, f)
    f.close()
    _TMP.append(f.name)
    return f.name


def _base_scs():
    """A synthetic base .tran TB matching the designer's REAL testbench under the source-reuse
    model: the DUT instance + the designer's OWN source on EVERY tagged pin (a supply vsource,
    a load isource per v_out, a compliance vdc vsource per i_out) -- the named *_src instances
    the manifest reuses -- and a .tran analysis to be stripped."""
    return (
        "simulator lang=spectre\n"
        'include "models.scs"\n'
        "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO "
        "IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\n"
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u\n"
        "Iload_vco (VDD0P8_VCO 0) isource dc=2m\n"
        "Vbias_500n_lpf (IBP_POLY_500N_LPF 0) vsource dc=1.28\n"
        "Vbias_3p6u_vco (IBP_POLY_3P6U_VCO 0) vsource dc=1.28\n"
        "Vbias_1p5u_ptat (IBP_PTAT_TUNE_1P5U_VCO 0) vsource dc=0.667\n"
        "tt tran stop=1u\n"
    )


def _base_scs_continuations():
    """A base .tran TB that uses BACKSLASH line-continuations on the DUT instance, the supply
    vsource, AND the .tran analysis -- a realistic maestro formatting. The offline netlister
    must join logical statements before stripping / mag-modifying, or it half-processes them.
    Carries the designer's own source on every tagged pin (the reuse model)."""
    return (
        "simulator lang=spectre\n"
        'include "models.scs"\n'
        "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF \\\n"
        "      IBP_POLY_3P6U_VCO IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98 \\\n"
        "    type=dc\n"
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u\n"
        "Iload_vco (VDD0P8_VCO 0) isource dc=2m\n"
        "Vbias_500n_lpf (IBP_POLY_500N_LPF 0) vsource dc=1.28\n"
        "Vbias_3p6u_vco (IBP_POLY_3P6U_VCO 0) vsource dc=1.28\n"
        "Vbias_1p5u_ptat (IBP_PTAT_TUNE_1P5U_VCO 0) vsource dc=0.667\n"
        "tt tran stop=1u \\\n"
        "    errpreset=conservative\n"
    )


def _base_dir(tmp_path, text=None):
    d = tmp_path / "base"
    d.mkdir()
    (d / "input.scs").write_text(text if text is not None else _base_scs())
    return d


def _group(m, tag):
    return next(g for g in RUN.groups(m) if g["tag"] == tag)


def _netlist_text(tmp_path, m, tag, base_text=None):
    out = tmp_path / "out"
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path, base_text), m, out)
    d = gnl(_group(m, tag))
    return pathlib.Path(d, "input.scs").read_text()


# =====================================================================================
# (1) per-group netlist shape
# =====================================================================================
def test_eight_groups_each_get_a_netlist(tmp_path):
    m = _resolved_manifest()
    out = tmp_path / "out"
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path), m, out)
    grps = RUN.groups(m)
    assert len(grps) == 8                          # the documented wur 14->8 collapse
    for g in grps:
        d = gnl(g)
        assert pathlib.Path(d) == out / g["tag"]
        assert (pathlib.Path(d) / "input.scs").is_file()


def test_base_analysis_stripped_and_commented(tmp_path):
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll")
    # the base .tran line is commented with the visible marker, NOT left live
    assert NA.STRIP_MARKER + "tt tran stop=1u" in txt
    # there is no LIVE (uncommented) tran statement anywhere
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith(NA.STRIP_MARKER) or s.startswith("//"):
            continue
        toks = s.split()
        assert not (len(toks) >= 2 and toks[1] == "tran"), f"live tran left: {line!r}"


def test_instance_lines_never_stripped(tmp_path):
    # the DUT instance + sources have node-list 2nd tokens '(...' -> never mistaken for analyses
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll")
    assert "Xdut (AVDD1P0" in txt and NA.STRIP_MARKER + "Xdut" not in txt
    assert "Iload_pll (VDD0P8_PLL 0) isource" in txt


def test_ac_group_emits_ac_analysis_and_onehot(tmp_path):
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll")
    # ac analysis with the stable name + the manifest ac line
    assert f"{NA.AC_NAME} {m['analysis']['ac']}" in txt
    assert "noise" not in txt.replace("// ", "")   # no noise analysis on an ac group
    # one-hot: exactly acm_v_out_pll = 1, everything else = 0
    pline = next(l for l in txt.splitlines() if l.startswith("parameters "))
    assert "acm_v_out_pll=1" in pline
    for var in ("acm_v_out_vco", "acm_supply_avdd1p0", "acm_i_out_i500n_lpf",
                "acm_i_out_i3p6u_vco", "acm_i_out_i1p5u_ptat"):
        assert f"{var}=0" in pline
    assert pline.count("=1") == 1                   # exactly ONE hot var


def test_noise_group_emits_noise_with_output_port(tmp_path):
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_n_pll")
    pll_net = m["v_out"]["pll"]["net"]
    ground = m["ground"]
    # noise analysis names its output PORT (OUTNET ground) so Spectre emits the 'out' signal
    assert f"{NA.NOISE_NAME} ({pll_net} {ground}) {m['analysis']['noise']}" in txt
    # noise needs NO AC stimulus -> the one-hot parameters line has every acm var at 0
    pline = next(l for l in txt.splitlines() if l.startswith("parameters "))
    assert "=1" not in pline
    # the targeted save for a noise point is the output net
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    assert pll_net in sline.split()


# =====================================================================================
# (1c) PER-OBJECT analysis override (keyed by the group OWNER; falls back to global)
# =====================================================================================
def test_per_object_ac_override_on_supply_group(tmp_path):
    # supplies.<s>.analysis.ac overrides the global ac for the supply's ac group ONLY
    m = _resolved_manifest()
    m["supplies"]["avdd1p0"]["analysis"] = {"ac": "ac start=1 stop=1G dec=5"}
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0")
    assert f"{NA.AC_NAME} ac start=1 stop=1G dec=5" in txt        # the override, not the global


def test_per_object_ac_override_on_v_out_group(tmp_path):
    # v_out.<o>.analysis.ac overrides the global ac for THAT v_out's Zout group
    m = _resolved_manifest()
    m["v_out"]["pll"]["analysis"] = {"ac": "ac start=2 stop=2G dec=7"}
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")
    txt = pathlib.Path(gnl(_group(m, "g_v_out_pll")), "input.scs").read_text()
    assert f"{NA.AC_NAME} ac start=2 stop=2G dec=7" in txt
    # the vco group is NOT affected (its owner has no override -> global)
    txt2 = pathlib.Path(gnl(_group(m, "g_v_out_vco")), "input.scs").read_text()
    assert f"{NA.AC_NAME} {m['analysis']['ac']}" in txt2


def test_per_object_noise_override_on_v_out_noise_group(tmp_path):
    # v_out.<o>.analysis.noise overrides the global noise for THAT v_out's noise group
    m = _resolved_manifest()
    m["v_out"]["pll"]["analysis"] = {"noise": "noise start=1 stop=10M dec=3"}
    txt = _netlist_text(tmp_path, m, "g_n_pll")
    pll_net = m["v_out"]["pll"]["net"]
    ground = m["ground"]
    assert f"{NA.NOISE_NAME} ({pll_net} {ground}) noise start=1 stop=10M dec=3" in txt


def test_absent_override_uses_global_analysis(tmp_path):
    # no per-object analysis -> the global m['analysis'] is used (the clean-manifest default)
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll")
    assert f"{NA.AC_NAME} {m['analysis']['ac']}" in txt


def test_v_out_and_i_out_reuse_existing_source_mag(tmp_path):
    # REUSE model: v_out sets mag on its EXISTING load isource (Iload_*); i_out sets mag on its
    # EXISTING compliance vdc vsource (Vbias_*). NO Iext_/Vprobe_ inserted (the pins are driven).
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll")
    # the v_out load isource carries mag=acm (its existing dc preserved); no Iext_ appended
    pll = next(l for l in txt.splitlines() if l.strip().startswith("Iload_pll "))
    assert "dc=500u" in pll and "mag=acm_v_out_pll" in pll
    assert "Iext_pll" not in txt and "Iext_vco" not in txt
    # the i_out vdc vsources carry mag=acm; no Vprobe_ appended
    for c in m["i_out"]:
        probe = M._probe_name(m, c)                      # the reused vdc = probe_src
        sline = next(l for l in txt.splitlines() if l.strip().startswith(probe + " "))
        assert f"mag=acm_i_out_{c}" in sline
        assert "Vprobe_" not in txt


def test_supply_mag_modified_in_place(tmp_path):
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0")
    # the EXISTING supply vsource carries mag=acm_supply_avdd1p0 (appended, dc preserved)
    sline = next(l for l in txt.splitlines() if l.strip().startswith("V_AVDD "))
    assert "dc=0.98" in sline and "mag=acm_supply_avdd1p0" in sline
    # the supply group is one-hot on the supply var
    pline = next(l for l in txt.splitlines() if l.startswith("parameters "))
    assert "acm_supply_avdd1p0=1" in pline and pline.count("=1") == 1


def test_existing_mag_replaced_not_duplicated(tmp_path):
    # a base supply source that ALREADY has a mag= must be REWRITTEN, not have a 2nd mag appended
    base = _base_scs().replace("V_AVDD (AVDD1P0 0) vsource dc=0.98",
                               "V_AVDD (AVDD1P0 0) vsource dc=0.98 mag=0")
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0", base_text=base)
    sline = next(l for l in txt.splitlines() if l.strip().startswith("V_AVDD "))
    assert sline.count("mag=") == 1 and "mag=acm_supply_avdd1p0" in sline


def test_save_set_is_union_with_probe_p(tmp_path):
    # the supply group MERGES the 2 PSRR points (read both rails + the supply) AND the 3
    # current-PSRR points (read each probe current + the supply) -> the save union covers all
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0")
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    toks = set(sline.split()[1:])
    # both output rails + the supply net (voltage saves)
    assert m["v_out"]["pll"]["net"] in toks and m["v_out"]["vco"]["net"] in toks
    assert m["supplies"]["avdd1p0"]["net"] in toks
    # every sink probe current as <probe>:p (currents need an explicit save, never allpub)
    for c in m["i_out"]:
        assert f"{M._probe_name(m, c)}:p" in toks


def test_y_group_saves_only_its_probe_current(tmp_path):
    # an admittance group reads ONLY its own probe current
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_i_out_i500n_lpf")
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    toks = set(sline.split()[1:])
    assert f"{M._probe_name(m, 'i500n_lpf')}:p" in toks
    # not the other sinks' currents (this group is one-hot on i500n_lpf only)
    assert f"{M._probe_name(m, 'i3p6u_vco')}:p" not in toks


def test_simulator_lang_spectre_header_appended(tmp_path):
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll")
    # guard a trailing spice-lang section: the appended block re-asserts spectre lang
    assert txt.count("simulator lang=spectre") >= 2     # base header + the appended guard


# =====================================================================================
# (1b) backslash line-continuations (realistic maestro formatting)
# =====================================================================================
def test_continuation_supply_mag_joined_not_broken(tmp_path):
    # a multi-line supply source is collapsed to ONE clean line carrying mag -- the mag never
    # lands after a dangling backslash, and the continued token (type=dc) is not orphaned
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0", base_text=_base_scs_continuations())
    sline = next(l for l in txt.splitlines() if l.strip().startswith("V_AVDD "))
    assert "dc=0.98" in sline and "type=dc" in sline and "mag=acm_supply_avdd1p0" in sline
    assert "\\" not in sline                              # no dangling backslash mid-statement
    assert not any(l.strip() == "type=dc" for l in txt.splitlines())   # never left orphaned/live


def test_continuation_analysis_fully_stripped(tmp_path):
    # BOTH physical lines of the multi-line .tran are neutralised: no orphan continuation and
    # no live tran survive below the comment
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll", base_text=_base_scs_continuations())
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("//"):
            continue
        assert "errpreset=conservative" not in s, f"orphan continuation left live: {line!r}"
        toks = s.split()
        assert not (len(toks) >= 2 and toks[1] == "tran"), f"live tran left: {line!r}"
    assert any(l.startswith(NA.STRIP_MARKER) and "tran" in l for l in txt.splitlines())


def test_continuation_instance_preserved_verbatim(tmp_path):
    # a multi-line DUT instance is NEITHER stripped NOR collapsed -> its physical lines survive
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll", base_text=_base_scs_continuations())
    assert "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF \\" in txt
    assert "      IBP_POLY_3P6U_VCO IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP" in txt


def test_continuation_supply_auto_detect_joined(tmp_path):
    # auto-detect must see the logical statement (vsource master is on the line, net on the
    # first node) even though the source is wrapped across two physical lines
    m = _resolved_manifest()
    del m["supplies"]["avdd1p0"]["tb_src"]                # force auto-detect (no override)
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0", base_text=_base_scs_continuations())
    assert "mag=acm_supply_avdd1p0" in txt                # the wrapped V_AVDD was found + modified


# =====================================================================================
# (2) B+ net resolution (replaces the old hard placeholder guard)
# =====================================================================================
def test_bplus_resolves_net_equals_pin_silently(tmp_path):
    # the shipped manifest ships '<net:PIN>' placeholders whose PIN IS a base net (net==pin) ->
    # B+ resolves them silently against the base netlist, no raise, nets become the bare PIN.
    m = M.load(str(WUR))
    assert m["supplies"]["avdd1p0"]["net"].startswith("<net:")    # placeholder on load
    NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")   # no raise
    # the placeholders were resolved IN PLACE to the real (==pin) nets
    assert m["supplies"]["avdd1p0"]["net"] == "AVDD1P0"
    assert m["v_out"]["pll"]["net"] == "VDD0P8_PLL"
    assert m["i_out"]["i500n_lpf"]["net"] == "IBP_POLY_500N_LPF"


def test_bplus_hard_stops_on_net_not_equal_pin(tmp_path):
    # a placeholder whose PIN is NOT a net in the base netlist (net!=pin) -> hard error listing it
    m = M.load(str(WUR))
    m["v_out"]["pll"]["net"] = "<net:NOT_A_REAL_NET>"
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")
    msg = str(ei.value)
    assert "NOT_A_REAL_NET" in msg
    assert "not a net" in msg.lower() or "could not resolve" in msg.lower()
    assert "v_out.pll" in msg                        # which role/key is unresolvable


def test_resolved_manifest_builds_without_raise(tmp_path):
    m = _resolved_manifest()
    NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")   # no raise


# =====================================================================================
# (3) supply auto-detect / tb_src override / type guardrail
# =====================================================================================
def test_auto_detect_finds_lone_vsource(tmp_path):
    m = _resolved_manifest()
    del m["supplies"]["avdd1p0"]["tb_src"]               # no tb_src -> auto-detect V_AVDD
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0")
    assert "V_AVDD (AVDD1P0 0) vsource dc=0.98 mag=acm_supply_avdd1p0" in txt


def test_auto_detect_ambiguous_errors(tmp_path):
    # two vsources whose FIRST node is the supply net -> ambiguous, actionable error
    base = _base_scs().replace(
        "V_AVDD (AVDD1P0 0) vsource dc=0.98",
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\nV_AVDD2 (AVDD1P0 0) vsource dc=0.98")
    m = _resolved_manifest()
    del m["supplies"]["avdd1p0"]["tb_src"]               # force auto-detect -> hits the ambiguity
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path, base), m, tmp_path / "out")
    msg = str(ei.value)
    assert "AMBIGUOUS" in msg or "ambiguous" in msg.lower()
    assert "V_AVDD" in msg and "V_AVDD2" in msg     # lists the candidates


def test_auto_detect_missing_supply_errors(tmp_path):
    # no vsource drives the supply net -> actionable missing error (a supply NEVER falls back)
    base = _base_scs().replace("V_AVDD (AVDD1P0 0) vsource dc=0.98\n", "")
    m = _resolved_manifest()
    del m["supplies"]["avdd1p0"]["tb_src"]               # force auto-detect -> nothing on the net
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path, base), m, tmp_path / "out")
    msg = str(ei.value)
    assert "tb_src" in msg                          # the remedy is naming the source
    assert "AVDD1P0" in msg                         # which net it could not find a source for


def test_explicit_tb_src_overrides_auto_detect(tmp_path):
    # an explicit tb_src is used VERBATIM even when another vsource sits on the net first
    base = _base_scs().replace(
        "V_AVDD (AVDD1P0 0) vsource dc=0.98",
        "V_DECOY (AVDD1P0 0) vsource dc=0.98\nV_REAL (AVDD1P0 0) vsource dc=0.98")
    m = _resolved_manifest()
    m["supplies"]["avdd1p0"]["tb_src"] = "V_REAL"
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0", base_text=base)
    # V_REAL got the mag; V_DECOY did NOT (auto-detect was overridden, not consulted)
    real = next(l for l in txt.splitlines() if l.strip().startswith("V_REAL "))
    decoy = next(l for l in txt.splitlines() if l.strip().startswith("V_DECOY "))
    assert "mag=acm_supply_avdd1p0" in real
    assert "mag=" not in decoy


def test_named_src_missing_instance_errors_at_build(tmp_path):
    # a named source NOT in the base netlist -> a clear error raised EARLY (factory-build time)
    m = _resolved_manifest()
    m["supplies"]["avdd1p0"]["tb_src"] = "V_NONEXISTENT"
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")
    assert "V_NONEXISTENT" in str(ei.value)


def test_type_guardrail_rejects_wrong_master_named_source(tmp_path):
    # naming an ISOURCE for a v_out is correct, but naming the supply VSOURCE for a v_out (or an
    # isource for an i_out) is a wrong-master error -- the read math depends on the master type.
    m = _resolved_manifest()
    m["v_out"]["pll"]["src"] = "V_AVDD"             # a vsource named where an isource is required
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")
    msg = str(ei.value)
    assert "V_AVDD" in msg and "isource" in msg and "vsource" in msg


def test_type_guardrail_rejects_isource_for_i_out(tmp_path):
    # an i_out requires a vsource (read its :p under a voltage drive); naming an isource is wrong
    m = _resolved_manifest()
    m["i_out"]["i500n_lpf"]["probe_src"] = "Iload_pll"   # an isource where a vsource is required
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")
    assert "Iload_pll" in str(ei.value) and "vsource" in str(ei.value)


# =====================================================================================
# (3b) the FALLBACK-INSERT path (open pin: no source to reuse)
# =====================================================================================
def _base_scs_open_pins():
    """A base TB where the v_out/i_out pins are OPEN (no designer source on them) -- only the
    DUT + the supply vsource. The reuse model must fall back to inserting Iext_/Vprobe_."""
    return (
        "simulator lang=spectre\n"
        'include "models.scs"\n'
        "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO "
        "IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\n"
        "tt tran stop=1u\n"
    )


def _open_pin_manifest():
    """A resolved wur manifest with the v_out 'src'/i_out 'probe_src' STRIPPED so the open pins
    fall back to insert (no named source, none auto-detected on the bare base)."""
    m = _resolved_manifest()
    del m["supplies"]["avdd1p0"]["tb_src"]               # auto-detect the lone V_AVDD
    for o in m["v_out"]:
        m["v_out"][o].pop("src", None)
    for c in m["i_out"]:
        m["i_out"][c].pop("probe_src", None)
    return m


def test_fallback_insert_open_v_out_emits_iext(tmp_path):
    # an OPEN v_out pin (no src, none on net) -> the OLD Iext_ isource is appended, mag=acm
    m = _open_pin_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll", base_text=_base_scs_open_pins())
    ground = m["ground"]
    for o in m["v_out"]:
        net = m["v_out"][o]["net"]
        assert f"Iext_{o} ({ground} {net}) isource mag=acm_v_out_{o}" in txt


def test_fallback_insert_open_i_out_emits_vprobe(tmp_path):
    # an OPEN i_out pin (no probe_src, none on net) -> the OLD Vprobe_ vsource is appended
    m = _open_pin_manifest()
    txt = _netlist_text(tmp_path, m, "g_i_out_i500n_lpf", base_text=_base_scs_open_pins())
    ground = m["ground"]
    for c in m["i_out"]:
        net = m["i_out"][c]["net"]
        probe = M._probe_name(m, c)                      # Vprobe_<c> (no probe_src named)
        assert probe == f"Vprobe_{c}"
        dc = float(m["i_out"][c]["dc"])
        assert f"{probe} ({net} {ground}) vsource dc={dc:g} mag=acm_i_out_{c}" in txt


def test_reused_source_not_in_hot_group_keeps_mag_var_at_zero(tmp_path):
    # a REUSED source carries its acm VAR every group; a NON-hot group sets that var to 0 in the
    # parameters line (so the reused source is mag=acm_<x> with acm_<x>=0 -> inert).
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0")   # supply is hot; v_out/i_out are NOT
    pll = next(l for l in txt.splitlines() if l.strip().startswith("Iload_pll "))
    assert "mag=acm_v_out_pll" in pll                      # the reused source still carries its var
    pline = next(l for l in txt.splitlines() if l.startswith("parameters "))
    assert "acm_v_out_pll=0" in pline                      # but the var is 0 in this group
    assert "acm_supply_avdd1p0=1" in pline                 # only the supply is hot


# =====================================================================================
# (4) base path handling
# =====================================================================================
def test_base_can_be_file_or_dir(tmp_path):
    m = _resolved_manifest()
    bdir = _base_dir(tmp_path)
    # passing the dir works; passing the file directly works too
    for base in (bdir, bdir / "input.scs"):
        gnl = NA.make_offline_group_netlister(base, m, tmp_path / f"out_{base.name}")
        d = gnl(_group(m, "g_v_out_pll"))
        assert (pathlib.Path(d) / "input.scs").is_file()


def test_i_out_save_reads_reused_source_p(tmp_path):
    # the y/pi save reads <reused-vdc>:p -- the probe name is the reused probe_src (Vbias_*),
    # so the read targets the designer's real vdc current, not an inserted Vprobe_.
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_i_out_i500n_lpf")
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    toks = set(sline.split()[1:])
    assert "Vbias_500n_lpf:p" in toks                    # the reused vdc, not a Vprobe_
    assert "Vprobe_i500n_lpf:p" not in toks


def test_i_out_autodetect_pins_probe_src_for_read(tmp_path):
    # when i_out.probe_src is ABSENT, auto-detect finds the vdc on the net AND pins it back into
    # probe_src, so the save+read (manifest._probe_name) target the reused source, not Vprobe_.
    m = _resolved_manifest()
    for c in m["i_out"]:
        m["i_out"][c].pop("probe_src", None)             # force auto-detect
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")
    # the factory pinned the detected vdc into probe_src
    assert m["i_out"]["i500n_lpf"]["probe_src"] == "Vbias_500n_lpf"
    assert M._probe_name(m, "i500n_lpf") == "Vbias_500n_lpf"
    # and the save reads that reused source's :p
    txt = pathlib.Path(gnl(_group(m, "g_i_out_i500n_lpf")), "input.scs").read_text()
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    assert "Vbias_500n_lpf:p" in set(sline.split()[1:])


# =====================================================================================
# (5) scan_netlist_sources (the GUI 'detected source' view)
# =====================================================================================
def test_scan_returns_instance_master_dc_type_ok(tmp_path):
    m = _resolved_manifest()
    scan = NA.scan_netlist_sources(_base_dir(tmp_path), m)
    # supply: the reused V_AVDD vsource, dc 0.98, right master
    s = scan["supplies"]["avdd1p0"]
    assert s["instance"] == "V_AVDD" and s["master"] == "vsource"
    assert s["dc"] == 0.98 and s["type_ok"] is True and s["net"] == "AVDD1P0"
    # v_out: the reused Iload_pll isource, dc 500u
    o = scan["v_out"]["pll"]
    assert o["instance"] == "Iload_pll" and o["master"] == "isource"
    assert abs(o["dc"] - 500e-6) < 1e-12 and o["type_ok"] is True
    # i_out: the reused Vbias_500n_lpf vsource, dc 1.28
    c = scan["i_out"]["i500n_lpf"]
    assert c["instance"] == "Vbias_500n_lpf" and c["master"] == "vsource"
    assert c["dc"] == 1.28 and c["type_ok"] is True


def test_scan_does_not_raise_on_missing_source(tmp_path):
    # an OPEN pin (no source on the net, no named source) -> instance=None, type_ok=False (no raise)
    m = _open_pin_manifest()
    scan = NA.scan_netlist_sources(_base_dir(tmp_path, _base_scs_open_pins()), m)
    o = scan["v_out"]["pll"]
    assert o["instance"] is None and o["type_ok"] is False
    c = scan["i_out"]["i500n_lpf"]
    assert c["instance"] is None and c["type_ok"] is False
    # the supply IS found (auto-detect) even with no tb_src
    assert scan["supplies"]["avdd1p0"]["instance"] == "V_AVDD"


def test_scan_flags_wrong_master_type_not_ok(tmp_path):
    # a named source of the WRONG master surfaces as type_ok=False (the GUI flags it), no raise
    m = _resolved_manifest()
    m["v_out"]["pll"]["src"] = "V_AVDD"             # a vsource named where an isource is required
    scan = NA.scan_netlist_sources(_base_dir(tmp_path), m)
    o = scan["v_out"]["pll"]
    assert o["instance"] == "V_AVDD" and o["master"] == "vsource" and o["type_ok"] is False


def test_scan_applies_bplus_by_pin(tmp_path):
    # scan applies B+: a '<net:PIN>' placeholder is scanned by PIN (net==pin) -- the raw shipped
    # manifest scans cleanly against the base, no resolution needed first.
    m = M.load(str(WUR))
    scan = NA.scan_netlist_sources(_base_dir(tmp_path), m)
    assert scan["supplies"]["avdd1p0"]["instance"] == "V_AVDD"
    assert scan["supplies"]["avdd1p0"]["net"] == "AVDD1P0"
    assert scan["v_out"]["pll"]["instance"] == "Iload_pll"


# =====================================================================================
# (6) base path handling
# =====================================================================================
def test_missing_base_netlist_errors(tmp_path):
    m = _resolved_manifest()
    with pytest.raises(NA.NetlistAugmentError):
        NA.make_offline_group_netlister(tmp_path / "nope", m, tmp_path / "out")


# =====================================================================================
# (7) STAGE 1b coverage kinds: dc (iv / dropout), tran (slew), ac2 (lin-gate) + op_loads/temp
# =====================================================================================
def test_dc_iv_group_emits_dc_sweep_no_mag_onehot(tmp_path):
    # an I-V group sweeps the REUSED vdc (Vbias_500n_lpf) by a Spectre DC sweep, reads its :p,
    # and does NOT one-hot mag (every acm var stays 0; the swept source carries no mag=)
    m = _resolved_coverage_manifest()
    txt = _netlist_text(tmp_path, m, "g_iv_i500n_lpf")
    dcl = next(l for l in txt.splitlines() if l.startswith(NA.DC_NAME + " "))
    assert dcl == f"{NA.DC_NAME} dc dev=Vbias_500n_lpf param=dc start=0 stop=1 lin=11"
    # the swept i_out save reads the reused vdc :p
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    assert "Vbias_500n_lpf:p" in set(sline.split()[1:])
    # no mag one-hot anywhere: every acm var is 0 and no source carries mag=
    pline = next(l for l in txt.splitlines() if l.startswith("parameters "))
    assert "=1" not in pline and "=2" not in pline
    assert "mag=" not in txt


def test_dc_dropout_group_emits_dc_sweep_on_load_isource(tmp_path):
    # a dropout/load-reg group sweeps the REUSED v_out load isource (Iload_pll) and reads Vout
    m = _resolved_coverage_manifest()
    txt = _netlist_text(tmp_path, m, "g_dc_pll")
    dcl = next(l for l in txt.splitlines() if l.startswith(NA.DC_NAME + " "))
    assert dcl.startswith(f"{NA.DC_NAME} dc dev=Iload_pll param=dc start=")
    assert "log=8" in dcl                              # the log sweep type -> log=<n>
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    assert m["v_out"]["pll"]["net"] in set(sline.split()[1:])
    assert "mag=" not in txt                           # dc sweep -> never one-hot mag


def test_tran_group_rewrites_stepped_source_to_pwl(tmp_path):
    # a slew/transient group rewrites the reused v_out load isource (Iload_pll) to a PWL stepping
    # from->to, emits a tran analysis, reads Vout, and leaves NO stray dc=/mag= on the source line
    m = _resolved_coverage_manifest()
    txt = _netlist_text(tmp_path, m, "g_tr_pll_step1")
    trl = next(l for l in txt.splitlines() if l.startswith(NA.TRAN_NAME + " "))
    assert trl == f"{NA.TRAN_NAME} tran stop=1e-05 step=1e-09"
    iload = next(l for l in txt.splitlines() if l.strip().startswith("Iload_pll "))
    assert "type=pwl" in iload and "wave=[" in iload
    assert "0.0005" in iload and "0.002" in iload       # the from + to values appear in the wave
    assert "dc=" not in iload and "mag=" not in iload   # the dc/mag tokens are dropped
    assert "\\" not in iload                             # collapsed: no dangling backslash
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    assert m["v_out"]["pll"]["net"] in set(sline.split()[1:])


def test_tran_group_pwl_backslash_continuation_safe(tmp_path):
    # the stepped-source rewrite is logical-line aware: a backslash-continued Iload_pll is joined
    # to ONE clean PWL line (no half-rewrite below a dangling backslash)
    m = _resolved_coverage_manifest()
    base = _base_scs().replace(
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u",
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u \\\n    type=idc")
    txt = _netlist_text(tmp_path, m, "g_tr_pll_step1", base_text=base)
    iload = next(l for l in txt.splitlines() if l.strip().startswith("Iload_pll "))
    assert "type=pwl" in iload and "wave=[" in iload and "\\" not in iload
    assert "type=idc" in iload                           # the continued token survives, joined
    assert "dc=" not in iload                            # the dc token dropped on the joined line
    assert not any(l.strip() == "type=idc" for l in txt.splitlines())   # never orphaned/live


def test_ac2_lin_gate_group_sets_hot_var_to_amp(tmp_path):
    # the 2x lin-gate group is a normal reuse-mag ac group, but its ONE hot var = the amp (2),
    # not 1 -- and it reads the Zout node voltage (the v_out net) like the 1x Zout point
    m = _resolved_coverage_manifest()
    txt = _netlist_text(tmp_path, m, "g_z2_pll")
    pline = next(l for l in txt.splitlines() if l.startswith("parameters "))
    assert "acm_v_out_pll=2" in pline                    # hot var = the amp, not 1
    assert pline.count("=2") == 1                         # exactly one hot at the amp
    for var in ("acm_v_out_vco", "acm_supply_avdd1p0", "acm_i_out_i500n_lpf"):
        assert f"{var}=0" in pline
    # the reused load isource still carries its mag (this IS a reuse-mag ac group)
    iload = next(l for l in txt.splitlines() if l.strip().startswith("Iload_pll "))
    assert "mag=acm_v_out_pll" in iload
    # z read: the v_out net is saved + the ac analysis is emitted (not dc/tran)
    sline = next(l for l in txt.splitlines() if l.startswith("save "))
    assert m["v_out"]["pll"]["net"] in set(sline.split()[1:])
    assert f"{NA.AC_NAME} {m['analysis']['ac']}" in txt


def test_op_loads_rewrites_reused_load_dc(tmp_path):
    # op_loads={pll:1.7e-3} rewrites the REUSED Iload_pll dc= to 1.7m (:g -> 0.0017); the default
    # path (op_loads=None) leaves the base dc=500u verbatim
    m = _resolved_coverage_manifest()
    a = tmp_path / "a"; a.mkdir()
    gnl = NA.make_offline_group_netlister(_base_dir(a), m, a / "out", op_loads={"pll": 1.7e-3})
    txt = pathlib.Path(gnl(_group(m, "g_v_out_vco")), "input.scs").read_text()
    iload = next(l for l in txt.splitlines() if l.strip().startswith("Iload_pll "))
    assert "dc=0.0017" in iload and "dc=500u" not in iload
    # default path: no op_loads -> the base dc=500u survives verbatim
    b = tmp_path / "b"; b.mkdir()
    txt0 = _netlist_text(b, m, "g_v_out_vco")
    iload0 = next(l for l in txt0.splitlines() if l.strip().startswith("Iload_pll "))
    assert "dc=500u" in iload0


def test_op_loads_overrides_swept_rail_but_sweep_wins(tmp_path):
    # for a dc dropout group, op_loads still applies (other rails take their op dc); the swept
    # rail's load is set by op_loads in the source line, but the DC sweep over it is what runs
    m = _resolved_coverage_manifest()
    out = tmp_path / "out"
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path), m, out, op_loads={"vco": 3e-3})
    txt = pathlib.Path(gnl(_group(m, "g_dc_pll")), "input.scs").read_text()
    # the non-swept rail (vco) takes the op_loads dc
    ivco = next(l for l in txt.splitlines() if l.strip().startswith("Iload_vco "))
    assert "dc=0.003" in ivco
    # the swept rail still has its dc analysis (the sweep drives it regardless)
    assert any(l.startswith(f"{NA.DC_NAME} dc dev=Iload_pll ") for l in txt.splitlines())


def test_temp_emits_options_line_when_set(tmp_path):
    # temp=125 -> a '_covtemp options temp=125' line; temp=None -> absent
    m = _resolved_coverage_manifest()
    a = tmp_path / "a"; a.mkdir()
    gnl = NA.make_offline_group_netlister(_base_dir(a), m, a / "out", temp=125)
    txt = pathlib.Path(gnl(_group(m, "g_v_out_pll")), "input.scs").read_text()
    tline = next(l for l in txt.splitlines() if l.strip().startswith(NA.COVTEMP_NAME + " "))
    assert tline.split("//")[0].split() == [NA.COVTEMP_NAME, "options", "temp=125"]
    # temp=None (default) -> no covtemp line at all
    b = tmp_path / "b"; b.mkdir()
    txt0 = _netlist_text(b, m, "g_v_out_pll")
    assert NA.COVTEMP_NAME not in txt0


def test_temp_applies_to_coverage_and_ac_groups_alike(tmp_path):
    # the temp options line is emitted on a dc group too (it runs the WHOLE netlist hot)
    m = _resolved_coverage_manifest()
    out = tmp_path / "out"
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path), m, out, temp=-40)
    txt = pathlib.Path(gnl(_group(m, "g_iv_i500n_lpf")), "input.scs").read_text()
    assert any(l.strip().startswith(f"{NA.COVTEMP_NAME} options temp=-40")
               for l in txt.splitlines())


# =====================================================================================
# (8) BACKWARD-COMPAT: a coverage-free manifest yields the IDENTICAL 8 groups + text
# =====================================================================================
def test_coverage_free_manifest_yields_same_eight_groups(tmp_path):
    # the resolved wur (no coverage params) still produces exactly the 8 T0 groups -- the new
    # dc/tran/ac2 group kinds NEVER appear without declared coverage params
    m = _resolved_manifest()
    tags = sorted(g["tag"] for g in RUN.groups(m))
    assert len(tags) == 8
    assert not any(t.startswith(("g_iv_", "g_dc_", "g_tr_", "g_z2_")) for t in tags)


def test_coverage_free_text_unchanged_by_new_kwargs_defaults(tmp_path):
    # with the new kwargs at their defaults (op_loads=None, temp=None) every T0 group's emitted
    # text is byte-identical to the no-kwargs call -- the new paths fire ONLY for the new kinds
    m = _resolved_manifest()
    for i, tag in enumerate(g["tag"] for g in RUN.groups(m)):
        ad = tmp_path / f"a{i}"; ad.mkdir()
        bd = tmp_path / f"b{i}"; bd.mkdir()
        a = _netlist_text(ad, m, tag)                    # the no-kwargs call (via _netlist_text)
        gnl = NA.make_offline_group_netlister(_base_dir(bd), m, bd / "out",
                                              op_loads=None, temp=None)
        b = pathlib.Path(gnl(_group(m, tag)), "input.scs").read_text()
        assert a == b, f"new-kwargs default changed T0 text for {tag}"
        assert NA.COVTEMP_NAME not in a                  # no temp line on a coverage-free run


# =====================================================================================
# (9) subckt SCOPE awareness -- by-name resolvers/rewriters match ONLY at depth 0
#     (regression for the real bug: a DUT subckt pass device named the same as a top-level
#      TB source the manifest reuses, e.g. both `I1`; the scope-blind first-match grabbed the
#      subckt device -> wrong-master raise / mag landed on the wrong instance.)
# =====================================================================================
_COLLIDE = (
    "simulator lang=spectre\n"
    "subckt PMU_top (vout avdd nbias gnd)\n"
    "  I1 (vout avdd nbias gnd) pmos_18_1umL w=1u l=180n\n"     # subckt pass device named I1
    "  R1 (vout gnd) resistor r=1meg\n"
    "ends PMU_top\n"
    "Xdut (VDD0P8_PLL AVDD1P0 nbias 0) PMU_top\n"
    "I1 (VDD0P8_PLL 0) isource dc=500u\n"                       # top-level load idc, ALSO I1
)


def test_subckt_delta_classifies_headers_and_ends():
    assert NA._subckt_delta("subckt foo a b c") == +1
    assert NA._subckt_delta("subckt foo (a b c)") == +1
    assert NA._subckt_delta("inline subckt foo a b") == +1
    assert NA._subckt_delta(".subckt foo a b") == +1
    assert NA._subckt_delta(".SUBCKT FOO A B") == +1            # spice directives case-insensitive
    assert NA._subckt_delta("ends foo") == -1
    assert NA._subckt_delta("ends") == -1
    assert NA._subckt_delta(".ends") == -1
    assert NA._subckt_delta("// subckt in a comment") == 0      # commented text never counts
    assert NA._subckt_delta("I1 (a b) isource dc=1") == 0       # a plain instance
    assert NA._subckt_delta("simulator lang=spectre") == 0      # lang switch is not a delimiter


def test_find_instance_skips_subckt_internal():
    inst = NA._find_instance(_COLLIDE, "I1")
    assert inst is not None and inst[2] == "isource"            # the TOP-LEVEL isource, not the pmos
    assert inst[1] == ["VDD0P8_PLL", "0"]


def test_detect_on_net_skips_subckt_internal():
    hit = NA._detect_on_net(_COLLIDE, "VDD0P8_PLL", prefer_master="isource")
    assert hit is not None and hit[0] == "I1" and hit[1] == "isource"


def test_modify_mag_lands_on_top_level_not_subckt():
    new, found = NA._modify_mag(_COLLIDE, "I1", "acm_x")
    assert found
    pmos = next(l for l in new.splitlines() if "pmos_18_1umL" in l)
    assert "mag=" not in pmos                                   # subckt pass device untouched
    top = next(l for l in new.splitlines()
               if l.strip().startswith("I1 (VDD0P8_PLL 0) isource"))
    assert "mag=acm_x" in top


def test_base_nets_excludes_subckt_internal_nets():
    nets = NA._base_nets(_COLLIDE)
    assert "VDD0P8_PLL" in nets and "AVDD1P0" in nets          # top-level (Xdut nodes + sources)
    assert "nbias" in nets                                     # also top-level (in the Xdut node list)
    assert "vout" not in nets and "gnd" not in nets           # subckt-internal-only formal nodes excluded


def test_nested_subckt_depth_tracking():
    txt = (
        "subckt outer (a b)\n"
        "  subckt inner (c d)\n"
        "    I1 (c d) isource dc=1\n"                          # depth 2
        "  ends inner\n"
        "  I1 (a b) isource dc=2\n"                            # depth 1 -> still NOT top level
        "ends outer\n"
        "I1 (top 0) isource dc=3\n"                            # depth 0
    )
    inst = NA._find_instance(txt, "I1")
    assert inst[1] == ["top", "0"]                             # only the depth-0 one matches
    new, found = NA._modify_mag(txt, "I1", "acm_x")
    hot = [l for l in new.splitlines() if "mag=acm_x" in l]
    assert len(hot) == 1 and hot[0].strip().startswith("I1 (top 0)")


def test_inline_subckt_scope():
    txt = (
        "inline subckt buf (i o)\n"
        "  I1 (i o) isource dc=1\n"
        "ends buf\n"
        "I1 (net 0) isource dc=2\n"
    )
    assert NA._find_instance(txt, "I1")[1] == ["net", "0"]


def test_spice_lang_subckt_scope():
    # `.subckt`/`.ends` must delimit scope too (parens kept so _parse_instance would otherwise
    # see the inner line as a matching instance -- proving the depth guard, not the parser, skips it)
    txt = (
        "simulator lang=spice\n"
        ".subckt buf i o\n"
        "I1 (i o) isource dc=1\n"
        ".ends\n"
        "I1 (net 0) isource dc=2\n"
    )
    assert NA._find_instance(txt, "I1")[1] == ["net", "0"]


def test_subckt_collision_full_build_resolves_top_level(tmp_path):
    # END-TO-END: the user's exact failure -- a reused v_out source (Iload_pll) collides by name
    # with a DUT subckt pass device. Build must NOT raise and the mag must land on the top-level
    # isource, never the subckt pmos.
    base = (
        "simulator lang=spectre\n"
        'include "models.scs"\n'
        "inline subckt WuR_PMU_TOP (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF "
        "IBP_POLY_3P6U_VCO IBP_PTAT_TUNE_1P5U_VCO VSS)\n"
        "  Iload_pll (VDD0P8_PLL AVDD1P0 nbias VSS) pmos_18_1umL w=1u l=180n\n"   # subckt pass dev
        "  R_int (VDD0P8_PLL VSS) resistor r=1meg\n"
        "ends WuR_PMU_TOP\n"
        "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO "
        "IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\n"
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u\n"                              # top-level load idc
        "Iload_vco (VDD0P8_VCO 0) isource dc=2m\n"
        "Vbias_500n_lpf (IBP_POLY_500N_LPF 0) vsource dc=1.28\n"
        "Vbias_3p6u_vco (IBP_POLY_3P6U_VCO 0) vsource dc=1.28\n"
        "Vbias_1p5u_ptat (IBP_PTAT_TUNE_1P5U_VCO 0) vsource dc=0.667\n"
        "tt tran stop=1u\n"
    )
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll", base_text=base)              # must not raise
    top = next(l for l in txt.splitlines()
               if l.strip().startswith("Iload_pll (VDD0P8_PLL 0) isource"))
    assert "mag=acm_v_out_pll" in top                                            # top-level got mag
    pmos = next(l for l in txt.splitlines()
                if "pmos_18_1umL" in l and "Iload_pll" in l)
    assert "mag=" not in pmos                                                    # subckt dev untouched


# =====================================================================================
# (10) iv coverage with SPECIFIC POINTS -> the dc analysis folds sweep+points into one
#      `values=[...]` value list; sweep-only stays the proven start=/stop=/lin= form.
# =====================================================================================
def _iv_cov_manifest(iv_spec):
    raw = re.sub(r"<net:([^>]+)>", r"\1", WUR.read_text())
    d = json.loads(raw)
    d["coverage"] = {"tier": "T4", "iv": {"i500n_lpf": iv_spec}}
    return M.load(_write_tmp(d))


def _dc_line(txt):
    return next(l for l in txt.splitlines() if l.strip().startswith(NA.DC_NAME + " dc"))


def test_iv_sweep_only_emits_classic_clause(tmp_path):
    m = _iv_cov_manifest({"sweep": {"type": "lin", "start": 0.0, "stop": 0.8, "n": 5}})
    dc = _dc_line(_netlist_text(tmp_path, m, "g_iv_i500n_lpf"))
    assert "lin=5" in dc and "start=0" in dc and "stop=0.8" in dc
    assert "values=[" not in dc                          # no-points path unchanged


def test_iv_sweep_with_points_emits_union_values_list(tmp_path):
    m = _iv_cov_manifest({"sweep": {"type": "lin", "start": 0.0, "stop": 0.8, "n": 5},
                          "points": [0.05, 0.42]})
    dc = _dc_line(_netlist_text(tmp_path, m, "g_iv_i500n_lpf"))
    # grid 0,0.2,0.4,0.6,0.8 UNION points 0.05,0.42 -> sorted dedup value list
    assert "values=[0 0.05 0.2 0.4 0.42 0.6 0.8]" in dc
    assert "lin=" not in dc


def test_iv_points_only_emits_values_list(tmp_path):
    m = _iv_cov_manifest({"points": [0.1, 0.3, 0.5]})
    dc = _dc_line(_netlist_text(tmp_path, m, "g_iv_i500n_lpf"))
    assert "values=[0.1 0.3 0.5]" in dc


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
