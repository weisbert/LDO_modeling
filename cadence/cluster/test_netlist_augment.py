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
    """A synthetic base .tran TB: simulator-lang header, the DUT instance, a supply vsource on
    the (resolved) supply net, an output load isource, and a .tran analysis to be stripped."""
    return (
        "simulator lang=spectre\n"
        'include "models.scs"\n'
        "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO "
        "IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\n"
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u\n"
        "tt tran stop=1u\n"
    )


def _base_scs_continuations():
    """A base .tran TB that uses BACKSLASH line-continuations on the DUT instance, the supply
    vsource, AND the .tran analysis -- a realistic maestro formatting. The offline netlister
    must join logical statements before stripping / mag-modifying, or it half-processes them."""
    return (
        "simulator lang=spectre\n"
        'include "models.scs"\n'
        "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF \\\n"
        "      IBP_POLY_3P6U_VCO IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98 \\\n"
        "    type=dc\n"
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u\n"
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


def test_iext_and_probe_lines_present_with_augment_node_order(tmp_path):
    m = _resolved_manifest()
    txt = _netlist_text(tmp_path, m, "g_v_out_pll")
    ground = m["ground"]
    # v_out isource: PLUS=ground, MINUS=net (mirror augment: +1A into the out net), mag=acm
    for o in m["v_out"]:
        net = m["v_out"][o]["net"]
        assert f"Iext_{o} ({ground} {net}) isource mag=acm_v_out_{o}" in txt
    # i_out probe: PLUS=net, MINUS=ground, dc=<compliance>, mag=acm
    for c in m["i_out"]:
        net = m["i_out"][c]["net"]
        probe = M._probe_name(m, c)
        dc = float(m["i_out"][c]["dc"])
        assert f"{probe} ({net} {ground}) vsource dc={dc:g} mag=acm_i_out_{c}" in txt


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
    assert "tb_src" not in m["supplies"]["avdd1p0"]       # pure auto-detect, no override
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0", base_text=_base_scs_continuations())
    assert "mag=acm_supply_avdd1p0" in txt                # the wrapped V_AVDD was found + modified


# =====================================================================================
# (2) the resolved-net GUARD
# =====================================================================================
def test_guard_trips_on_placeholder_manifest(tmp_path):
    # the shipped manifest ships '<net:...>' placeholders -> the factory must refuse
    m = M.load(str(WUR))
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")
    msg = str(ei.value)
    assert "<net:" in msg or "placeholder" in msg.lower()
    assert "resolve" in msg.lower()                 # actionable: tells the designer to resolve


def test_guard_passes_on_resolved_manifest(tmp_path):
    m = _resolved_manifest()
    # no raise
    NA.make_offline_group_netlister(_base_dir(tmp_path), m, tmp_path / "out")


# =====================================================================================
# (3) supply auto-detect / tb_src override
# =====================================================================================
def test_auto_detect_finds_lone_vsource(tmp_path):
    m = _resolved_manifest()
    # the supply has NO tb_src -> auto-detect must find the lone V_AVDD on AVDD1P0
    assert "tb_src" not in m["supplies"]["avdd1p0"]
    txt = _netlist_text(tmp_path, m, "g_supply_avdd1p0")
    assert "V_AVDD (AVDD1P0 0) vsource dc=0.98 mag=acm_supply_avdd1p0" in txt


def test_auto_detect_ambiguous_errors(tmp_path):
    # two vsources whose FIRST node is the supply net -> ambiguous, actionable error
    base = _base_scs().replace(
        "V_AVDD (AVDD1P0 0) vsource dc=0.98",
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\nV_AVDD2 (AVDD1P0 0) vsource dc=0.98")
    m = _resolved_manifest()
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(_base_dir(tmp_path, base), m, tmp_path / "out")
    msg = str(ei.value)
    assert "AMBIGUOUS" in msg or "ambiguous" in msg.lower()
    assert "tb_src" in msg                          # tells the designer how to disambiguate
    assert "V_AVDD" in msg and "V_AVDD2" in msg     # lists the candidates


def test_auto_detect_missing_errors(tmp_path):
    # no vsource drives the supply net -> actionable missing error
    base = _base_scs().replace("V_AVDD (AVDD1P0 0) vsource dc=0.98\n", "")
    m = _resolved_manifest()
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


def test_tb_src_naming_missing_instance_errors(tmp_path):
    # tb_src names a source that is NOT in the base netlist -> a clear per-group error
    m = _resolved_manifest()
    m["supplies"]["avdd1p0"]["tb_src"] = "V_NONEXISTENT"
    out = tmp_path / "out"
    gnl = NA.make_offline_group_netlister(_base_dir(tmp_path), m, out)
    with pytest.raises(NA.NetlistAugmentError) as ei:
        gnl(_group(m, "g_supply_avdd1p0"))
    assert "V_NONEXISTENT" in str(ei.value)


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


def test_missing_base_netlist_errors(tmp_path):
    m = _resolved_manifest()
    with pytest.raises(NA.NetlistAugmentError):
        NA.make_offline_group_netlister(tmp_path / "nope", m, tmp_path / "out")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
