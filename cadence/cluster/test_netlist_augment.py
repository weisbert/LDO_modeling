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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
